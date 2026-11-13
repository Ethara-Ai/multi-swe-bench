import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The root pom.xml sets java.version=17 (maven.compiler release) at both PR
# bases (#6309 @ ace409f9, #6424 @ 40aa6c23); CI (.github/workflows/
# continuous-integration.yml) builds on Temurin 17. The enforcer asks for
# JDK [17.0.19,) (error-prone/NullAway); the current 17 image is 17.0.21.
# Maven comes from the repo's own wrapper (./mvnw, Maven 3.9.16) -- identical
# at both bases, so one era. The official image is multi-arch (amd64 + arm64).
_JDK_IMAGE = "eclipse-temurin:17-jdk-noble"

# CI=true also keeps the root pom's "format-apply" profile (activated by
# !env.CI) from rewriting sources; the "format-check" profile it activates
# instead is skipped, as are checkstyle, javadoc, jacoco and sources jars.
# The repo's .mvn/extensions.xml loads maven-build-cache-extension; it is
# disabled so every stage really compiles and runs its tests. This mvnw
# (takari 0.5.6 style) launches the wrapper jar directly and ignores the
# MAVEN_ARGS environment variable, so every call passes $MAVEN_ARGS itself.
_ENV = """export CI=true
export NO_COLOR=1
export MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"
export MAVEN_ARGS="-B -ntp -nsu -T 3 -Dstyle.color=never -Djacoco.skip=true -Denforcer.skip=true -Dspring-javaformat.skip=true -Ddisable.checks=true -Dcheckstyle.skip=true -Dmaven.javadoc.skip=true -Dmaven.source.skip=true -Dmaven.build.cache.enabled=false"
export TESTCONTAINERS_RYUK_DISABLED=true"""

# Helper. Multi-module Maven build: the scope is derived from test.patch.
#   modules  -> Maven modules (the directory in front of src/test/java, which
#               holds the module's pom.xml) of the test sources test.patch
#               touches -- old and new side of renames -- that exist in the
#               current tree; built with `-pl <modules> -am`.
#   classes  -> the surefire-pattern test classes test.patch touches plus the
#               concrete surefire-pattern test classes in the same packages
#               (pass-to-pass scope). *IT classes (integration tests against
#               real model providers), Testcontainers/Docker tests and classes
#               reading credentials from the environment without an
#               @EnabledIf* gate are left out, so nothing calls an external
#               service.
#   orphans  -> test.patch sources whose module directory has no pom.xml in
#               the current tree (a module the fix creates): their tests
#               cannot run and are reported FAILED.
#   broken   -> test sources that javac rejected (to be removed).
#   methods  -> tests of a removed source that test.patch adds/rewrites (all
#               @Test methods when test.patch creates or renames the file),
#               reported FAILED under the surefire name (Outer$Inner).
#   junit    -> surefire XML -> "SPRAI_TEST <STATUS> <module>:<class>#<method>".
TESTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
TEST_DIR = "src/test/java/"
PATCH = "/home/test.patch"
INCLUDE = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase)\.java$")
NEEDS_DOCKER = re.compile(
    r"org\.testcontainers|@Testcontainers|DockerClientFactory|GenericContainer"
)
READS_ENV = re.compile(r"System\.getenv\(")
ENV_GATED = re.compile(r"@(EnabledIfEnvironmentVariable|EnabledIfSystemProperty|EnabledIf)\b")
BROKEN = re.compile(r"^\[ERROR\] (/\S+?/src/test/java/\S+?\.java):\[\d+")
METHOD = re.compile(r"^([^(\[]+)\([^)]*\)(.*)$")
TEST_METHOD = re.compile(
    r"@Test\b(?:\s*\([^)]*\))?\s+(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final)\s+)*void\s+(\w+)\s*\("
)
CLASS_DECL = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_]\w*)")
# Tests with an environment-dependent outcome, excluded from the surefire run:
# - BedrockProxyChatModelTest#allowlistRejects...: MediaFetcher resolves
#   "evil.com" before its allowlist check, so with no network the cause is an
#   UnknownHostException, not the expected SecurityException.
# - ToolCallingAdvisorSpanHierarchyTests#modelCallSpans...: reactor/Micrometer
#   context propagation through a thread-local scope; failed once and passed
#   on reruns of the same unpatched tree (flaky).
UNSTABLE = [
    "org.springframework.ai.bedrock.converse.BedrockProxyChatModelTest#allowlistRejectsUnlistedStringUrlMediaThrowsRuntimeException",
    "org.springframework.ai.chat.client.advisor.ToolCallingAdvisorSpanHierarchyTests#modelCallSpansNestUnderToolCallingAdvisorEvenWhenAnOuterScopeIsOpen",
]


def local(tag):
    return tag.rsplit("}", 1)[-1]


def patch_blocks(patch_file):
    """(a-path, b-path, created-or-renamed, block text) per file of a patch."""
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    out = []
    for block in re.split(r"^(?=diff --git )", text, flags=re.M):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", block)
        if not m:
            continue
        head = block.split("\n@@", 1)[0]
        created = bool(re.search(r"^(new file mode|rename to) ", head, re.M))
        out.append((m.group(1), m.group(2), created, block))
    return out


def split_test(path):
    """(module dir, path below src/test/java) of a Java test source, or None."""
    if not path.endswith(".java") or ("/" + TEST_DIR) not in ("/" + path):
        return None
    mod, _, rest = ("/" + path).partition("/" + TEST_DIR)
    return (mod.strip("/") or "."), rest


def has_pom(mod):
    return os.path.isfile(os.path.join(REPO, mod, "pom.xml"))


def test_sources(patch_file):
    out = set()
    for a, b, _, _ in patch_blocks(patch_file):
        for p in (a, b):
            s = split_test(p)
            if s:
                out.add(s)
    return sorted(out)


def modules(patch_file):
    print(",".join(sorted({mod for mod, _ in test_sources(patch_file) if has_pom(mod)})))


def runnable(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    name = os.path.basename(path)[:-5]
    if re.search(r"\babstract\s+class\s+" + re.escape(name) + r"\b", text):
        return False
    if NEEDS_DOCKER.search(text):
        return False
    if READS_ENV.search(text) and not ENV_GATED.search(text):
        return False
    return True


def classes(patch_file):
    out = set()
    for mod, rest in test_sources(patch_file):
        if not has_pom(mod):
            continue
        root = os.path.join(REPO, mod, TEST_DIR)
        pkg = os.path.dirname(rest)
        touched = os.path.join(root, rest)
        if INCLUDE.match(os.path.basename(rest)) and os.path.isfile(touched) and runnable(touched):
            out.add(rest[:-5].replace("/", "."))
        full = os.path.join(root, pkg)
        if not os.path.isdir(full):
            continue
        for f in sorted(os.listdir(full)):
            if INCLUDE.match(f) and runnable(os.path.join(full, f)):
                out.add(os.path.join(pkg, f[:-5]).replace("/", "."))
    if out:
        excluded = ["!" + t for t in UNSTABLE if t.split("#", 1)[0] in out]
        print(",".join(sorted(out) + excluded))


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = BROKEN.search(line.strip())
            if m and m.group(1) not in seen and os.path.isfile(m.group(1)):
                seen.append(m.group(1))
    for f in seen:
        print(f)


def touched_methods(patch_file, rel):
    """None = every test of `rel` (test.patch creates or renames the file);
    otherwise the method names declared on lines test.patch adds to it."""
    names = set()
    for _, b, created, block in patch_blocks(patch_file):
        if b != rel:
            continue
        if created:
            return None
        for line in block.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
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


def report_failed(src_file, reason, patch_file=PATCH):
    rel = os.path.relpath(src_file, REPO)
    s = split_test(rel)
    if not s or not INCLUDE.match(os.path.basename(rel)) or not runnable(src_file):
        return
    module, rest = s
    pkg = os.path.dirname(rest).replace("/", ".")
    outer = os.path.basename(rest)[:-5]
    with open(src_file, encoding="utf-8", errors="replace") as fh:
        code = strip_java(fh.read())
    touched = touched_methods(patch_file, rel)
    seen = set()
    for m in TEST_METHOD.finditer(code):
        name = m.group(1)
        if touched is not None and name not in touched:
            continue
        cls = class_at(code, m.start(), outer)
        tid = module + ":" + (pkg + "." if pkg else "") + cls + "#" + name
        if tid not in seen:
            seen.add(tid)
            print("SPRAI_TEST FAILED " + tid + " (" + reason + ")")


def methods(src_file):
    """Tests that test.patch adds or rewrites in a source file that no longer
    compiles -> FAILED (they cannot run). Untouched tests of that file are
    left unreported (NONE) rather than marked failed."""
    report_failed(src_file, "test source does not compile")


def orphans(patch_file):
    """test.patch sources placed in a module directory without a pom.xml."""
    for _, b, _, _ in patch_blocks(patch_file):
        s = split_test(b)
        path = os.path.join(REPO, b)
        if s and not has_pom(s[0]) and os.path.isfile(path):
            report_failed(path, "module has no pom.xml", patch_file)


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
        print("SPRAI_TEST " + results[tid] + " " + tid)


def clean_reports(mods):
    """Remove stale surefire XML of the scoped modules."""
    import shutil
    for module in [m for m in mods.split(",") if m]:
        shutil.rmtree(os.path.join(REPO, module, "target", "surefire-reports"), ignore_errors=True)


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {
        "modules": modules,
        "classes": classes,
        "orphans": orphans,
        "broken": broken,
        "methods": methods,
        "junit": junit,
        "clean": clean_reports,
    }[cmd](arg)
'''

# Driver: compile the scoped modules (and their upstream reactor modules, so a
# fix in e.g. spring-ai-model is picked up), drop test sources that test.patch
# left uncompilable (reporting their new tests as FAILED), then run the scoped
# surefire suite. Test failures do not stop the reactor
# (maven.test.failure.ignore), so modules downstream of a failing one still
# run; their status comes from the surefire XML. Maven runs offline (-o): prepare.sh resolved everything into
# ~/.m2. The exit code is Maven's, never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/sprai_tests.py
cd "$REPO"

MODULES=$(python3 "$HELPER" modules "$1")
echo "SPRAI_TEST_MODULES=$MODULES"
python3 "$HELPER" orphans "$1" > /tmp/sprai_orphans
: > /tmp/sprai_removed

compile_tests() {
    local i f rc
    for i in $(seq 1 10); do
        rc=0
        ./mvnw $MAVEN_ARGS -o test-compile -pl "$MODULES" -am > /tmp/sprai_compile.log 2>&1 || rc=$?
        grep -E '^\[ERROR\]|BUILD (SUCCESS|FAILURE)' /tmp/sprai_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/sprai_compile.log > /tmp/sprai_broken
        if [ ! -s /tmp/sprai_broken ]; then
            tail -n 80 /tmp/sprai_compile.log
            echo "SPRAI_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> /tmp/sprai_removed
            rm -f "$f"
        done < /tmp/sprai_broken
    done
    echo "SPRAI_COMPILE_FAILED (too many rounds)"
    return 1
}

run_tests() {
    local classes rc=0
    if [ -n "$MODULES" ]; then
        python3 "$HELPER" clean "$MODULES"
        compile_tests
        classes=$(python3 "$HELPER" classes "$1")
        echo "SPRAI_TEST_CLASSES=$classes"
        if [ -n "$classes" ]; then
            ./mvnw $MAVEN_ARGS -o test -pl "$MODULES" -am -Dtest="$classes" -Dsurefire.failIfNoSpecifiedTests=false \
                -Dmaven.test.failure.ignore=true > /tmp/sprai_test.log 2>&1 || rc=$?
            grep -E 'Tests run:|FAIL|BUILD (SUCCESS|FAILURE)' /tmp/sprai_test.log | tail -n 200 || true
            if [ "$rc" != 0 ] && ! grep -q 'There are test failures\|There are test errors\|Tests run:.*Failures' /tmp/sprai_test.log; then
                tail -n 80 /tmp/sprai_test.log
            fi
        fi
    fi
    cat /tmp/sprai_orphans /tmp/sprai_removed
    python3 "$HELPER" junit "$MODULES"
    echo "SPRAI_SUREFIRE_EXIT=$rc"
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


class SpringAiImageBase(Image):
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


class SpringAiImageDefault(Image):
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
        return SpringAiImageBase(self.pr, self._config)

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
            File(".", "sprai_tests.py", tests_py),
            File(".", "sprai_tests.sh", tests_sh),
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

modules=$(python3 /home/sprai_tests.py modules /home/test.patch)
echo "modules=$modules"
test -n "$modules"

# Resolve the Maven distribution (./mvnw) and every build/test dependency of
# the scoped modules and their upstream reactor modules, compiling main and
# test sources at the base commit. Retries cover transient network failures.
ok=0
for attempt in 1 2 3; do
  if ./mvnw $MAVEN_ARGS test-compile -pl "$modules" -am; then
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
classes=$(python3 /home/sprai_tests.py classes /home/test.patch)
if [ -n "$classes" ]; then
  ./mvnw $MAVEN_ARGS test -pl "$modules" -am -Dtest="$classes" \\
    -Dsurefire.failIfNoSpecifiedTests=false -Dmaven.test.failure.ignore=true
fi
python3 /home/sprai_tests.py clean "$modules"

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
./mvnw $MAVEN_ARGS -o test-compile -pl "$modules" -am -q
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
bash /home/sprai_tests.sh /home/test.patch
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
bash /home/sprai_tests.sh /home/test.patch
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
bash /home/sprai_tests.sh /home/test.patch
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


@Instance.register("spring-projects", "spring-ai")
class SPRING_AI(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpringAiImageDefault(self.pr, self._config)

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
            r"^SPRAI_TEST (PASSED|FAILED|SKIPPED) (\S+#\S+?)(?: \((?:test source does not compile|module has no pom\.xml)\))?\s*$"
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
