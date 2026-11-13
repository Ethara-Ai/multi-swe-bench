import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# pom.xml at the PR base sets java.version=21 (maven.compiler.source/target);
# CI (.github/workflows/pullrequest.yml) builds on JDK 21 and 25. JDK 21 is the
# declared baseline. The official image is multi-arch (amd64 + arm64). Maven
# comes from the repo's own wrapper (./mvnw, "only-script", Maven 3.9.16, a
# platform-independent zip).
_JDK_IMAGE = "eclipse-temurin:21-jdk-noble"

_ENV = """export CI=true
export NO_COLOR=1
export MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"
export MAVEN_ARGS="-B -ntp -nsu -Dstyle.color=never -Djacoco.skip=true"
export TESTCONTAINERS_RYUK_DISABLED=true"""

# Helper. Multi-module Maven build: the scope is derived from test.patch.
#   modules  -> Maven modules (nearest pom.xml) holding the test sources that
#               test.patch touches; built with `-pl <modules> -am`.
#   classes  -> the test classes test.patch touches (including in-memory *IT
#               classes) plus the concrete surefire-pattern test classes in
#               the same packages (pass-to-pass scope). Classes that need
#               Docker / Testcontainers / Axon Server are left out.
#   broken   -> test sources that javac rejected (to be removed).
#   methods  -> @Test methods test.patch adds/rewrites in a removed source,
#               reported FAILED under the surefire name (nested classes as
#               Outer$Inner).
#   junit    -> surefire XML -> "AXON_TEST <STATUS> <module>:<class>#<method>".
TESTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
TEST_DIR = "src/test/java/"
INCLUDE = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase)\.java$")
TOUCHED = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase|\w*IT)\.java$")
NEEDS_DOCKER = re.compile(
    r"org\.testcontainers|@Testcontainers|AxonServerContainer|DockerClientFactory"
)
BROKEN = re.compile(r"^\[ERROR\] (/\S+?/src/test/java/\S+?\.java):\[\d+")
METHOD = re.compile(r"^([^(\[]+)\([^)]*\)(.*)$")
TEST_METHOD = re.compile(
    r"@Test\b(?:\s*\([^)]*\))?\s+(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final)\s+)*void\s+(\w+)\s*\("
)
CLASS_DECL = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_]\w*)")


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


def module_of(rel):
    """Nearest directory (relative to the repo root) holding a pom.xml."""
    d = os.path.dirname(rel)
    while d:
        if os.path.isfile(os.path.join(REPO, d, "pom.xml")):
            return d
        d = os.path.dirname(d)
    return "."


def test_sources(patch_file):
    out = []
    for p in sorted(patch_paths(patch_file)):
        if "/" + TEST_DIR in "/" + p and p.endswith(".java"):
            mod, _, rest = ("/" + p).partition("/" + TEST_DIR)
            out.append((mod.strip("/") or ".", rest))
    return out


def modules(patch_file):
    mods = set()
    for mod, rest in test_sources(patch_file):
        mods.add(module_of(os.path.join(mod, TEST_DIR, rest)))
    print(",".join(sorted(mods)))


def runnable(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    name = os.path.basename(path)[:-5]
    if re.search(r"\babstract\s+class\s+" + re.escape(name) + r"\b", text):
        return False
    return not NEEDS_DOCKER.search(text)


def classes(patch_file):
    out = set()
    for mod, rest in test_sources(patch_file):
        root = os.path.join(REPO, mod, TEST_DIR)
        pkg = os.path.dirname(rest)
        touched = os.path.join(root, rest)
        if TOUCHED.match(os.path.basename(rest)) and os.path.isfile(touched) and runnable(touched):
            out.add(rest[:-5].replace("/", "."))
        full = os.path.join(root, pkg)
        if not os.path.isdir(full):
            continue
        for f in sorted(os.listdir(full)):
            if INCLUDE.match(f) and runnable(os.path.join(full, f)):
                out.add(os.path.join(pkg, f[:-5]).replace("/", "."))
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


def strip_java(text):
    """Blank out comments and string/char literals, keeping offsets."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(" " * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def class_at(code, pos, outer):
    """Binary name (Outer$Inner) of the named class enclosing offset `pos`."""
    stack = []  # (depth, name or None)
    pending = None
    depth = 0
    for m in re.finditer(r"[{}]|" + CLASS_DECL.pattern, code[:pos]):
        tok = m.group(0)
        if tok == "{":
            depth += 1
            stack.append((depth, pending))
            pending = None
        elif tok == "}":
            if stack and stack[-1][0] == depth:
                stack.pop()
            depth -= 1
        else:
            pending = m.group(2)
    names = [name for _, name in stack if name]
    return "$".join(names) if names else outer


def methods(src_file, patch_file="/home/test.patch"):
    """Tests that test.patch adds or rewrites in a source file that no longer
    compiles -> FAILED (they cannot run). Untouched tests of that file are
    left unreported (NONE) rather than marked failed."""
    rel = os.path.relpath(src_file, REPO)
    if ("/" + TEST_DIR) not in ("/" + rel) or not runnable(src_file):
        return
    mod_dir, _, rest = ("/" + rel).partition("/" + TEST_DIR)
    module = module_of(rel)
    pkg = os.path.dirname(rest).replace("/", ".")
    outer = os.path.basename(rest)[:-5]
    with open(src_file, encoding="utf-8", errors="replace") as fh:
        code = strip_java(fh.read())
    touched = added_methods(patch_file, rel)
    seen = set()
    for m in TEST_METHOD.finditer(code):
        name = m.group(1)
        if name not in touched:
            continue
        cls = class_at(code, m.start(), outer)
        tid = module + ":" + (pkg + "." if pkg else "") + cls + "#" + name
        if tid not in seen:
            seen.add(tid)
            print("AXON_TEST FAILED " + tid + " (test source does not compile)")


def method_name(name):
    name = name.strip()
    m = METHOD.match(name)
    if m:
        name = m.group(1) + m.group(2)
    return name


def junit(mods):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    for module in [m for m in mods.split(",") if m]:
        rdir = os.path.join(REPO, module, "target", "surefire-reports")
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
                tid = re.sub(r"\s+", " ", module + ":" + cls + "#" + name).strip()
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
        print("AXON_TEST " + results[tid] + " " + tid)


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {
        "modules": modules,
        "classes": classes,
        "broken": broken,
        "methods": methods,
        "junit": junit,
    }[cmd](arg)
'''

# Driver: compile the scoped modules (and their upstream reactor modules, so a
# fix in e.g. messaging is picked up), drop test sources that test.patch left
# uncompilable (reporting their new tests as FAILED), then run the scoped
# surefire suite. The exit code is Maven's, never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/axon_tests.py
cd "$REPO"

MODULES=$(python3 "$HELPER" modules "$1")
echo "AXON_TEST_MODULES=$MODULES"
test -n "$MODULES"

compile_tests() {
    local i f rc
    : > /tmp/axon_removed
    for i in $(seq 1 10); do
        rc=0
        ./mvnw test-compile -pl "$MODULES" -am > /tmp/axon_compile.log 2>&1 || rc=$?
        grep -E '^\[ERROR\]|BUILD (SUCCESS|FAILURE)' /tmp/axon_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/axon_compile.log > /tmp/axon_broken
        if [ ! -s /tmp/axon_broken ]; then
            tail -n 80 /tmp/axon_compile.log
            echo "AXON_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> /tmp/axon_removed
            rm -f "$f"
        done < /tmp/axon_broken
    done
    echo "AXON_COMPILE_FAILED (too many rounds)"
    return 1
}

run_tests() {
    local classes m rc=0
    for m in ${MODULES//,/ }; do
        rm -rf "$REPO/$m/target/surefire-reports"
    done
    compile_tests
    classes=$(python3 "$HELPER" classes "$1")
    echo "AXON_TEST_CLASSES=$classes"
    test -n "$classes"
    ./mvnw test -pl "$MODULES" -am -Dtest="$classes" -Dsurefire.failIfNoSpecifiedTests=false \
        > /tmp/axon_test.log 2>&1 || rc=$?
    grep -E 'Tests run:|FAIL|BUILD (SUCCESS|FAILURE)' /tmp/axon_test.log | tail -n 200 || true
    if [ "$rc" != 0 ] && ! grep -q 'There are test failures\|There are test errors\|Tests run:.*Failures' /tmp/axon_test.log; then
        tail -n 80 /tmp/axon_test.log
    fi
    cat /tmp/axon_removed
    python3 "$HELPER" junit "$MODULES"
    echo "AXON_SUREFIRE_EXIT=$rc"
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


class AxonFrameworkImageBase(Image):
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
            '    MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"\n'
            "RUN git config --global --add safe.directory '*' && java -version"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. Per-PR base, so the pin and
    # the history scrub belong here.


class AxonFrameworkImageDefault(Image):
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
        return AxonFrameworkImageBase(self.pr, self._config)

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
            File(".", "axon_tests.py", tests_py),
            File(".", "axon_tests.sh", tests_sh),
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

modules=$(python3 /home/axon_tests.py modules /home/test.patch)
echo "modules=$modules"
test -n "$modules"

# Resolve the Maven distribution (./mvnw) and every build/test dependency of
# the scoped modules and their upstream reactor modules, compiling main and
# test sources at the base commit. Retries cover transient network failures.
ok=0
for attempt in 1 2 3; do
  if ./mvnw test-compile -pl "$modules" -am; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1

# Resolve the surefire JUnit Platform provider (and warm the forked JVM's
# classpath) by running the base-commit test scope once. Test failures at the
# base are not a provisioning error, so they are ignored by Maven itself; any
# other build failure still aborts.
classes=$(python3 /home/axon_tests.py classes /home/test.patch)
if [ -n "$classes" ]; then
  ./mvnw test -pl "$modules" -am -Dtest="$classes" \\
    -Dsurefire.failIfNoSpecifiedTests=false -Dmaven.test.failure.ignore=true
fi
for m in ${{modules//,/ }}; do
  rm -rf "$m/target/surefire-reports"
done

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
./mvnw -o test-compile -pl "$modules" -am -q
for m in ${{modules//,/ }}; do
  test -d "$m/target/test-classes"
done
ls -d ~/.m2/repository/org/apache/maven/surefire/surefire-junit-platform/*/ > /dev/null
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
bash /home/axon_tests.sh /home/test.patch
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
bash /home/axon_tests.sh /home/test.patch
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
bash /home/axon_tests.sh /home/test.patch
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


@Instance.register("AxonIQ", "AxonFramework")
class AXON_FRAMEWORK(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AxonFrameworkImageDefault(self.pr, self._config)

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
            r"^AXON_TEST (PASSED|FAILED|SKIPPED) (\S+#\S+?)(?: \(test source does not compile\))?\s*$"
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
