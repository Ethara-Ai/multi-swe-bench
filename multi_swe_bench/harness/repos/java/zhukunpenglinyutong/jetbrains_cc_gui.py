import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# jetbrains-cc-gui is an IntelliJ Platform plugin with three test suites, all
# of which .github/workflows/tests.yml runs on ubuntu-latest:
#   * Java unit tests (JUnit 4) via `./gradlew test`;
#   * ai-bridge unit tests via Node's built-in runner (`node --test`);
#   * webview unit tests via vitest (happy-dom).
# build.gradle (both PR bases) pins a Java 17 toolchain and Gradle 8.14.4 via
# the wrapper. The official eclipse-temurin image is multi-arch and is itself a
# JDK 17, so the toolchain is satisfied by the current JVM; auto-download is
# disabled. CI uses Node 22.12.0, installed from the official tarball for the
# build architecture.
_JDK_IMAGE = "eclipse-temurin:17-jdk-noble"
_NODE_VERSION = "22.12.0"

GRADLE_PROPERTIES = """org.gradle.java.installations.auto-download=false
org.gradle.java.installations.auto-detect=true
org.gradle.daemon=false
org.gradle.caching=false
org.gradle.console=plain
org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8
"""

_ENV = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export TERM=dumb
export GRADLE_OPTS="-Xmx512m -Dfile.encoding=UTF-8\""""

# Helper: finds Java test sources that no longer compile (and the tests
# test.patch adds in them), and turns JUnit XML (Gradle and node:test) plus the
# vitest JSON report into "CCGUI_TEST <STATUS> <id>" lines.
TESTS_PY = r'''import json
import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
BROKEN = re.compile(r"(/\S+?/src/test/java/\S+?\.java):\d+: error:")
JAVA_TEST = re.compile(
    r"@Test\b(?:\s*\([^)]*\))?\s+(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final)\s+)*void\s+(\w+)\s*\("
)
RANK = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
results = {}


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def put(tid, status):
    tid = norm(tid)
    if tid and (tid not in results or RANK[status] > RANK[results[tid]]):
        results[tid] = status


def local(tag):
    return tag.rsplit("}", 1)[-1]


def node_files(_):
    out = []
    for root, dirs, files in os.walk(os.path.join(REPO, "ai-bridge")):
        dirs[:] = sorted(d for d in dirs if d != "node_modules")
        for f in sorted(files):
            if f.endswith(".test.js") or f.endswith(".test.mjs"):
                out.append(os.path.relpath(os.path.join(root, f), REPO))
    for f in sorted(out):
        print(f)


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
    with open(src_file, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    pkg = re.search(r"^\s*package\s+([\w.]+)", text, re.M)
    stem = os.path.splitext(os.path.basename(src_file))[0]
    cls = (pkg.group(1) + "." if pkg else "") + stem
    touched = set(re.findall(r"\bvoid\s+(\w+)\s*\(", added_lines(patch_file, rel)))
    for name in sorted(set(JAVA_TEST.findall(text)) & touched):
        print("CCGUI_TEST FAILED " + cls + "#" + name + " (test source does not compile)")


def junit_status(tc):
    tags = {local(c.tag) for c in tc}
    if tags & {"failure", "error"} or tc.get("failure"):
        return "FAILED"
    if "skipped" in tags:
        return "SKIPPED"
    return "PASSED"


def gradle(rdir):
    if not os.path.isdir(rdir):
        return
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
            cls = norm(tc.get("classname"))
            name = norm(tc.get("name"))
            if cls and name:
                put(cls + "#" + name, junit_status(tc))


def node_suite(el, rel, path):
    for c in el:
        t = local(c.tag)
        if t == "testsuite":
            node_suite(c, rel, path + [norm(c.get("name"))])
        elif t == "testcase":
            put(" > ".join([rel] + path + [norm(c.get("name"))]), junit_status(c))


def node(map_file):
    with open(map_file, encoding="utf-8") as fh:
        for line in fh:
            xml, rel = line.rstrip("\n").split("\t", 1)
            if not os.path.isfile(xml) or os.path.getsize(xml) == 0:
                continue
            try:
                root = ET.parse(xml).getroot()
            except ET.ParseError:
                continue
            node_suite(root, rel, [])


def vitest(json_file):
    if not os.path.isfile(json_file):
        return
    with open(json_file, encoding="utf-8") as fh:
        data = json.load(fh)
    for tr in data.get("testResults", []):
        rel = os.path.relpath(tr.get("name", ""), REPO)
        for a in tr.get("assertionResults", []):
            st = a.get("status")
            status = {"passed": "PASSED", "failed": "FAILED"}.get(st, "SKIPPED")
            parts = [norm(x) for x in a.get("ancestorTitles", [])] + [norm(a.get("title"))]
            put(" > ".join([rel] + parts), status)


def deps(_):
    """Maven coordinates that test.patch / fix.patch add to a *.gradle file,
    so prepare.sh can put their jars into ~/.gradle without applying
    either patch (the run stages build offline)."""
    rx = re.compile(
        r"^\+\s*(?:implementation|api|compileOnly|runtimeOnly|testImplementation|"
        r"testCompileOnly|testRuntimeOnly)\s*\(?\s*['\"]([\w.\-]+:[\w.\-]+:[\w.\-]+)['\"]"
    )
    found = set()
    for patch_file in ("/home/test.patch", "/home/fix.patch"):
        current = None
        with open(patch_file, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
                if m:
                    current = m.group(2)
                    continue
                if current and current.endswith((".gradle", ".gradle.kts")):
                    m = rx.match(line)
                    if m:
                        found.add(m.group(1))
    for c in sorted(found):
        print(c)


def report(out_dir):
    gradle(os.path.join(REPO, "build", "test-results", "test"))
    node(os.path.join(out_dir, "node.map"))
    vitest(os.path.join(out_dir, "vitest.json"))
    for tid in sorted(results):
        print("CCGUI_TEST " + results[tid] + " " + tid)


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {
        "nodefiles": node_files,
        "broken": broken,
        "methods": methods,
        "report": report,
        "deps": deps,
    }[cmd](arg)
'''

# Driver for all three suites. Every suite runs offline against what
# prepare.sh installed (node_modules, ~/.gradle, .intellijPlatform are
# git-ignored, so a `git clean -fd` keeps them). Each runner's exit code is
# recorded and the script exits non-zero if any suite failed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/ccgui_tests.py
OUT=/tmp/ccgui
GRADLE="./gradlew --offline --no-daemon --console=plain -PskipWebview=true"
cd "$REPO"
rm -rf "$OUT"
mkdir -p "$OUT"
: > "$OUT/removed"

# ---- Java (JUnit 4 via Gradle) ----
compile_tests() {
    local i f rc
    for i in $(seq 1 15); do
        rc=0
        $GRADLE testClasses > "$OUT/compile.log" 2>&1 || rc=$?
        grep -E 'error:|BUILD (SUCCESSFUL|FAILED)' "$OUT/compile.log" | head -n 60 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken "$OUT/compile.log" > "$OUT/broken"
        if [ ! -s "$OUT/broken" ]; then
            tail -n 80 "$OUT/compile.log"
            echo "CCGUI_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> "$OUT/removed"
            rm -f "$f"
        done < "$OUT/broken"
    done
    echo "CCGUI_COMPILE_FAILED (too many rounds)"
    return 1
}

rm -rf build/test-results/test
java_rc=0
compile_tests || java_rc=$?
if [ "$java_rc" = 0 ]; then
    $GRADLE test --continue --rerun > "$OUT/gradle-test.log" 2>&1 || java_rc=$?
    grep -E 'FAILED|BUILD (SUCCESSFUL|FAILED)|tests completed' "$OUT/gradle-test.log" | tail -n 200 || true
fi
echo "CCGUI_JAVA_EXIT=$java_rc"

# ---- ai-bridge (node:test, run from the repo root as CI does) ----
node_rc=0
: > "$OUT/node.map"
i=0
while IFS= read -r f; do
    i=$((i + 1))
    rc=0
    node --test --test-timeout=300000 --test-reporter=junit \
        --test-reporter-destination="$OUT/node-$i.xml" "$f" > /dev/null 2>&1 || rc=$?
    printf '%s\t%s\n' "$OUT/node-$i.xml" "$f" >> "$OUT/node.map"
    [ "$rc" = 0 ] || { echo "NODE_TEST_FILE_FAILED $f (exit $rc)"; node_rc=1; }
done < <(python3 "$HELPER" nodefiles)
test "$i" -gt 0
echo "CCGUI_NODE_FILES=$i"
echo "CCGUI_NODE_EXIT=$node_rc"

# ---- webview (vitest) ----
web_rc=0
cd "$REPO/webview"
node scripts/extract-version.mjs
npx vitest run --reporter=dot --reporter=json --outputFile.json="$OUT/vitest.json" || web_rc=$?
test -s "$OUT/vitest.json"
echo "CCGUI_WEBVIEW_EXIT=$web_rc"
cd "$REPO"

cat "$OUT/removed"
python3 "$HELPER" report "$OUT"
rc=0
[ "$java_rc" = 0 ] && [ "$node_rc" = 0 ] && [ "$web_rc" = 0 ] || rc=1
echo "CCGUI_EXIT=$rc"
exit "$rc"
'''


# Init script used only by prepare.sh: resolves the coordinates listed in
# $CCGUI_PREFETCH (with transitive dependencies) against the project's own
# repositories, which caches them for the offline run stages.
PREFETCH_GRADLE = """rootProject {
    afterEvaluate { p ->
        p.tasks.register('ccguiPrefetch') {
            doLast {
                def coords = (System.getenv('CCGUI_PREFETCH') ?: '').split(/\\s+/).findAll { it }
                if (coords) {
                    def deps = coords.collect { p.dependencies.create(it) }
                    p.configurations.detachedConfiguration(*deps).resolve().each {
                        println "CCGUI_PREFETCHED ${it.name}"
                    }
                }
            }
        }
    }
}
"""


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


class JetbrainsCcGuiImageBase(Image):
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
        return ["unzip", "zip", "xz-utils"]

    def extra_setup(self) -> str:
        return f"""ENV CI=true
RUN set -eux; \\
    arch="$(dpkg --print-architecture)"; \\
    case "$arch" in amd64) narch=x64 ;; arm64) narch=arm64 ;; *) echo "unsupported arch $arch"; exit 1 ;; esac; \\
    curl -fsSL "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-$narch.tar.xz" -o /tmp/node.tar.xz; \\
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \\
    rm -f /tmp/node.tar.xz; \\
    node --version; npm --version; java -version
RUN git config --global --add safe.directory '*'"""

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. Per-PR base, so the pin and
    # the history scrub belong here.


class JetbrainsCcGuiImageDefault(Image):
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
        return JetbrainsCcGuiImageBase(self.pr, self._config)

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
            File(".", "ccgui_prefetch.gradle", PREFETCH_GRADLE),
            File(".", "ccgui_tests.py", tests_py),
            File(".", "ccgui_tests.sh", tests_sh),
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

retry() {{
  local n
  for n in 1 2 3; do
    if "$@"; then return 0; fi
    sleep 20
  done
  return 1
}}

# npm dependencies of both JS packages (lockfiles committed).
(cd webview && retry npm ci --no-audit --no-fund)
(cd ai-bridge && retry npm ci --no-audit --no-fund)

# Gradle distribution, IntelliJ Platform SDK and every compile/test
# dependency; compile main + test sources.
retry ./gradlew --no-daemon -PskipWebview=true testClasses

# Libraries that test.patch / fix.patch add to build.gradle: cache their jars
# (the patches themselves are not applied here).
CCGUI_PREFETCH="$(python3 /home/ccgui_tests.py deps)"
export CCGUI_PREFETCH
echo "CCGUI_PREFETCH=$CCGUI_PREFETCH"
if [ -n "$CCGUI_PREFETCH" ]; then
  retry ./gradlew --no-daemon -PskipWebview=true --init-script /home/ccgui_prefetch.gradle ccguiPrefetch
fi

# Resolve run-time-only test dependencies (JUnit launcher, test sandbox) by
# running the test task once at the base commit. A failing base test is
# tolerated here (the stages report it); the gate below requires that the
# test JVM actually started and wrote JUnit XML.
./gradlew --no-daemon -PskipWebview=true test --continue || true

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
./gradlew --offline --no-daemon -q -PskipWebview=true testClasses
test -d build/classes/java/test
ls build/test-results/test/TEST-*.xml > /dev/null
rm -rf build/test-results build/reports
(cd webview && node scripts/extract-version.mjs && npx --no-install vitest --version)
(cd ai-bridge && node -e "require.resolve('sql.js')")
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
bash /home/ccgui_tests.sh
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
bash /home/ccgui_tests.sh
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
bash /home/ccgui_tests.sh
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


@Instance.register("zhukunpenglinyutong", "jetbrains-cc-gui")
class JETBRAINS_CC_GUI(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JetbrainsCcGuiImageDefault(self.pr, self._config)

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
            r"^CCGUI_TEST (PASSED|FAILED|SKIPPED) (.+?)"
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
