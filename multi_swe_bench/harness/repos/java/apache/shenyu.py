from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_JDK_IMAGE = "maven:3.9.9-eclipse-temurin-17"
_MVN = "mvn -B --no-transfer-progress -fae -Daether.connector.http.retryHandler.count=5"
_SKIPS = "-Dcheckstyle.skip=true -Drat.skip=true -Djacoco.skip=true -Dmaven.javadoc.skip=true"
_SUREFIRE_FLAGS = "-Dmaven.test.failure.ignore=true -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false"
_MAVEN_OPTS = "-Xmx2g"
_NO_TESTS = "-DskipTests=true -Dmaven.test.skip=true"

_OUTSIDE_REACTOR = {"shenyu-e2e", "shenyu-examples", "shenyu-integrated-test"}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DIFF_FILE_RE = re.compile(r"^diff --git a/(\S+) b/", re.M)
_FILE_MARKER_RE = re.compile(r"^##### FILE: (\S+)[ \t]*$", re.M)
_MODULE_RE = re.compile(r"^(?:\./)?(.*?)/?target/surefire-reports/")
_TESTCASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
_NAME_RE = re.compile(r'\bname="([^"]*)"')
_CLASSNAME_RE = re.compile(r'\bclassname="([^"]*)"')


def _patch_files(patch: str) -> list[str]:
    return _DIFF_FILE_RE.findall(patch or "")


def _modules(paths: list[str]) -> list[str]:
    mods = set()
    for path in paths:
        if "/src/" in path:
            mod = path.split("/src/", 1)[0]
        elif path.endswith("/pom.xml"):
            mod = path[: -len("/pom.xml")]
        else:
            continue
        if mod.split("/", 1)[0] in _OUTSIDE_REACTOR:
            continue
        mods.add(mod)
    return sorted(mods)


def _touches_root_pom(pr: PullRequest) -> bool:
    return "pom.xml" in _patch_files(pr.test_patch) + _patch_files(pr.fix_patch)


def _test_modules(pr: PullRequest) -> list[str]:
    return _modules(_patch_files(pr.test_patch))


def _build_modules(pr: PullRequest) -> list[str]:
    return _modules(_patch_files(pr.test_patch) + _patch_files(pr.fix_patch))


def _present_modules(var: str, mods: list[str]) -> str:
    return f"""{var}=""
for m in {" ".join(mods)}; do
    [ -f "$m/pom.xml" ] && {var}="${{{var}:+${var},}}$m"
done"""


def _test_block(pr: PullRequest) -> str:
    repo = pr.repo
    test_mods = _test_modules(pr)
    build_mods = _build_modules(pr)
    if not test_mods:
        return 'echo "No Maven module with tests is touched by test.patch; nothing to run."\nexit 0'
    root_install = (
        f"{_MVN} -N install {_NO_TESTS} {_SKIPS}\n" if _touches_root_pom(pr) else ""
    )
    return f"""find . -type d -name surefire-reports -prune -exec rm -rf {{}} +
{_present_modules("BUILD_PL", build_mods)}
{_present_modules("TEST_PL", test_mods)}
if [ -z "$TEST_PL" ]; then
    echo "None of the test modules exist in this tree; nothing to run."
    exit 0
fi
{root_install}{_MVN} install {_NO_TESTS} {_SKIPS} -pl "$BUILD_PL" -am
for attempt in 1 2 3 4 5; do
    compile_rc=0
    {_MVN} test-compile {_SKIPS} -pl "$TEST_PL" > /home/compile.log 2>&1 || compile_rc=$?
    cat /home/compile.log
    [ "$compile_rc" -eq 0 ] && break
    bad=$( {{ grep -oE '^\\[ERROR\\] /home/{repo}/[^ :]*/src/test/java/[^ :]+\\.java:\\[[^]]*\\]' /home/compile.log || true; }} | sed -e 's|^\\[ERROR\\] ||' -e 's|:\\[[^]]*\\]$||' | sort -u)
    [ -n "$bad" ] || break
    printf '%s\\n' "$bad" | while IFS= read -r f; do
        echo "QUARANTINE attempt $attempt: ${{f#/home/{repo}/}}"
        rm -f "$f"
    done
done
rc=0
{_MVN} test {_SUREFIRE_FLAGS} {_SKIPS} -pl "$TEST_PL" || rc=$?
echo "===== BEGIN TEST RESULTS ====="
find . -path '*/target/surefire-reports/TEST-*.xml' -print0 | sort -z | while IFS= read -r -d '' f; do
    echo "##### FILE: ${{f#./}}"
    cat "$f"
    echo
done
echo "===== END TEST RESULTS ====="
if [ "$rc" -ne 0 ] && [ -z "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)" ]; then
    echo "FATAL: maven exited $rc and produced no surefire reports" >&2
    exit "$rc"
fi
exit 0"""


_MAVEN_SETTINGS = """\
mkdir -p ~/.m2
cat > ~/.m2/settings.xml <<'EOF'
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">
  <mirrors>
    <mirror>
      <id>google-maven-central</id>
      <mirrorOf>central</mirrorOf>
      <name>Google mirror of Maven Central</name>
      <url>https://maven-central.storage-download.googleapis.com/maven2/</url>
    </mirror>
  </mirrors>
</settings>
EOF"""

_PROVIDER_PREFETCH = f"""\
SUREFIRE_VERSION=$(sed -n 's:.*<maven-surefire-plugin.version>\\(.*\\)</maven-surefire-plugin.version>.*:\\1:p' pom.xml | head -1)
{_MVN} -N dependency:get -Dartifact=org.apache.maven.surefire:surefire-junit-platform:$SUREFIRE_VERSION
for v in $(ls ~/.m2/repository/org/junit/platform/junit-platform-engine/ 2>/dev/null); do
    {_MVN} -N dependency:get -Dartifact=org.junit.platform:junit-platform-launcher:$v
done
test -f ~/.m2/repository/org/apache/maven/surefire/surefire-junit-platform/$SUREFIRE_VERSION/surefire-junit-platform-$SUREFIRE_VERSION.jar"""


def _provision_block(pr: PullRequest) -> str:
    test_mods = _test_modules(pr)
    build_mods = _build_modules(pr)
    if not test_mods:
        return f"{_MAVEN_SETTINGS}\n{_MVN} -N validate {_SKIPS}"
    return f"""{_MAVEN_SETTINGS}
{_present_modules("BUILD_PL", build_mods)}
{_present_modules("TEST_PL", test_mods)}
if [ -z "$BUILD_PL" ]; then
    {_MVN} -N install -DskipTests {_SKIPS}
else
    {_MVN} install -DskipTests {_SKIPS} -pl "$BUILD_PL" -am
fi
{_PROVIDER_PREFETCH}
if [ -n "$TEST_PL" ]; then
    {_MVN} test-compile {_SKIPS} -pl "$TEST_PL"
fi"""


class ShenyuImageBase(Image):
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
        return _JDK_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class ShenyuImageDefault(Image):
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
        return ShenyuImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

export MAVEN_OPTS="{_MAVEN_OPTS}"
java -version
mvn -version

{_provision_block(self.pr)}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
export MAVEN_OPTS="{_MAVEN_OPTS}"
{_test_block(self.pr)}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
export MAVEN_OPTS="{_MAVEN_OPTS}"
git apply --whitespace=nowarn /home/test.patch
{_test_block(self.pr)}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
export MAVEN_OPTS="{_MAVEN_OPTS}"
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_test_block(self.pr)}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ShenyuImageDefault dependency must be an Image")
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

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}
"""


@Instance.register("apache", "shenyu")
class SHENYU(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ShenyuImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        chunks = _FILE_MARKER_RE.split(_ANSI_RE.sub("", test_log))[1:]
        for report_path, body in zip(chunks[0::2], chunks[1::2]):
            module_match = _MODULE_RE.match(report_path)
            module = module_match.group(1) if module_match else ""
            for case in _TESTCASE_RE.finditer(body):
                attrs = case.group(1) or ""
                name = _NAME_RE.search(attrs)
                classname = _CLASSNAME_RE.search(attrs)
                if not name or not classname:
                    continue
                rel = classname.group(1).split("$", 1)[0].replace(".", "/") + ".java"
                source = f"{module}/src/test/java/{rel}" if module else f"src/test/java/{rel}"
                test_id = f"{source}::{name.group(1)}"
                inner = case.group(3) or ""
                if case.group(2) == "/>":
                    passed.add(test_id)
                elif "<failure" in inner or "<error" in inner:
                    failed.add(test_id)
                elif "<skipped" in inner:
                    skipped.add(test_id)
                else:
                    passed.add(test_id)

        passed -= failed
        skipped -= failed
        skipped -= passed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
