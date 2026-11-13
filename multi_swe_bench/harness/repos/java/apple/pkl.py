import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# build-logic/src/main/kotlin/BuildInfo.kt at the PR base: PKL_JDK_VERSION_MIN = 25,
# toolchain vendor ADOPTIUM; .github/workflows use temurin 25. The official
# eclipse-temurin image is multi-arch (amd64 + arm64) and is itself an
# Adoptium JDK 25, so Gradle's toolchain resolution is satisfied by the
# current JVM and never needs to download one (auto-download is disabled).
# Gradle comes from the repo's wrapper (a platform-independent zip).
_JDK_IMAGE = "eclipse-temurin:25-jdk-noble"

# ~/.gradle/gradle.properties overrides the project's gradle.properties. It
# keeps the project's jdk.compiler exports, adds a heap limit, and turns off
# the build cache so test tasks always execute instead of being restored.
GRADLE_PROPERTIES = """org.gradle.java.installations.auto-download=false
org.gradle.java.installations.auto-detect=true
org.gradle.daemon=false
org.gradle.caching=false
org.gradle.parallel=true
org.gradle.workers.max=4
org.gradle.console=plain
org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=1g -XX:+UseParallelGC -Dfile.encoding=UTF-8 \\
  --add-exports jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED \\
  --add-exports jdk.compiler/com.sun.tools.javac.file=ALL-UNNAMED \\
  --add-exports jdk.compiler/com.sun.tools.javac.parser=ALL-UNNAMED \\
  --add-exports jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED \\
  --add-exports jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED
kotlin.daemon.jvmargs=-Xmx2g -XX:+UseParallelGC
"""

# Init script for the stage runs: Gradle's JUnit XML names a parameterized
# invocation only "[i] args", so invocations of different methods in one class
# collide (e.g. GlobResolverTest). Prefixing the method's display name keeps
# every test id unique and stable across stages. Changes reporting only.
INIT_GRADLE = """allprojects {
    tasks.withType(Test).configureEach {
        systemProperty 'junit.jupiter.params.displayname.default', '{displayName} [{index}] {arguments}'
    }
}
"""

_ENV = """export CI=true
export NO_COLOR=1
export TERM=dumb
export GRADLE_OPTS="-Xmx512m -Dfile.encoding=UTF-8\""""

# Helper: decides the Gradle modules to test from test.patch, finds test
# sources that no longer compile, lists the @Test functions test.patch adds in
# them, and turns Gradle JUnit XML into "PKL_TEST <STATUS> <id>" lines.
TESTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
# javac: "/home/pkl/m/src/test/java/a/B.java:12: error: ..."
# kotlinc: "e: file:///home/pkl/m/src/test/kotlin/a/B.kt:12:5 ..."
BROKEN = [
    re.compile(r"(/\S+?/src/test/(?:java|kotlin)/\S+?\.(?:java|kt)):\d+: error:"),
    re.compile(r"^e: (?:file://)?(/\S+?/src/test/(?:java|kotlin)/\S+?\.(?:java|kt)):\d+"),
]
METHOD = re.compile(r"^([^(\[]+)\([^)]*\)(.*)$")
KT_TEST = re.compile(
    r"@(?:Test|ParameterizedTest)\b(?:\s*\([^)]*\))?\s+(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|internal|private|open|override)\s+)*fun\s+(`[^`]+`|\w+)\s*\("
)
JAVA_TEST = re.compile(
    r"@(?:Test|ParameterizedTest)\b(?:\s*\([^)]*\))?\s+(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final)\s+)*void\s+(\w+)\s*\("
)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def patch_paths(patch_file):
    paths = set()
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                paths.update(m.groups())
    return paths


def module_of(path):
    if "/src/test/" not in path:
        return None
    mod = path[: path.index("/src/test/")]
    if "/" in mod or not mod:
        return None
    if not os.path.isfile(os.path.join(REPO, mod, mod + ".gradle.kts")):
        return None
    return mod


def modules(patch_file):
    mods = {m for m in map(module_of, patch_paths(patch_file)) if m}
    print(" ".join(sorted(mods)))


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            for rx in BROKEN:
                m = rx.search(line)
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


def fun_names(text):
    return {n.strip("`") for n in KT_TEST.findall(text)} | set(JAVA_TEST.findall(text))


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
    added = added_lines(patch_file, rel)
    touched = set(re.findall(r"\bfun\s+`([^`]+)`", added))
    touched |= set(re.findall(r"\bfun\s+(\w+)\s*\(", added))
    touched |= set(re.findall(r"\bvoid\s+(\w+)\s*\(", added))
    for name in sorted(fun_names(text) & touched):
        print("PKL_TEST FAILED " + mod + ":" + cls + "#" + name + " (test source does not compile)")


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
        print("PKL_TEST " + results[tid] + " " + tid)


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {"modules": modules, "broken": broken, "methods": methods, "junit": junit}[cmd](arg)
'''

# Driver: compile the scoped modules' test sources (dropping any that
# test.patch left uncompilable, and reporting the tests it adds in them as
# FAILED), then run those modules' JVM unit test tasks offline. The exit code
# is Gradle's, never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/pkl_tests.py
GRADLE="./gradlew --offline --no-daemon --console=plain --init-script /home/pkl_init.gradle"
cd "$REPO"

MODS=$(python3 "$HELPER" modules "$1")
echo "PKL_TEST_MODULES=$MODS"
test -n "$MODS"

compile_tests() {
    local i f tasks="" m
    for m in $MODS; do tasks="$tasks :$m:testClasses"; done
    : > /tmp/pkl_removed
    for i in $(seq 1 10); do
        rc=0
        $GRADLE --continue $tasks > /tmp/pkl_compile.log 2>&1 || rc=$?
        grep -E '^e: |error:|BUILD (SUCCESSFUL|FAILED)' /tmp/pkl_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/pkl_compile.log > /tmp/pkl_broken
        if [ ! -s /tmp/pkl_broken ]; then
            tail -n 80 /tmp/pkl_compile.log
            echo "PKL_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> /tmp/pkl_removed
            rm -f "$f"
        done < /tmp/pkl_broken
    done
    echo "PKL_COMPILE_FAILED (too many rounds)"
    return 1
}

run_tests() {
    local tasks="" m rc=0
    for m in $MODS; do
        rm -rf "$REPO/$m/build/test-results/test"
        tasks="$tasks :$m:test"
    done
    compile_tests
    $GRADLE --continue $tasks --rerun > /tmp/pkl_test.log 2>&1 || rc=$?
    grep -E 'FAILED|BUILD (SUCCESSFUL|FAILED)|tests completed' /tmp/pkl_test.log | tail -n 200 || true
    cat /tmp/pkl_removed
    python3 "$HELPER" junit "$MODS"
    echo "PKL_GRADLE_EXIT=$rc"
    return "$rc"
}

run_tests
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


class PklImageBase(Image):
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
        return _JDK_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["unzip"]

    def extra_setup(self) -> str:
        return (
            "ENV CI=true\n"
            "RUN git config --global --add safe.directory '*' && java -version"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. Per-PR base, so the pin and
    # the history scrub belong here.


class PklImageDefault(Image):
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
        return PklImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        apply = _apply_flags(self.pr)
        tests_py = TESTS_PY.replace("[[REPO]]", repo)
        tests_sh = TESTS_SH.replace("[[REPO]]", repo)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
            File(".", "gradle.properties", GRADLE_PROPERTIES),
            File(".", "pkl_init.gradle", INIT_GRADLE),
            File(".", "pkl_tests.py", tests_py),
            File(".", "pkl_tests.sh", tests_sh),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
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

MODS=$(python3 /home/pkl_tests.py modules /home/test.patch)
echo "PKL_TEST_MODULES=$MODS"
test -n "$MODS"
tasks=""
for m in $MODS; do tasks="$tasks :$m:testClasses"; done

# Download the Gradle distribution (./gradlew), the build-logic plugins and
# every compile/test dependency, and compile the scoped modules' tests, with
# retries for transient network failures.
ok=0
for attempt in 1 2 3; do
  if ./gradlew --no-daemon $tasks; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1

# Resolve the JUnit Platform launcher and run-time-only test dependencies
# by running the scoped test tasks once at the base commit. A failing base
# test is tolerated here (the stages report it); the gate below requires
# that the test JVM actually started and wrote JUnit XML for every module.
for m in $MODS; do
  ./gradlew --no-daemon --init-script /home/pkl_init.gradle :$m:test --continue || true
done

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
./gradlew --offline --no-daemon -q $tasks
for m in $MODS; do
  test -d $m/build/classes
  ls $m/build/test-results/test/TEST-*.xml > /dev/null
  rm -rf $m/build/test-results $m/build/reports
done
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
bash /home/pkl_tests.sh /home/test.patch
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
bash /home/pkl_tests.sh /home/test.patch
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
bash /home/pkl_tests.sh /home/test.patch
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("apple", "pkl")
class PKL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PklImageDefault(self.pr, self._config)

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
        result_re = re.compile(
            r"^PKL_TEST (PASSED|FAILED|SKIPPED) (\S+?:\S.*?#.+?)"
            r"(?: \(test source does not compile\))?\s*$"
        )
        buckets = {
            "PASSED": passed_tests,
            "FAILED": failed_tests,
            "SKIPPED": skipped_tests,
        }
        for raw in log.splitlines():
            found = result_re.match(raw.strip())
            if found:
                buckets[found.group(1)].add(found.group(2))

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
