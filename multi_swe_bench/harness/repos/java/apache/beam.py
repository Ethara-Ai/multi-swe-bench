import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# apache/beam is a polyglot monorepo. Each PR is graded with the SDK whose
# unit tests its test.patch touches (see _kind()):
#
# * "java":   test.patch changes test sources of a Gradle project under
#   sdks/java/... (other than *IT.java, which need GCP/containers and are not
#   part of the `test` task). The build at the base sha (gradle.properties
#   javaVersion=11, .github/actions/setup-environment-action java-version
#   'default' = 21, Gradle 8.14.3 wrapper) runs on JDK 21; eclipse-temurin is
#   multi-arch, toolchain auto-download is disabled. Only the touched
#   project(s) are built and tested: `:<project>:test`.
# * "python": test.patch changes unit tests of the Python SDK (sdks/python,
#   python_requires >=3.10, CI python_versions 3.11-3.14). The SDK is
#   installed editable with its `yaml` extra into python:3.12 and the touched
#   test files are run with pytest.
_JDK_IMAGE = "eclipse-temurin:21-jdk-noble"
_PY_IMAGE = "python:3.12-bookworm"

_GRADLE_FLAGS = (
    "-PenableCheckerFramework=false -PdisableSpotlessCheck=true "
    "-PdisableCheckStyle=true --init-script /home/beam_init.gradle"
)

# ~/.gradle/gradle.properties overrides the project's gradle.properties: keeps
# the project's -Xss10240k, adds a heap limit, turns off the build cache so
# test tasks always execute, and caps workers.
GRADLE_PROPERTIES = """org.gradle.java.installations.auto-download=false
org.gradle.java.installations.auto-detect=true
org.gradle.daemon=false
org.gradle.caching=false
org.gradle.parallel=true
org.gradle.workers.max=3
org.gradle.console=plain
org.gradle.vfs.watch=false
org.gradle.jvmargs=-Xmx4g -Xss10240k -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8
"""

# Beam's Repositories.groovy declares only the in-tree offline-repository when
# Gradle runs with --offline, so offline stage runs could not see the
# dependencies cached from Maven Central. This init script (used by prepare.sh
# and every stage) declares the same remote repositories Beam uses online, so
# --offline resolves from the Gradle cache filled at build time.
INIT_GRADLE = """allprojects {
    repositories {
        mavenCentral()
        maven {
            url "https://packages.confluent.io/maven/"
            content { includeGroup "io.confluent" }
        }
        maven { url "https://repository.apache.org/content/repositories/releases" }
    }
}
"""

_ENV = """export CI=true
export NO_COLOR=1
export TERM=dumb
export GRADLE_OPTS="-Xmx512m -Dfile.encoding=UTF-8\""""

_JAVA_TEST_RE = re.compile(r"^(sdks/java/\S+?)/src/test/java/\S+\.java$")
_PY_TEST_RE = re.compile(
    r"^sdks/python/apache_beam/\S*(?:/test_[^/]+|_test)\.py$"
)


def _patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for a, b in re.findall(r"^diff --git a/(\S+) b/(\S+)", patch, flags=re.M):
        for p in (a, b):
            if p not in paths:
                paths.append(p)
    return paths


def _kind(pr: PullRequest) -> str:
    paths = _patch_paths(pr.test_patch)
    if any(_JAVA_TEST_RE.match(p) and not p.endswith("IT.java") for p in paths):
        return "java"
    if any(_PY_TEST_RE.match(p) and not re.search(r"_it_test\.py$", p) for p in paths):
        return "python"
    return "java"


# Java helper: decides the Gradle projects to test from test.patch, finds test
# sources that no longer compile, lists the @Test methods test.patch adds in
# them, and turns Gradle JUnit XML into "BEAM_TEST <STATUS> <id>" lines.
JAVA_TESTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
# javac / error-prone: "/home/beam/m/src/test/java/a/B.java:12: error: ..."
BROKEN = re.compile(r"(/\S+?/src/test/java/\S+?\.java):\d+: error:")
METHOD = re.compile(r"^([^(\[]+)\([^)]*\)(.*)$")
JAVA_TEST = re.compile(
    r"@(?:Test|ParameterizedTest)\b(?:\s*\([^)]*\))?\s+(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final)\s+)*void\s+(\w+)\s*\("
)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def patch_paths(patch_file):
    paths = []
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                for p in m.groups():
                    if p not in paths:
                        paths.append(p)
    return paths


def module_of(path):
    m = re.match(r"^(sdks/java/\S+?)/src/test/java/\S+\.java$", path)
    if not m:
        return None
    mod = m.group(1)
    if not os.path.isfile(os.path.join(REPO, mod, "build.gradle")):
        return None
    return mod


def modules(patch_file):
    mods = set()
    for p in patch_paths(patch_file):
        if p.endswith("IT.java"):
            continue
        mod = module_of(p)
        if mod:
            mods.add(mod)
    print(" ".join(sorted(mods)))


def tasks(mods_and_task):
    task, _, mods = mods_and_task.partition(" ")
    print(" ".join(":" + m.replace("/", ":") + ":" + task for m in mods.split()))


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = BROKEN.search(line.strip())
            if m and m.group(1) not in seen and os.path.isfile(m.group(1)):
                seen.append(m.group(1))
    for f in seen:
        print(f)


def added_lines(patch_file, rel):
    out = []
    current = None
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                current = m.group(2)
                continue
            if current == rel and line.startswith("+") and not line.startswith("+++"):
                out.append(line[1:])
    return "".join(out)


def methods(src_file, patch_file="/home/test.patch"):
    """Tests that test.patch adds or rewrites in a test source that no longer
    compiles -> FAILED (they cannot run). Untouched tests of that file are
    left unreported rather than marked failed."""
    rel = os.path.relpath(src_file, REPO)
    mod = module_of(rel)
    if not mod:
        return
    with open(src_file, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    pkg = re.search(r"^\s*package\s+([\w.]+)", text, re.M)
    stem = os.path.splitext(os.path.basename(src_file))[0]
    cls = (pkg.group(1) + "." if pkg else "") + stem
    touched = set(re.findall(r"\bvoid\s+(\w+)\s*\(", added_lines(patch_file, rel)))
    for name in sorted(set(JAVA_TEST.findall(text)) & touched):
        print("BEAM_TEST FAILED " + mod + ":" + cls + "#" + name + " (test source does not compile)")


def method_name(name):
    name = name.strip()
    m = METHOD.match(name)
    if m:
        name = m.group(1) + m.group(2)
    return name


def junit(mods):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    for mod in mods.split():
        rdir = os.path.join(REPO, mod, "build", "test-results", "test")
        if not os.path.isdir(rdir):
            continue
        for f in sorted(os.listdir(rdir)):
            if not (f.startswith("TEST-") and f.endswith(".xml")):
                continue
            try:
                root = ET.parse(os.path.join(rdir, f)).getroot()
            except ET.ParseError:
                continue
            for tc in root.iter():
                if local(tc.tag) != "testcase":
                    continue
                cls = (tc.get("classname") or "").strip()
                name = method_name(tc.get("name") or "")
                if not cls or not name:
                    continue
                tid = re.sub(r"\s+", " ", mod + ":" + cls + "#" + name).strip()
                tags = {local(c.tag) for c in tc}
                if tags & {"failure", "error"}:
                    status = "FAILED"
                elif "skipped" in tags:
                    status = "SKIPPED"
                else:
                    status = "PASSED"
                if tid not in results or rank[status] > rank[results[tid]]:
                    results[tid] = status
    for tid in sorted(results):
        print("BEAM_TEST " + results[tid] + " " + tid)


if __name__ == "__main__":
    cmd, arg = sys.argv[1], " ".join(sys.argv[2:])
    {"modules": modules, "tasks": tasks, "broken": broken, "methods": methods,
     "junit": junit}[cmd](arg)
'''

# Java driver: compile the scoped projects' test sources (dropping any that
# test.patch left uncompilable, and reporting the tests it adds in them as
# FAILED), then run those projects' `test` tasks offline. Gradle's exit code
# is returned, never swallowed.
JAVA_TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/beam_tests.py
GRADLE="./gradlew --offline --no-daemon --console=plain [[FLAGS]]"
cd "$REPO"

MODS=$(python3 "$HELPER" modules "$1")
echo "BEAM_TEST_MODULES=$MODS"
test -n "$MODS"

compile_tests() {
    local i f tasks rc
    tasks=$(python3 "$HELPER" tasks testClasses $MODS)
    : > /tmp/beam_removed
    for i in $(seq 1 15); do
        rc=0
        $GRADLE --continue $tasks > /tmp/beam_compile.log 2>&1 || rc=$?
        grep -E 'error:|BUILD (SUCCESSFUL|FAILED)' /tmp/beam_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/beam_compile.log > /tmp/beam_broken
        if [ ! -s /tmp/beam_broken ]; then
            tail -n 80 /tmp/beam_compile.log
            echo "BEAM_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> /tmp/beam_removed
            rm -f "$f"
        done < /tmp/beam_broken
    done
    echo "BEAM_COMPILE_FAILED (too many rounds)"
    return 1
}

run_tests() {
    local tasks m rc=0
    for m in $MODS; do rm -rf "$REPO/$m/build/test-results/test"; done
    compile_tests
    tasks=$(python3 "$HELPER" tasks test $MODS)
    $GRADLE --continue $tasks --rerun > /tmp/beam_test.log 2>&1 || rc=$?
    grep -E 'FAILED|BUILD (SUCCESSFUL|FAILED)|tests completed' /tmp/beam_test.log | tail -n 200 || true
    cat /tmp/beam_removed
    python3 "$HELPER" junit "$MODS"
    echo "BEAM_GRADLE_EXIT=$rc"
    return "$rc"
}

run_tests
'''

# Python driver: run the Python SDK test files test.patch touches (those
# present in the tree) with pytest -v from sdks/python. pytest's exit code is
# returned, never swallowed.
PY_TESTS_SH = r'''#!/bin/bash
set -eo pipefail
cd /home/[[REPO]]/sdks/python
FILES=""
for f in $(grep -oE '^diff --git a/sdks/python/\S+ b/sdks/python/\S+' "$1" | sed -E 's#^diff --git a/sdks/python/(\S+) b/.*#\1#' | sort -u); do
    case "$f" in
        *_it_test.py) continue ;;
        apache_beam/*_test.py|apache_beam/*/test_*.py) ;;
        *) continue ;;
    esac
    if [ -f "$f" ]; then FILES="$FILES $f"; fi
done
echo "BEAM_PY_TEST_FILES=$FILES"
test -n "$FILES"
python -m pytest -p no:cacheprovider -v -rA --timeout=900 $FILES
'''


def _apply_flags(pr: PullRequest) -> str:
    skipped: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for block in re.split(r"^(?=diff --git )", patch, flags=re.M):
            head = re.match(r"diff --git a/(\S+) b/(\S+)", block)
            if (
                head
                and re.search(r"^Binary files .* differ$", block, re.M)
                and "GIT binary patch" not in block
            ):
                skipped.update(head.groups())
    flags = ["--whitespace=nowarn"] + [
        f"--exclude={shlex.quote(path)}" for path in sorted(skipped)
    ]
    return " ".join(flags)


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


class BeamImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _JDK_IMAGE if _kind(self.pr) == "java" else _PY_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Per-PR base. The repo's full history is very large, so only the base
        # commit is fetched (depth 1); the pin and Image._HARDENING_BLOCK
        # (history scrub + assertions) follow. The enhancer adds the
        # REPO_URL/BASE_COMMIT ARGs, proxy/CA setup and labels; since the
        # hardening marker already follows the fetch, it is not re-injected.
        repo = self.pr.repo
        packages = "ca-certificates curl git python3 unzip"
        return f"""FROM {self.dependency()}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends {packages} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*' \\
    && git init -q /home/{repo} \\
    && cd /home/{repo} \\
    && git fetch --depth 1 "${{REPO_URL}}" "${{BASE_COMMIT}}" \\
    && git checkout --detach FETCH_HEAD

WORKDIR /home/{repo}

ENV CI=true

{Image._HARDENING_BLOCK}

CMD ["/bin/bash"]
"""


class BeamImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return BeamImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _java_prepare(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""#!/bin/bash
set -euo pipefail

# Section 1: pin
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Section 2: provision
{_ENV}
mkdir -p /root/.gradle
cp /home/gradle.properties /root/.gradle/gradle.properties

MODS=$(python3 /home/beam_tests.py modules /home/test.patch)
echo "BEAM_TEST_MODULES=$MODS"
test -n "$MODS"
CLASSES=$(python3 /home/beam_tests.py tasks testClasses $MODS)
TESTS=$(python3 /home/beam_tests.py tasks test $MODS)

# Download the Gradle distribution (./gradlew), buildSrc plugins and every
# compile/test dependency, and compile the scoped projects' tests, with
# retries for transient network failures.
ok=0
for attempt in 1 2 3; do
  if ./gradlew --no-daemon --console=plain {_GRADLE_FLAGS} $CLASSES; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1

# Resolve run-time-only test dependencies by running the scoped test tasks
# once at the base commit. A failing base test is tolerated here (the stages
# report it); the gate below requires that the test JVM actually started and
# wrote JUnit XML for every project.
./gradlew --no-daemon --console=plain {_GRADLE_FLAGS} --continue $TESTS || true

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
./gradlew --offline --no-daemon --console=plain -q {_GRADLE_FLAGS} $CLASSES
for m in $MODS; do
  test -d $m/build/classes/java/test
  ls $m/build/test-results/test/TEST-*.xml > /dev/null
  rm -rf $m/build/test-results $m/build/reports
done
echo DEPS_OK
"""

    def _python_prepare(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""#!/bin/bash
set -euo pipefail

# Section 1: pin
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Section 2: provision
{_ENV}
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
cd /home/{repo}/sdks/python
python -m pip install --upgrade pip
# Editable install: builds the generated protos and Cython extensions in place
# (all git-ignored), plus the yaml extra and the unit-test tools.
python -m pip install -e '.[yaml]' \\
    'pytest>=7.1.2,<10.0' 'pytest-timeout>=2.1.0,<3' 'mock>=1.0.1,<6.0.0' \\
    'pyhamcrest>=1.9,!=1.10.0,<3.0.0' 'parameterized>=0.7.1,<0.10.0' \\
    'freezegun>=0.3.12' 'requests_mock>=1.7,<2.0'

# Section 3: gate
cd /home/{repo}
test "$(git rev-parse HEAD)" = "{sha}"
cd /home/{repo}/sdks/python
python -c "import apache_beam, apache_beam.yaml.yaml_provider, apache_beam.portability.api; print(apache_beam.__version__)"
python -m pytest --version
python -c "import pytest_timeout, mock, hamcrest, parameterized"
echo DEPS_OK
"""

    def files(self) -> list[File]:
        repo = self.pr.repo
        apply = _apply_flags(self.pr)
        kind = _kind(self.pr)
        files = [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
        ]
        if kind == "java":
            files += [
                File(".", "gradle.properties", GRADLE_PROPERTIES),
                File(".", "beam_init.gradle", INIT_GRADLE),
                File(".", "beam_tests.py", JAVA_TESTS_PY.replace("[[REPO]]", repo)),
                File(
                    ".",
                    "beam_tests.sh",
                    JAVA_TESTS_SH.replace("[[REPO]]", repo).replace(
                        "[[FLAGS]]", _GRADLE_FLAGS
                    ),
                ),
                File(".", "prepare.sh", self._java_prepare()),
            ]
        else:
            files += [
                File(".", "beam_tests.sh", PY_TESTS_SH.replace("[[REPO]]", repo)),
                File(".", "prepare.sh", self._python_prepare()),
            ]
        files += [
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
bash /home/beam_tests.sh /home/test.patch
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply {apply} /home/test.patch
bash /home/beam_tests.sh /home/test.patch
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply {apply} /home/test.patch /home/fix.patch
bash /home/beam_tests.sh /home/test.patch
""",
            ),
        ]
        return files

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh
"""


_JAVA_RESULT_RE = re.compile(
    r"^BEAM_TEST (PASSED|FAILED|SKIPPED) (\S+?:\S.*?#.+?)"
    r"(?: \(test source does not compile\))?\s*$"
)
_PY_VERBOSE_RE = re.compile(
    r"^(apache_beam/\S+?\.py::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_PY_SUMMARY_RE = re.compile(
    r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(apache_beam/\S+?\.py::\S+)"
)


@Instance.register("apache", "beam")
class BEAM(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BeamImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        buckets = {
            "PASSED": passed_tests,
            "XPASS": passed_tests,
            "FAILED": failed_tests,
            "ERROR": failed_tests,
            "SKIPPED": skipped_tests,
            "XFAIL": skipped_tests,
        }
        for raw in log.splitlines():
            line = raw.strip()
            m = _JAVA_RESULT_RE.match(line)
            if m:
                buckets[m.group(1)].add(m.group(2))
                continue
            m = _PY_VERBOSE_RE.match(line)
            if m:
                buckets[m.group(2)].add(m.group(1))
                continue
            m = _PY_SUMMARY_RE.match(line)
            if m:
                buckets[m.group(1)].add(m.group(2))

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
