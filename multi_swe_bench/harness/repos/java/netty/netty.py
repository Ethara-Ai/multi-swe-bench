import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# netty 5.0 (base bc9a8f07 of #17239): the root pom's java25/java26 profiles
# set maven.compiler.release=25 and every CI job (.github/workflows/ci-pr.yml)
# builds on JDK 25, so the toolchain is Temurin 25 (multi-arch amd64+arm64).
# JDK 25 also matters for the test itself: PlatformDependent0 sets
# io.netty.noUnsafe=true by default on Java 25+, which is the "Unsafe
# unavailable" path the new test asserts on.
# Maven comes from the repo's own wrapper (./mvnw 3.2.0 script with a tracked
# .mvn/wrapper/maven-wrapper.jar; Maven 3.9.11 bin zip, platform independent).
# The PR touches only netty5-common (pure Java), so no native transport
# (epoll/kqueue/io_uring) or tcnative module is built: `-pl common -am` builds
# the parent, dev-tools and common.
_JDK_IMAGE = "eclipse-temurin:25-jdk-noble"

_ENV = """export CI=true
export NO_COLOR=1
export MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"
export MAVEN_ARGS="-B -ntp -nsu -Dstyle.color=never -Djacoco.skip=true -Dcheckstyle.skip=true -Danimal.sniffer.skip=true -Djapicmp.skip=true -Drevapi.skip=true -Dforbiddenapis.skip=true -Denforcer.skip=true -Dxml.skip=true"
export TESTCONTAINERS_RYUK_DISABLED=true"""

# Artifacts that fix.patch adds to the build (pom.xml/common/pom.xml declare
# org.jctools:jctools-core-jdk11:${jctools.version}=4.0.6). prepare.sh only
# downloads them into ~/.m2 -- it never applies the patch -- so the fix stage
# can build offline (Harbor runs the verifier without network).
_EXTRA_ARTIFACTS = {
    17239: ["org.jctools:jctools-core-jdk11:4.0.6"],
}

# Helper. Multi-module Maven build: the scope is derived from test.patch.
#   modules  -> Maven modules (nearest pom.xml) holding the test sources that
#               test.patch touches; built with `-pl <modules> -am`.
#   classes  -> the surefire-pattern test classes test.patch touches plus the
#               concrete surefire-pattern test classes in the same packages
#               (pass-to-pass scope). The pattern mirrors netty's surefire
#               config (includes **/*Test*.java, excludes Abstract* and
#               *TestUtil*); Testcontainers/Docker tests are left out.
#   broken   -> test sources that javac rejected (to be removed).
#   methods  -> @Test methods test.patch adds/rewrites in a removed source,
#               reported FAILED under the surefire name (nested classes as
#               Outer$Inner).
#   junit    -> surefire XML -> "NETTY_TEST <STATUS> <module>:<class>#<method>".
TESTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
TEST_DIR = "src/test/java/"
INCLUDE = re.compile(r"^(?!Abstract)(?!\w*TestUtil)\w*Test\w*\.java$")
TOUCHED = INCLUDE
NEEDS_DOCKER = re.compile(
    r"org\.testcontainers|@Testcontainers|DockerClientFactory|GenericContainer"
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
    if NEEDS_DOCKER.search(text):
        return False
    return True


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
            print("NETTY_TEST FAILED " + tid + " (test source does not compile)")


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
        print("NETTY_TEST " + results[tid] + " " + tid)


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
# fix in an upstream module is picked up), drop test sources that test.patch left
# uncompilable (reporting their new tests as FAILED), then run the scoped
# surefire suite. Maven runs offline (-o): everything was resolved into
# ~/.m2 by prepare.sh. The exit code is Maven's, never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/netty_tests.py
cd "$REPO"

MODULES=$(python3 "$HELPER" modules "$1")
echo "NETTY_TEST_MODULES=$MODULES"
test -n "$MODULES"

compile_tests() {
    local i f rc
    : > /tmp/netty_removed
    for i in $(seq 1 10); do
        rc=0
        ./mvnw -o test-compile -pl "$MODULES" -am > /tmp/netty_compile.log 2>&1 || rc=$?
        grep -E '^\[ERROR\]|BUILD (SUCCESS|FAILURE)' /tmp/netty_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/netty_compile.log > /tmp/netty_broken
        if [ ! -s /tmp/netty_broken ]; then
            tail -n 80 /tmp/netty_compile.log
            echo "NETTY_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> /tmp/netty_removed
            rm -f "$f"
        done < /tmp/netty_broken
    done
    echo "NETTY_COMPILE_FAILED (too many rounds)"
    return 1
}

run_tests() {
    local classes m rc=0
    for m in ${MODULES//,/ }; do
        rm -rf "$REPO/$m/target/surefire-reports"
    done
    compile_tests
    classes=$(python3 "$HELPER" classes "$1")
    echo "NETTY_TEST_CLASSES=$classes"
    test -n "$classes"
    ./mvnw -o test -pl "$MODULES" -am -Dtest="$classes" -Dsurefire.failIfNoSpecifiedTests=false \
        > /tmp/netty_test.log 2>&1 || rc=$?
    grep -E 'Tests run:|FAIL|BUILD (SUCCESS|FAILURE)' /tmp/netty_test.log | tail -n 200 || true
    if [ "$rc" != 0 ] && ! grep -q 'There are test failures\|There are test errors\|Tests run:.*Failures' /tmp/netty_test.log; then
        tail -n 80 /tmp/netty_test.log
    fi
    cat /tmp/netty_removed
    python3 "$HELPER" junit "$MODULES"
    echo "NETTY_SUREFIRE_EXIT=$rc"
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


class NettyImageBase(Image):
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


class NettyImageDefault(Image):
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
        return NettyImageBase(self.pr, self._config)

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
        extra = _EXTRA_ARTIFACTS.get(self.pr.number, [])
        warm_extra = "".join(
            f"./mvnw -N dependency:get -Dtransitive=true -Dartifact={a}\n" for a in extra
        )
        gate_extra = "".join(
            "test -f ~/.m2/repository/{g}/{a}/{v}/{a}-{v}.jar\n".format(
                g=c.split(":")[0].replace(".", "/"), a=c.split(":")[1], v=c.split(":")[2]
            )
            for c in extra
        )
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
            File(".", "netty_tests.py", tests_py),
            File(".", "netty_tests.sh", tests_sh),
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

modules=$(python3 /home/netty_tests.py modules /home/test.patch)
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
classes=$(python3 /home/netty_tests.py classes /home/test.patch)
if [ -n "$classes" ]; then
  ./mvnw test -pl "$modules" -am -Dtest="$classes" \\
    -Dsurefire.failIfNoSpecifiedTests=false -Dmaven.test.failure.ignore=true
fi
for m in ${{modules//,/ }}; do
  rm -rf "$m/target/surefire-reports"
done

# Download (only) the artifacts fix.patch adds to the poms, so the fix stage
# builds offline. Nothing is applied to the tree.
{warm_extra}
# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
./mvnw -o test-compile -pl "$modules" -am -q
for m in ${{modules//,/ }}; do
  test -d "$m/target/test-classes"
done
ls -d ~/.m2/repository/org/apache/maven/surefire/surefire-junit-platform/*/ > /dev/null
{gate_extra}echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
bash /home/netty_tests.sh /home/test.patch
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
bash /home/netty_tests.sh /home/test.patch
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
bash /home/netty_tests.sh /home/test.patch
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


@Instance.register("netty", "netty")
class NETTY(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NettyImageDefault(self.pr, self._config)

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
            r"^NETTY_TEST (PASSED|FAILED|SKIPPED) (\S+#\S+?)(?: \(test source does not compile\))?\s*$"
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
