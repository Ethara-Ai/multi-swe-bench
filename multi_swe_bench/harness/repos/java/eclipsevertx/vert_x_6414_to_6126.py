import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

PREPARE = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __SHA__
bash /home/check_git_changes.sh

SETTINGS=""
if [ -f .github/maven-ci-settings.xml ]; then
    SETTINGS="-s .github/maven-ci-settings.xml"
fi
MVN="mvn $SETTINGS -B -ntp -Dcheckstyle.skip=true -Dmaven.javadoc.skip=true -Dmaven.source.skip=true"

grep -hE '^diff --git a/' /home/test.patch /home/fix.patch \\
    | sed -E 's|^diff --git a/[^ ]+ b/||' | sort -u > /home/changed.txt
grep -E '(^|/)src/' /home/changed.txt | sed -E 's#(^|/)src/.*##' | sort -u > /home/modules.txt
grep -E '(^|/)src/test/java/.+\\.java$' /home/changed.txt | sort -u > /home/changed_tests.txt

TEST_DIRS=""
while IFS= read -r module; do
    dir="${module:+$module/}src/test/java"
    if [ -d "$dir" ]; then
        TEST_DIRS="$TEST_DIRS $dir"
    fi
done < /home/modules.txt

: > /home/tests.txt
: > /home/pending.txt
while IFS= read -r source; do
    if [ -z "$source" ]; then
        continue
    fi
    cls="$(basename "$source" .java)"
    if [ ! -f "$source" ]; then
        echo "$cls" >> /home/tests.txt
    elif ! grep -q '@Test' "$source"; then
        :
    elif grep -qE "abstract[[:space:]]+class[[:space:]]+$cls([[:space:]]|<|\\{)" "$source"; then
        echo "$cls" >> /home/pending.txt
    else
        echo "$cls" >> /home/tests.txt
    fi
done < /home/changed_tests.txt

round=0
while [ -s /home/pending.txt ] && [ "$round" -lt 8 ]; do
    : > /home/pending_next.txt
    while IFS= read -r cls; do
        if [ -z "$cls" ]; then
            continue
        fi
        for dir in $TEST_DIRS; do
            while IFS= read -r sub; do
                if [ -z "$sub" ]; then
                    continue
                fi
                subcls="$(basename "$sub" .java)"
                if grep -qE "abstract[[:space:]]+class[[:space:]]+$subcls([[:space:]]|<|\\{)" "$sub"; then
                    echo "$subcls" >> /home/pending_next.txt
                else
                    echo "$subcls" >> /home/tests.txt
                fi
            done < <(grep -rlE "extends[[:space:]]+$cls([[:space:]]|<|\\{)" $dir --include='*.java' 2>/dev/null || true)
        done
    done < /home/pending.txt
    sort -u /home/pending_next.txt > /home/pending.txt
    round=$((round + 1))
done
sort -u -o /home/tests.txt /home/tests.txt

PL="$(sed '/^$/d' /home/modules.txt | paste -sd, -)"

rc=0
$MVN install -DskipTests > /home/mvn-install.log 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
    if grep -q 'vertx-dependencies' /home/mvn-install.log \\
        && grep -qE 'Non-resolvable import POM|Could not find artifact|could not be resolved' /home/mvn-install.log; then
        PROJECT_VERSION="$(sed -n '/<\\/parent>/,$p' pom.xml | grep -m1 -oE '<version>[^<]+</version>' | sed 's|</*version>||g')"
        test -n "$PROJECT_VERSION"
        sed -i "/<artifactId>vertx-dependencies<\\/artifactId>/{n;s|<version>[^<]*</version>|<version>${PROJECT_VERSION%-SNAPSHOT}</version>|}" pom.xml
        $MVN install -DskipTests > /home/mvn-install-retry.log 2>&1
    else
        tail -40 /home/mvn-install.log
        exit "$rc"
    fi
fi

$MVN ${PL:+-pl "$PL"} test-compile > /home/mvn-test-compile.log 2>&1 \\
    || { tail -40 /home/mvn-test-compile.log; exit 1; }
$MVN ${PL:+-pl "$PL"} -Dtest='__NoSuchTestClass__' -Dsurefire.failIfNoSpecifiedTests=false test \\
    > /home/mvn-warm.log 2>&1 || true

test -f pom.xml
test -s /home/changed_tests.txt
test -s /home/tests.txt
while IFS= read -r module; do
    if [ -n "$module" ]; then
        test -f "$module/pom.xml"
    fi
done < /home/modules.txt
java -version
mvn -v > /dev/null
find /root/.m2/repository/org/apache/maven/plugins/maven-surefire-plugin -name '*.jar' | grep -q .
while IFS= read -r cls; do
    find . -path '*/target/test-classes/*' -name "$cls.class" | grep -q .
done < /home/tests.txt
echo "DEPS_OK modules=[$PL] tests=$(wc -l < /home/tests.txt | tr -d ' ')"
"""

TEST_COMMAND = """SETTINGS=""
if [ -f .github/maven-ci-settings.xml ]; then
    SETTINGS="-s .github/maven-ci-settings.xml"
fi
PL="$(sed '/^$/d' /home/modules.txt | paste -sd, -)"
TESTS="$(paste -sd, - < /home/tests.txt)"

rc=0
mvn $SETTINGS -B -ntp -Dcheckstyle.skip=true -Dmaven.javadoc.skip=true -Dmaven.source.skip=true \\
    ${PL:+-pl "$PL"} -Dtest="$TESTS" -Dsurefire.failIfNoSpecifiedTests=false -DskipITs test || rc=$?

echo "===== BEGIN TEST RESULTS ====="
find . -path '*/surefire-reports/TEST-*.xml' -print0 2>/dev/null \\
  | while IFS= read -r -d '' f; do
        rel="${f#./}"
        module="${rel%%target/surefire-reports/*}"
        module="${module%/}"
        cls=$(grep -o '<testsuite[^>]*' "$f" | head -1 | sed -n 's/.*[[:space:]]name="\\([^"]*\\)".*/\\1/p' || true)
        if [ -z "$cls" ]; then
            continue
        fi
        outer="${cls%%\\$*}"
        path="$(printf '%s' "$outer" | tr '.' '/')"
        echo "##### FILE: ${module:+$module/}src/test/java/$path.java"
        cat "$f"
        echo
    done
echo "===== END TEST RESULTS ====="
exit $rc
"""

RUN_PREFIX = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
"""

APPLY_TEST_PATCH = "git apply --whitespace=nowarn /home/test.patch\n"

APPLY_BOTH_PATCHES = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"


class VertxImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "maven:3.9-eclipse-temurin-11"

    def image_tag(self) -> str:
        return "base-6414_to_6126"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{DockerfileEnhancer._infrastructure_block(self, image_name)}
{self.global_env}

ENV LC_ALL=C.UTF-8
ENV MAVEN_OPTS="-Xmx2g -Dfile.encoding=UTF-8"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class VertxImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return VertxImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                CHECK_GIT_CHANGES,
            ),
            File(
                ".",
                "prepare.sh",
                PREPARE.replace("__REPO__", repo).replace("__SHA__", self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                RUN_PREFIX.replace("__REPO__", repo) + TEST_COMMAND,
            ),
            File(
                ".",
                "test-run.sh",
                RUN_PREFIX.replace("__REPO__", repo) + APPLY_TEST_PATCH + TEST_COMMAND,
            ),
            File(
                ".",
                "fix-run.sh",
                RUN_PREFIX.replace("__REPO__", repo)
                + APPLY_BOTH_PATCHES
                + TEST_COMMAND,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

{Image._HARDENING_BLOCK}"""


@Instance.register("eclipse-vertx", "vert.x")
class VERT_X_6414_TO_6126(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VertxImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi = re.compile(r"\x1B\[[0-?9;]*[mK]")
        clean = ansi.sub("", test_log)

        marker = re.compile(r"^##### FILE: (\S+)[ \t]*$", re.M)
        chunks = marker.split(clean)[1:]

        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        name_re = re.compile(r'\bname="([^"]*)"')
        classname_re = re.compile(r'\bclassname="([^"]*)"')

        for source_file, body in zip(chunks[0::2], chunks[1::2]):
            for testcase in testcase_re.finditer(body):
                attrs = testcase.group(1) or ""
                closing = testcase.group(2)
                inner = testcase.group(3) or ""

                name_match = name_re.search(attrs)
                if not name_match:
                    continue

                classname_match = classname_re.search(attrs)
                nested = ""
                if classname_match and "$" in classname_match.group(1):
                    nested = (
                        classname_match.group(1).split("$", 1)[1].replace("$", ".") + "."
                    )

                test_id = f"{source_file}::{nested}{name_match.group(1)}"

                if closing == "/>":
                    passed_tests.add(test_id)
                elif "<failure" in inner or "<error" in inner:
                    failed_tests.add(test_id)
                elif "<skipped" in inner:
                    skipped_tests.add(test_id)
                else:
                    passed_tests.add(test_id)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
