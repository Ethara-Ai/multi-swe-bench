"""questdb/questdb, era #7484 onward (JDK 25 + local-client submodule era).

Why a separate era file: the legacy `questdb/questdb` config (questdb.py) runs
`mvn clean test -pl core` on ubuntu:22.04 with apt openjdk-<N> picked from the
root pom, which does not fit this era. At base fbbe40f4 (PR #7484):

  * core/pom.xml sets javac.target=25, and the `java25+` profile (jdk (24,))
    is the only one that defines javac.compile.source/target and
    java.enforce.version, so JDK 25 is required (CI: "Activate JDK 25").
  * core depends on org.questdb:questdb-client:${questdb.client.version} =
    1.3.9-SNAPSHOT (test scope). A -SNAPSHOT client is not on Maven Central;
    CI (ci/templates/detect-local-client.yml + build-client-native.yml) checks
    out the `java-questdb-client` git submodule (recursively, for zstd), builds
    its JNI library with cmake (nasm only on x86-64), copies it to the client's
    src/main/resources/io/questdb/client/bin/<platform>/, and builds the client
    from source (`-P local-client`). This config does the same, then installs
    the client to ~/.m2 so the run stages build only `core`, offline.
  * The core's own native libraries (libquestdb.so, libquestdbr.so) are
    committed prebuilt for both linux-x86-64 and linux-aarch64
    (core/src/main/resources/io/questdb/bin/<platform>/); the gate checks the
    current platform's copies.
  * JUnit 4.13.2, surefire 3.5.3 (useModulePath, TestListener).

Repo acquisition: the GitHub repo is ~2.2 GB of history (committed binaries),
so instead of a full clone + `git gc --aggressive` over all of it, the per-PR
base fetches exactly BASE_COMMIT by SHA (`git fetch --depth 1 origin <sha>`,
which also covers base commits that no branch points at any more). The
standard pin and Image._HARDENING_BLOCK then run unchanged.

Test scope (from test.patch): the Maven modules that hold the touched test
sources (`core` here), and the touched test classes themselves. Test sources
that javac rejects at a stage are removed; only the @Test methods that
test.patch adds or rewrites in them are reported FAILED. Results come from the
surefire XML: `QDB_TEST <STATUS> <module>:<class>#<method>`.

Self-contained: no imports from questdb.py.
"""

import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA = "7484_to_99999"

# Multi-arch (amd64 + arm64) official Temurin image; JDK 25 per core/pom.xml.
_JDK_IMAGE = "eclipse-temurin:25-jdk-noble"

# Maven distribution (architecture independent), checksum-verified.
_MAVEN_VERSION = "3.9.9"

_ENV = """export CI=true
export NO_COLOR=1
export TZ=UTC
export MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"
export QDB_MVN_FLAGS="-B -ntp -nsu -T 3 -Dstyle.color=never -Djacoco.skip=true -Dmaven.javadoc.skip=true -Dmaven.test.skip=false"
"""

TESTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
TEST_DIR = "src/test/java/"
TOUCHED = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase)\.java$")
BROKEN = re.compile(r"^\[ERROR\] (/\S+?/src/test/java/\S+?\.java):\[\d+")
TEST_METHOD = re.compile(
    r"@(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b(?:\s*\([^)]*\))?\s+"
    r"(?:@[\w.]+(?:\s*\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final)\s+)*(?:void|\w+(?:<[^>]*>)?)\s+(\w+)\s*\("
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
    return re.search(r"@(?:org\.junit\.)?Test\b", text) is not None


def classes(patch_file):
    out = set()
    for mod, rest in test_sources(patch_file):
        path = os.path.join(REPO, mod, TEST_DIR, rest)
        if TOUCHED.match(os.path.basename(rest)) and os.path.isfile(path) and runnable(path):
            out.add(rest[:-5].replace("/", "."))
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
    names = set()
    current = None
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                current = m.group(2)
                continue
            if current == rel and line.startswith("+") and not line.startswith("+++"):
                names.update(re.findall(r"\b(?:void|\w+(?:<[^>]*>)?)\s+(\w+)\s*\(", line))
    return names


def strip_java(text):
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
    stack = []
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
    """Tests test.patch adds or rewrites in a source that no longer compiles
    -> FAILED. Untouched tests of that file stay unreported (NONE)."""
    rel = os.path.relpath(src_file, REPO)
    if ("/" + TEST_DIR) not in ("/" + rel):
        return
    if not TOUCHED.match(os.path.basename(rel)) or not runnable(src_file):
        return
    _, _, rest = ("/" + rel).partition("/" + TEST_DIR)
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
            print("QDB_TEST FAILED " + tid + " (test source does not compile)")


def method_name(name):
    name = name.strip()
    name = re.sub(r"\[[^\]]*\]\s*$", "", name)
    m = re.match(r"^([^(\[]+)\(.*$", name)
    if m:
        name = m.group(1)
    return name.strip()


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
                tid = re.sub(r"\s+", "_", module + ":" + cls + "#" + name).strip()
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
        print("QDB_TEST " + results[tid] + " " + tid)


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

# Driver. Offline (-o): every stage must work from a reset tree with no
# network (Harbor verifier / grader). Maven's exit code is never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/qdb_tests.py
cd "$REPO"

MODULES=$(python3 "$HELPER" modules "$1")
echo "QDB_TEST_MODULES=$MODULES"
test -n "$MODULES"

compile_tests() {
    local i f m rc
    : > /tmp/qdb_removed
    # Stale classes of a source removed below must not be run, so every stage
    # compiles its test sources from scratch.
    for m in ${MODULES//,/ }; do
        rm -rf "$REPO/$m/target/test-classes" "$REPO/$m/target/surefire-reports"
    done
    for i in $(seq 1 15); do
        rc=0
        mvn $QDB_MVN_FLAGS -o test-compile -pl "$MODULES" > /tmp/qdb_compile.log 2>&1 || rc=$?
        grep -E '^\[ERROR\]|BUILD (SUCCESS|FAILURE)' /tmp/qdb_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/qdb_compile.log > /tmp/qdb_broken
        if [ ! -s /tmp/qdb_broken ]; then
            tail -n 80 /tmp/qdb_compile.log
            echo "QDB_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> /tmp/qdb_removed
            rm -f "$f"
        done < /tmp/qdb_broken
    done
    echo "QDB_COMPILE_FAILED (too many rounds)"
    return 1
}

run_tests() {
    local classes rc=0
    compile_tests
    classes=$(python3 "$HELPER" classes "$1")
    echo "QDB_TEST_CLASSES=$classes"
    test -n "$classes"
    mvn $QDB_MVN_FLAGS -o test -pl "$MODULES" -Dtest="$classes" \
        -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false \
        > /tmp/qdb_test.log 2>&1 || rc=$?
    grep -E 'Tests run:|FAIL|BUILD (SUCCESS|FAILURE)' /tmp/qdb_test.log | tail -n 200 || true
    if [ "$rc" != 0 ] && ! grep -q 'There are test failures\|There are test errors\|Tests run:.*Failures' /tmp/qdb_test.log; then
        tail -n 80 /tmp/qdb_test.log
    fi
    cat /tmp/qdb_removed
    python3 "$HELPER" junit "$MODULES"
    echo "QDB_SUREFIRE_EXIT=$rc"
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


class Questdb7484To99999ImageBase(Image):
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
        return f"base-{_ERA}-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-{_ERA}-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["cmake", "unzip"]

    def extra_setup(self) -> str:
        mvn = _MAVEN_VERSION
        url = (
            "https://archive.apache.org/dist/maven/maven-3/"
            f"{mvn}/binaries/apache-maven-{mvn}-bin.tar.gz"
        )
        return (
            "ENV CI=true \\\n"
            "    TZ=UTC \\\n"
            '    MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8" \\\n'
            f"    MAVEN_HOME=/opt/apache-maven-{mvn} \\\n"
            f"    PATH=/opt/apache-maven-{mvn}/bin:$PATH\n"
            # nasm assembles the client's x86-64 asmlib; arm64 does not use it.
            'RUN if [ "$(uname -m)" = x86_64 ]; then \\\n'
            "        apt-get update && apt-get install -y --no-install-recommends nasm \\\n"
            "        && rm -rf /var/lib/apt/lists/*; \\\n"
            "    fi\n"
            "RUN set -eux; \\\n"
            f"    curl -fsSL -o /tmp/maven.tgz {url}; \\\n"
            f'    echo "$(curl -fsSL {url}.sha512 | cut -d" " -f1)  /tmp/maven.tgz" | sha512sum -c -; \\\n'
            "    tar -xzf /tmp/maven.tgz -C /opt; \\\n"
            "    rm -f /tmp/maven.tgz; \\\n"
            "    mvn -v\n"
            # The SNAPSHOT client core depends on is a git submodule (with zstd
            # nested in it); check it out at the commit BASE_COMMIT records,
            # while the remotes still exist. The hardening block scrubs it too.
            "RUN git config --global --add safe.directory '*' \\\n"
            "    && git submodule update --init --recursive --depth 1 java-questdb-client \\\n"
            "    && git submodule status --recursive java-questdb-client \\\n"
            "    && java -version"
        )

    def dockerfile(self) -> str:
        # The default per-PR Dockerfile, with the full clone replaced by a
        # fetch of exactly BASE_COMMIT by SHA (the history is ~2.2 GB and the
        # base commit need not be on any branch). The standard pin, extra_setup
        # and hardening block follow unchanged.
        default = super().dockerfile()
        clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        fetch = (
            f"RUN git init -q /home/{self.pr.repo} \\\n"
            f"    && cd /home/{self.pr.repo} \\\n"
            '    && git remote add origin "${REPO_URL}" \\\n'
            '    && git fetch --no-tags --depth 1 origin "${BASE_COMMIT}" \\\n'
            "    && git checkout -q --detach FETCH_HEAD"
        )
        if default.count(clone) != 1:
            raise RuntimeError("unexpected default Dockerfile layout")
        return default.replace(clone, fetch)


class Questdb7484To99999ImageDefault(Image):
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
        return Questdb7484To99999ImageBase(self.pr, self._config)

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
            File(".", "qdb_tests.py", tests_py),
            File(".", "qdb_tests.sh", tests_sh),
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

modules=$(python3 /home/qdb_tests.py modules /home/test.patch)
echo "modules=$modules"
test -n "$modules"

# The submodule must sit at the commit BASE_COMMIT records ('-' = missing,
# '+' = other commit).
git submodule status --recursive java-questdb-client | tee /tmp/qdb_sub.txt
if grep -q '^[-+U]' /tmp/qdb_sub.txt; then exit 1; fi

# questdb-client (a -SNAPSHOT): build its JNI library from source as CI
# does (ci/templates/build-client-native.yml), then build + install the client
# into ~/.m2 so core resolves it offline. The library ends up inside the
# installed client jar; the submodule tree is cleaned afterwards.
case "$(uname -m)" in
  aarch64|arm64) plat=linux-aarch64 ;;
  x86_64) plat=linux-x86-64 ;;
  *) echo "unsupported arch $(uname -m)"; exit 1 ;;
esac
(
  cd java-questdb-client/core
  cmake -DCMAKE_BUILD_TYPE=Release -B cmake-build-release -S.
  cmake --build cmake-build-release --config Release --parallel 4
  mkdir -p "src/main/resources/io/questdb/client/bin/$plat"
  cp target/classes/io/questdb/client/bin-local/libquestdb.so \\
     "src/main/resources/io/questdb/client/bin/$plat/libquestdb.so"
)
ok=0
for attempt in 1 2 3; do
  if mvn $QDB_MVN_FLAGS -f java-questdb-client/pom.xml -pl core -am install -DskipTests; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1
git submodule foreach --recursive 'git reset -q --hard && git clean -qfdx'

# Resolve every build/test dependency of the scoped modules and compile main
# and test sources at the base commit. Retries cover transient network errors.
ok=0
for attempt in 1 2 3; do
  if mvn $QDB_MVN_FLAGS test-compile -pl "$modules"; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1

# Resolve the surefire JUnit 4 provider by running one base-commit test class
# once. Test failures at the base are not a provisioning error.
first=$(python3 /home/qdb_tests.py classes /home/test.patch | cut -d, -f1)
if [ -n "$first" ]; then
  mvn $QDB_MVN_FLAGS test -pl "$modules" -Dtest="$first" \\
    -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true
fi
for m in ${{modules//,/ }}; do
  rm -rf "$m/target/surefire-reports"
done

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
mvn $QDB_MVN_FLAGS -o -q test-compile -pl "$modules"
for m in ${{modules//,/ }}; do
  test -d "$m/target/test-classes"
done
test -s "core/src/main/resources/io/questdb/bin/$plat/libquestdb.so"
test -s "core/src/main/resources/io/questdb/bin/$plat/libquestdbr.so"
cv=$(sed -n 's/.*<questdb.client.version>\\(.*\\)<\\/questdb.client.version>.*/\\1/p' core/pom.xml | head -1)
test -n "$cv"
unzip -l ~/.m2/repository/org/questdb/questdb-client/$cv/questdb-client-$cv.jar > /tmp/qdb_client_jar.txt
grep -q "io/questdb/client/bin/$plat/libquestdb.so" /tmp/qdb_client_jar.txt
ls -d ~/.m2/repository/org/apache/maven/surefire/surefire-junit4/*/ > /dev/null
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
bash /home/qdb_tests.sh /home/test.patch
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
bash /home/qdb_tests.sh /home/test.patch
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
bash /home/qdb_tests.sh /home/test.patch
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


@Instance.register("questdb", "questdb_7484_to_99999")
class QUESTDB_7484_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Questdb7484To99999ImageDefault(self.pr, self._config)

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
            r"^QDB_TEST (PASSED|FAILED|SKIPPED) (\S+#\S+?)(?: \(test source does not compile\))?\s*$"
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
