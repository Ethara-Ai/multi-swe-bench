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
set -e

cd /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

chmod +x ./gradlew || true

{ grep -hE '^diff --git a/' /home/test.patch /home/fix.patch 2>/dev/null \\
    | sed -E 's|^diff --git a/[^ ]+ b/||' \\
    | grep -E '/src/(main|test|testFixtures)/' \\
    | grep -vE '^(buildSrc|gradle)/' \\
    | sed -E 's|/src/.*||' \\
    | sed -E 's|^|:|; s|/|:|g' \\
    | sort -u > /home/modules.txt ; } || true

GRADLE_VERSION="$(sed -n 's|.*gradle-\\([0-9.][0-9.]*\\)-bin.zip|\\1|p' gradle/wrapper/gradle-wrapper.properties)" || true
GRADLE_DIST_URL="$(sed -n 's|^distributionUrl=||p' gradle/wrapper/gradle-wrapper.properties | sed 's|\\\\||g')" || true
timeout 120 ./gradlew --version > /dev/null 2>&1 || true
GRADLE_DIST_DIR="$(find "$GRADLE_USER_HOME/wrapper/dists" -maxdepth 2 -type d 2>/dev/null | grep -E "gradle-$GRADLE_VERSION-bin/[a-z0-9][a-z0-9]*$" | head -1)" || true
curl -fsSL --retry 3 --retry-delay 5 --max-time 1800 -o "$GRADLE_DIST_DIR/gradle-$GRADLE_VERSION-bin.zip" "$GRADLE_DIST_URL" || true

WARM_TASKS=""
while IFS= read -r project; do
    if [ -n "$project" ]; then
        WARM_TASKS="$WARM_TASKS $project:testClasses"
    fi
done < /home/modules.txt || true
timeout --kill-after=60 3600 ./gradlew $WARM_TASKS --no-daemon --continue --max-workers=4 || true

test -s /home/modules.txt
test -n "$GRADLE_VERSION"
java -version
./gradlew --version | grep -qx "Gradle $GRADLE_VERSION"
while IFS= read -r project; do
    dir="$(printf '%s' "${project#:}" | tr ':' '/')"
    test -f "$dir/build.gradle"
    grep -qF "include \\"${project#:}\\"" settings.gradle
done < /home/modules.txt

bash /home/check_git_changes.sh
"""

TEST_COMMAND = """TASKS=""
while IFS= read -r project; do
    if [ -n "$project" ]; then
        TASKS="$TASKS $project:test"
    fi
done < /home/modules.txt

rc=0
timeout --kill-after=60 3600 ./gradlew $TASKS --no-daemon --continue --max-workers=4 || rc=$?

echo "===== BEGIN TEST RESULTS ====="
find . -path ./buildSrc -prune -o -path ./gradle -prune -o -path '*/build/test-results/test/TEST-*.xml' -print0 2>/dev/null \\
  | while IFS= read -r -d '' f; do
        rel="${f#./}"
        module="${rel%%/build/test-results/test/*}"
        cls=$(grep -o '<testsuite[^>]*' "$f" | head -1 | sed -n 's/.*[[:space:]]name="\\([^"]*\\)".*/\\1/p')
        if [ -z "$cls" ]; then
            continue
        fi
        outer="${cls%%\\$*}"
        path="$(printf '%s' "$outer" | tr '.' '/')"
        src=""
        for cand in "test/java:java" "test/kotlin:kt" "testFixtures/java:java" "testFixtures/kotlin:kt"; do
            d="${cand%%:*}"
            e="${cand##*:}"
            if [ -f "$module/src/$d/$path.$e" ]; then
                src="$module/src/$d/$path.$e"
                break
            fi
        done
        if [ -z "$src" ]; then
            src="$module/src/test/java/$path.java"
        fi
        echo "##### FILE: $src"
        cat "$f"
        echo
    done
echo "===== END TEST RESULTS ====="
exit $rc
"""

RUN_PREFIX = """#!/bin/bash
set -eo pipefail
export CI=false
export ENABLE_PREDICTIVE_TEST_SELECTION=false

cd /home/__REPO__
"""

APPLY_TEST_PATCH = "git apply --whitespace=nowarn /home/test.patch\n"

APPLY_BOTH_PATCHES = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"


class SpringBootImageBase(Image):
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
        return "eclipse-temurin:25-jdk"

    def image_tag(self) -> str:
        return "base"

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
ENV GRADLE_USER_HOME=/root/.gradle
ENV GRADLE_OPTS="-Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false -Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class SpringBootImageDefault(Image):
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
        return SpringBootImageBase(self.pr, self._config)

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


@Instance.register("spring-projects", "spring-boot")
class SpringBoot52246To50461(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpringBootImageDefault(self.pr, self._config)

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
