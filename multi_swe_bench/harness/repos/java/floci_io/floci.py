import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# pom.xml at the PR bases sets maven.compiler.release=25 and an enforcer rule
# requireJavaVersion [25,26); CI (.github/workflows/ci.yml) uses temurin 25.
# The official image is multi-arch (amd64 + arm64). Maven comes from the
# repo's own wrapper (./mvnw, "only-script", a platform-independent zip).
_JDK_IMAGE = "eclipse-temurin:25-jdk-noble"

_ENV = """export CI=true
export NO_COLOR=1
export MAVEN_OPTS="-Xmx2g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"
export MAVEN_ARGS="-B -ntp -Dstyle.color=never -Dcheckstyle.skip=true"
export TESTCONTAINERS_RYUK_DISABLED=true"""

# Helper: decides the test scope from test.patch, finds test sources that no
# longer compile, lists their @Test methods, and turns surefire XML reports
# into "FLOCI_TEST <STATUS> <class>#<method>" lines for parse_log.
TESTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
TEST_ROOT = "src/test/java/"
INCLUDE = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase)\.java$")
BROKEN = re.compile(r"^\[ERROR\] (/\S+?/src/test/java/\S+?\.java):\[\d+")
METHOD = re.compile(r"^([^(\[]+)\([^)]*\)(.*)$")
TEST_METHOD = re.compile(
    r"@Test\b(?:\s*\([^)]*\))?\s+(?:@\w+(?:\([^)]*\))?\s+)*"
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


def packages(patch_file):
    """Package dirs of the root module's test sources that test.patch touches."""
    dirs = set()
    for p in patch_paths(patch_file):
        if p.startswith(TEST_ROOT) and p.endswith(".java"):
            dirs.add(os.path.dirname(p))
    return sorted(dirs)


def classes(patch_file):
    """Concrete test classes in those packages (pass-to-pass scope)."""
    out = set()
    for d in packages(patch_file):
        full = os.path.join(REPO, d)
        if not os.path.isdir(full):
            continue
        for f in sorted(os.listdir(full)):
            if not INCLUDE.match(f):
                continue
            with open(os.path.join(full, f), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if re.search(r"\babstract\s+class\s+" + re.escape(f[:-5]) + r"\b", text):
                continue
            out.add((d[len(TEST_ROOT):] + "/" + f[:-5]).replace("/", "."))
    print(",".join(sorted(out)))


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = BROKEN.search(line.strip())
            if m and m.group(1) not in seen and os.path.isfile(m.group(1)):
                seen.append(m.group(1))
    for f in seen:
        print(f)


def added_methods(patch_file, rel):
    """Method names declared on lines test.patch adds to file `rel`."""
    names = set()
    current = None
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                current = m.group(2)
                continue
            if current == rel and line.startswith("+") and not line.startswith("+++"):
                names.update(re.findall(r"\bvoid\s+(\w+)\s*\(", line))
    return names


def methods(src_file, patch_file="/home/test.patch"):
    """Tests that test.patch adds or rewrites in a source file that no longer
    compiles -> FAILED (they cannot run). Untouched tests of that file are
    left unreported (NONE) rather than marked failed."""
    rel = os.path.relpath(src_file, REPO)
    if not rel.startswith(TEST_ROOT):
        return
    cls = rel[len(TEST_ROOT):-5].replace("/", ".")
    with open(src_file, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    touched = added_methods(patch_file, rel)
    for name in sorted(set(TEST_METHOD.findall(text)) & touched):
        print("FLOCI_TEST FAILED " + cls + "#" + name + " (test source does not compile)")


def method_name(name):
    name = name.strip()
    m = METHOD.match(name)
    if m:
        name = m.group(1) + m.group(2)
    return name


def junit(_arg):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    rdir = os.path.join(REPO, "target", "surefire-reports")
    if os.path.isdir(rdir):
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
                tid = re.sub(r"\s+", " ", cls + "#" + name).strip()
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
        print("FLOCI_TEST " + results[tid] + " " + tid)


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {"classes": classes, "broken": broken, "methods": methods, "junit": junit}[cmd](arg)
'''

# Driver: compile the test sources (dropping any that test.patch left
# uncompilable, and reporting their tests as FAILED), then run the scoped
# surefire suite. The exit code is surefire's, never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/floci_tests.py
cd "$REPO"

compile_tests() {
    local i f
    : > /tmp/floci_removed
    for i in $(seq 1 10); do
        rc=0
        ./mvnw test-compile > /tmp/floci_compile.log 2>&1 || rc=$?
        grep -E '^\[ERROR\]|BUILD (SUCCESS|FAILURE)' /tmp/floci_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/floci_compile.log > /tmp/floci_broken
        if [ ! -s /tmp/floci_broken ]; then
            tail -n 80 /tmp/floci_compile.log
            echo "FLOCI_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> /tmp/floci_removed
            rm -f "$f"
        done < /tmp/floci_broken
    done
    echo "FLOCI_COMPILE_FAILED (too many rounds)"
    return 1
}

run_tests() {
    local classes rc=0
    rm -rf "$REPO/target/surefire-reports"
    compile_tests
    classes=$(python3 "$HELPER" classes "$1")
    echo "FLOCI_TEST_CLASSES=$classes"
    test -n "$classes"
    ./mvnw surefire:test -Dtest="$classes" -Dsurefire.failIfNoSpecifiedTests=false \
        > /tmp/floci_test.log 2>&1 || rc=$?
    grep -E 'Tests run:|FAIL|BUILD (SUCCESS|FAILURE)' /tmp/floci_test.log | tail -n 200 || true
    cat /tmp/floci_removed
    python3 "$HELPER" junit
    echo "FLOCI_SUREFIRE_EXIT=$rc"
    return "$rc"
}

run_tests "$1"
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


class FlociImageBase(Image):
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
            "ENV CI=true \\\n"
            '    MAVEN_OPTS="-Xmx2g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"\n'
            "RUN git config --global --add safe.directory '*' && java -version"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. Per-PR base, so the pin and
    # the history scrub belong here.


class FlociImageDefault(Image):
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
        return FlociImageBase(self.pr, self._config)

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
            File(".", "floci_tests.py", tests_py),
            File(".", "floci_tests.sh", tests_sh),
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

# Resolve the Maven distribution (./mvnw), all build/test dependencies and the
# quarkus code generation, with retries for transient network failures.
ok=0
for attempt in 1 2 3; do
  if ./mvnw test-compile; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1

# Resolve the surefire JUnit Platform provider by running one unit test class
# of the PR's scope at the base commit.
first=$(python3 /home/floci_tests.py classes /home/test.patch | tr ',' '\\n' | awk '!/IntegrationTest$/' | head -n 1)
if [ -n "$first" ]; then
  ./mvnw surefire:test -Dtest="$first" -Dsurefire.failIfNoSpecifiedTests=false
fi
rm -rf target/surefire-reports

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
./mvnw -o test-compile -q
test -d target/test-classes/io
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
bash /home/floci_tests.sh /home/test.patch
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
bash /home/floci_tests.sh /home/test.patch
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
bash /home/floci_tests.sh /home/test.patch
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


@Instance.register("floci-io", "floci")
class FLOCI(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FlociImageDefault(self.pr, self._config)

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
            r"^FLOCI_TEST (PASSED|FAILED|SKIPPED) (\S+#\S+?)(?: \(test source does not compile\))?\s*$"
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
