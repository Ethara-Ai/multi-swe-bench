"""apache/dolphinscheduler, era #18088 onward (dev branch, 2026-03+).

Era boundary: PR #18088 ("[Chore] Polish CI") moved the unit-test workflow to a
per-module matrix (`./mvnw install -pl <module> -am -DskipTests -Djacoco.skip`
then `./mvnw verify -pl <module>`), and #18221 added `dolphinscheduler-bom` to
that `-pl` list. This config follows that shape. At base e587de7f (PR #18727):
java.version=1.8 (CI matrix 8 + 11), Maven wrapper 3.8.4, surefire 3.5.4,
JUnit 5 (junit-bom import in the root pom), jacoco 0.8.14 *offline*
instrumentation (it breaks re-runs on already-instrumented classes, so it is
skipped), spotless bound to the build (skipped).

Self-contained: no imports from the other dolphinscheduler era files.
"""

import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA = "18088_to_99999"

# JDK 8 is the declared baseline (java.version=1.8); the official Temurin image
# is multi-arch (amd64 + arm64).
_JDK_IMAGE = "eclipse-temurin:8-jdk-noble"

_ENV = """export CI=true
export NO_COLOR=1
export TZ=UTC
export MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"
export DS_MVN_FLAGS="-B -ntp -nsu -T 3 -Dstyle.color=never -Djacoco.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Danalyze.skip=true -DskipDepCheck=true -Ddependency-check.skip=true -Dcheckstyle.skip=true -Drat.skip=true -Dmaven.javadoc.skip=true -DskipUT=false -Dmaven.test.skip=false"
export TESTCONTAINERS_RYUK_DISABLED=true"""

# Helper. Multi-module Maven build; the scope is derived from test.patch.
#   modules  -> Maven modules holding the test sources test.patch touches,
#               prefixed with dolphinscheduler-bom (as CI does since #18221).
#   classes  -> touched test classes + surefire-pattern classes in the same
#               packages (p2p scope); Docker/Testcontainers classes left out.
#   broken   -> test sources javac rejected (to be removed).
#   methods  -> test methods test.patch adds/rewrites in a removed source,
#               reported FAILED.
#   junit    -> surefire XML -> "DS_TEST <STATUS> <module>:<class>#<method>".
#               Parameterized invocations ([1], [2], ...) are folded into
#               their method (any failing invocation fails the method) so the
#               id is identical across stages.
#   warmpom  -> a scratch pom (outside the repo) declaring the dependencies
#               that pom.xml hunks of test.patch/fix.patch add, so prepare.sh
#               can pre-fetch them for the offline run stages WITHOUT applying
#               either patch.
TESTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
TEST_DIR = "src/test/java/"
INCLUDE = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase)\.java$")
TOUCHED = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase|\w*IT)\.java$")
NEEDS_DOCKER = re.compile(
    r"org\.testcontainers|@Testcontainers|DockerClientFactory|GenericContainer"
)
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


def test_modules(patch_file):
    mods = set()
    for mod, rest in test_sources(patch_file):
        mods.add(module_of(os.path.join(mod, TEST_DIR, rest)))
    return sorted(mods)


def modules(patch_file):
    mods = test_modules(patch_file)
    if not mods:
        print("")
        return
    if os.path.isfile(os.path.join(REPO, "dolphinscheduler-bom", "pom.xml")):
        mods = ["dolphinscheduler-bom"] + [m for m in mods if m != "dolphinscheduler-bom"]
    print(",".join(mods))


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
    if ("/" + TEST_DIR) not in ("/" + rel) or not runnable(src_file):
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
            print("DS_TEST FAILED " + tid + " (test source does not compile)")


def method_name(name):
    name = name.strip()
    # "testX(String)[3]" / "testX[3]" / "testX()" -> "testX"
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
        print("DS_TEST " + results[tid] + " " + tid)


def added_deps(patch_files):
    deps = []
    for pf in patch_files:
        cur = None
        block = None
        with open(pf, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
                if m:
                    cur = m.group(2)
                    block = None
                    continue
                if not (cur and cur.endswith("pom.xml")):
                    continue
                if not line.startswith("+") or line.startswith("+++"):
                    block = None
                    continue
                body = line[1:]
                if "<dependency>" in body:
                    block = ""
                if block is not None:
                    block += body
                    if "</dependency>" in body:
                        g = re.search(r"<groupId>([^<]+)</groupId>", block)
                        a = re.search(r"<artifactId>([^<]+)</artifactId>", block)
                        v = re.search(r"<version>([^<$]+)</version>", block)
                        t = re.search(r"<type>([^<]+)</type>", block)
                        if g and a and "${" not in g.group(1) + a.group(1):
                            deps.append((g.group(1).strip(), a.group(1).strip(),
                                         v.group(1).strip() if v else "",
                                         t.group(1).strip() if t else ""))
                        block = None
    return sorted(set(deps))


def warmpom(out_dir):
    """Scratch pom: parent = the repo root pom (so junit-bom etc. manage
    versions), plus an import of dolphinscheduler-bom."""
    deps = added_deps(["/home/test.patch", "/home/fix.patch"])
    os.makedirs(out_dir, exist_ok=True)
    rel_parent = os.path.relpath(os.path.join(REPO, "pom.xml"), out_dir)
    lines = []
    for g, a, v, t in deps:
        if a.startswith("dolphinscheduler-"):
            continue
        x = "    <dependency><groupId>%s</groupId><artifactId>%s</artifactId>" % (g, a)
        if v:
            x += "<version>%s</version>" % v
        if t:
            x += "<type>%s</type>" % t
        x += "</dependency>"
        lines.append(x)
        print("WARM_DEP %s:%s:%s" % (g, a, v or "(managed)"))
    pom = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.apache.dolphinscheduler</groupId>
    <artifactId>dolphinscheduler</artifactId>
    <version>dev-SNAPSHOT</version>
    <relativePath>%s</relativePath>
  </parent>
  <artifactId>ds-warm</artifactId>
  <packaging>pom</packaging>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.apache.dolphinscheduler</groupId><artifactId>dolphinscheduler-bom</artifactId><version>${project.version}</version><type>pom</type><scope>import</scope></dependency>
  </dependencies></dependencyManagement>
  <dependencies>
%s
  </dependencies>
</project>
""" % (rel_parent, "\n".join(lines))
    with open(os.path.join(out_dir, "pom.xml"), "w") as fh:
        fh.write(pom)
    print("WARM_DEPS=%d" % len(lines))


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {
        "modules": modules,
        "classes": classes,
        "broken": broken,
        "methods": methods,
        "junit": junit,
        "warmpom": warmpom,
    }[cmd](arg)
'''

# Driver. Offline (-o): every stage must work from a reset tree with no
# network, which is how the Harbor verifier and grader run it. The surefire
# exit code is Maven's, never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/ds_tests.py
cd "$REPO"

MODULES=$(python3 "$HELPER" modules "$1")
echo "DS_TEST_MODULES=$MODULES"
test -n "$MODULES"

# The root pom configures surefire with <skipAfterFailureCount>1</...>, which
# would turn the first real failure into SKIPPED for every later test (CI runs
# with it because it only wants a pass/fail bit). Tracked config workaround.
sed -i 's#<skipAfterFailureCount>1</skipAfterFailureCount>##' pom.xml

compile_tests() {
    local i f rc
    : > /tmp/ds_removed
    for i in $(seq 1 10); do
        rc=0
        ./mvnw $DS_MVN_FLAGS -o test-compile -pl "$MODULES" -am > /tmp/ds_compile.log 2>&1 || rc=$?
        grep -E '^\[ERROR\]|BUILD (SUCCESS|FAILURE)' /tmp/ds_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/ds_compile.log > /tmp/ds_broken
        if [ ! -s /tmp/ds_broken ]; then
            tail -n 80 /tmp/ds_compile.log
            echo "DS_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> /tmp/ds_removed
            rm -f "$f"
        done < /tmp/ds_broken
    done
    echo "DS_COMPILE_FAILED (too many rounds)"
    return 1
}

run_tests() {
    local classes m rc=0
    for m in ${MODULES//,/ }; do
        rm -rf "$REPO/$m/target/surefire-reports"
    done
    compile_tests
    classes=$(python3 "$HELPER" classes "$1")
    echo "DS_TEST_CLASSES=$classes"
    test -n "$classes"
    ./mvnw $DS_MVN_FLAGS -o test -pl "$MODULES" -am -Dtest="$classes" -Dsurefire.failIfNoSpecifiedTests=false \
        > /tmp/ds_test.log 2>&1 || rc=$?
    grep -E 'Tests run:|FAIL|BUILD (SUCCESS|FAILURE)' /tmp/ds_test.log | tail -n 200 || true
    if [ "$rc" != 0 ] && ! grep -q 'There are test failures\|There are test errors\|Tests run:.*Failures' /tmp/ds_test.log; then
        tail -n 80 /tmp/ds_test.log
    fi
    cat /tmp/ds_removed
    python3 "$HELPER" junit "$MODULES"
    echo "DS_SUREFIRE_EXIT=$rc"
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


class DolphinScheduler18088To99999ImageBase(Image):
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
        return ["unzip"]

    def extra_setup(self) -> str:
        return (
            "ENV CI=true \\\n"
            "    TZ=UTC \\\n"
            '    MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"\n'
            "RUN git config --global --add safe.directory '*' && java -version"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. Per-PR base, so the pin and
    # the history scrub belong here.


class DolphinScheduler18088To99999ImageDefault(Image):
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
        return DolphinScheduler18088To99999ImageBase(self.pr, self._config)

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
            File(".", "ds_tests.py", tests_py),
            File(".", "ds_tests.sh", tests_sh),
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

modules=$(python3 /home/ds_tests.py modules /home/test.patch)
echo "modules=$modules"
test -n "$modules"

# Resolve the Maven distribution (./mvnw) and every build/test dependency of
# the scoped modules and their upstream reactor modules, compiling main and
# test sources at the base commit. Retries cover transient network failures.
ok=0
for attempt in 1 2 3; do
  if ./mvnw $DS_MVN_FLAGS test-compile -pl "$modules" -am; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1

# Resolve the surefire JUnit Platform provider by running the base-commit
# test scope once. Test failures at the base are not a provisioning error.
classes=$(python3 /home/ds_tests.py classes /home/test.patch)
if [ -n "$classes" ]; then
  ./mvnw $DS_MVN_FLAGS test -pl "$modules" -am -Dtest="$classes" \\
    -Dsurefire.failIfNoSpecifiedTests=false -Dmaven.test.failure.ignore=true
fi
for m in ${{modules//,/ }}; do
  rm -rf "$m/target/surefire-reports"
done

# Dependencies that the patches' pom.xml hunks ADD (e.g. a new test-scoped
# artifact) must already be in ~/.m2, because the run stages are offline.
# Read from the patch text only -- nothing is applied to the tree. The scratch
# pom lives outside the repo; the root pom and dolphinscheduler-bom are
# installed to ~/.m2 only so the scratch pom can import the bom.
./mvnw $DS_MVN_FLAGS -N install -DskipTests
./mvnw $DS_MVN_FLAGS install -pl dolphinscheduler-bom -DskipTests
python3 /home/ds_tests.py warmpom /home/ds-warm | tee /tmp/ds_warm.txt
ok=0
for attempt in 1 2 3; do
  if ./mvnw $DS_MVN_FLAGS -f /home/ds-warm/pom.xml dependency:resolve -DincludeScope=test; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1
rm -rf /home/ds-warm

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
./mvnw $DS_MVN_FLAGS -o -q test-compile -pl "$modules" -am
for m in ${{modules//,/ }}; do
  [ "$m" = dolphinscheduler-bom ] || test -d "$m/target/test-classes"
done
ls -d ~/.m2/repository/org/apache/maven/surefire/surefire-junit-platform/*/ > /dev/null
grep '^WARM_DEP ' /tmp/ds_warm.txt | while read -r _ coord; do
  g=${{coord%%:*}}; rest=${{coord#*:}}; a=${{rest%%:*}}
  ls -d ~/.m2/repository/${{g//.//}}/$a/*/ > /dev/null
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
bash /home/ds_tests.sh /home/test.patch
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
bash /home/ds_tests.sh /home/test.patch
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
bash /home/ds_tests.sh /home/test.patch
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


@Instance.register("apache", "dolphinscheduler_18088_to_99999")
class DOLPHINSCHEDULER_18088_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DolphinScheduler18088To99999ImageDefault(self.pr, self._config)

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
            r"^DS_TEST (PASSED|FAILED|SKIPPED) (\S+#\S+?)(?: \(test source does not compile\))?\s*$"
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
