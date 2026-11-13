import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# build.gradle at both PR bases: Gradle java toolchain languageVersion 21,
# freefair aspectj 8.13; CI (.github/workflows/unit-test.yml) uses JDK 21.
# The official image is multi-arch (amd64 + arm64).
_JDK_IMAGE = "eclipse-temurin:21-jdk-noble"

# supertokens-core is not buildable on its own: CI checks it out inside
# supertokens-root (Gradle wrapper 8.13, root build.gradle, settings.gradle
# written by ./loadModules) next to supertokens-plugin-interface, then runs
# ./utils/setupTestEnv --local, and the tests find that root as "../"
# (hardcoded in many tests). Here the root is /home: the core clone sits at
# /home/supertokens-core, so "../" resolves to /home. The sqlite job of the CI
# matrix needs no external database (in-memory SQLite storage built into core).
_ROOT_REPO = "https://github.com/supertokens/supertokens-root.git"
# supertokens-root master as of 2026-08-14 (latest commit before both PRs).
_ROOT_SHA = "67451636c86423757ee4cafbb5cd6db71844d1f2"
_PI_REPO = "https://github.com/supertokens/supertokens-plugin-interface.git"

# The plugin-interface tree each core base was developed against. Both bases
# are on core's feat/activity-log branch, whose CI resolves the
# plugin-interface branch of the same name (since merged into plugin-interface
# master and deleted). The sqlite Start class implements the storage
# interfaces, so the interface must match the core commit exactly:
#  - core d76a4843 (merge of core #1380, activity-log contract) pairs with
#    plugin-interface #211 (activity-log contract) = merge 21182c40 on
#    plugin-interface feat/activity-log.
#  - core 0996b46f (merge of core #1395 window read + #1399 txn sign-up with the
#    (con, tenant, ...) parameter order) pairs with plugin-interface #215
#    (window read, merged as 58f151f0) plus #217's first commit 3de1b5dc
#    (txn sign-up); #217's second commit a909e14e swapped the parameter order
#    to (tenant, con, ...) and does not compile with this core. No single
#    plugin-interface commit has exactly that tree, so the two are merged
#    (clean merge) and the resulting tree hash is asserted.
# Value: (commit, commits merged on top of it, expected tree hash).
_PI_BY_BASE = {
    "d76a484382caee64e511f03198c9bde4ed27cf17": (
        "21182c40e8554cbce198b372908105e83da9acee",
        [],
        "cacfa9e1ee0f6ced41759b0e77dcadf17f32f984",
    ),
    "0996b46feaad80bcafc827968c2b3bda5459046c": (
        "58f151f0f735770e50af274a935b6f7bf3efe4b8",
        ["3de1b5dce0fe5a96aa1a94af4b91bc80d9dc675f"],
        "d8d0e0b37faa18f538ee74c95d8eebb675f59ea0",
    ),
}

# Helper: decides the test scope from test.patch, finds test sources that no
# longer compile, lists the @Test methods test.patch adds to them, and turns
# Gradle JUnit XML reports into "ST_TEST <STATUS> <module>:<class>#<method>".
TESTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

ROOT = "/home"
MODULE = "[[REPO]]"
REPO = ROOT + "/" + MODULE
TEST_ROOT = "src/test/java/"
INCLUDE = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase)\.java$")
# javac ("X.java:12: error: ...") and ajc ("X.java:12 [error] ...") formats;
# the freefair aspectj plugin compiles test sources with ajc.
BROKEN = re.compile(r"^(/\S+?/src/test/java/\S+?\.java):\d+(?::\s*error:|\s+\[error\])")
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


def classes(patch_file):
    """Concrete test classes of the core module that test.patch touches."""
    out = set()
    for p in sorted(patch_paths(patch_file)):
        if not (p.startswith(TEST_ROOT) and p.endswith(".java")):
            continue
        f = os.path.basename(p)
        full = os.path.join(REPO, p)
        if not INCLUDE.match(f) or not os.path.isfile(full):
            continue
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if re.search(r"\babstract\s+class\s+" + re.escape(f[:-5]) + r"\b", text):
            continue
        if "@Test" not in text:
            continue
        out.add(p[len(TEST_ROOT):-5].replace("/", "."))
    for c in sorted(out):
        print(c)


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = BROKEN.search(re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", line).strip())
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
        print("ST_TEST FAILED " + MODULE + ":" + cls + "#" + name + " (test source does not compile)")


def method_name(name):
    name = name.strip()
    m = METHOD.match(name)
    if m:
        name = m.group(1) + m.group(2)
    return name


def junit(_arg):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    rdir = os.path.join(REPO, "build", "test-results", "test")
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
                tid = re.sub(r"\s+", " ", MODULE + ":" + cls + "#" + name).strip()
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
        print("ST_TEST " + results[tid] + " " + tid)


def listen(ports):
    """Accept-and-close TCP listeners on 127.0.0.1 (see TESTS_SH)."""
    import socket
    import threading

    def serve(port):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", int(port)))
        srv.listen(64)
        while True:
            conn, _ = srv.accept()
            conn.close()

    threads = [threading.Thread(target=serve, args=(p,), daemon=True) for p in ports.split(",")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {"classes": classes, "broken": broken, "methods": methods, "junit": junit, "listen": listen}[cmd](arg)
'''

# Driver: compile the test sources (dropping any that test.patch left
# uncompilable, and reporting their test.patch-added tests as FAILED), then
# run the scoped Gradle test task. The exit code is Gradle's, never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/st_tests.py
GRADLE="./gradlew --no-daemon --console=plain"
cd /home

compile_tests() {
    local i f rc
    : > /tmp/st_removed
    for i in $(seq 1 10); do
        rc=0
        $GRADLE :[[REPO]]:testClasses > /tmp/st_compile.log 2>&1 || rc=$?
        grep -E ': error:|\[error\]|BUILD (SUCCESSFUL|FAILED)' /tmp/st_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/st_compile.log > /tmp/st_broken
        if [ ! -s /tmp/st_broken ]; then
            tail -n 80 /tmp/st_compile.log
            echo "ST_COMPILE_FAILED (not a test-source error)"
            return 1
        fi
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            python3 "$HELPER" methods "$f" >> /tmp/st_removed
            rm -f "$f"
        done < /tmp/st_broken
    done
    echo "ST_COMPILE_FAILED (too many rounds)"
    return 1
}

# TestingProcessManager's static init calls TestServiceUtils.startServices(),
# which runs `docker run` for a Hydra OAuth server and a PostgreSQL server unless
# TEST_OAUTH_* / TEST_PG_* point at already-running ones, in which case it only
# probes that their TCP ports accept connections. There is no docker inside the
# image, and the scoped tests use the built-in in-memory SQLite storage (the
# CI sqlite job: no postgresql plugin is loaded, DatabaseTestHelper finds no
# PostgreSQL helper and falls back). So accept-and-close listeners stand in
# for those two endpoints; nothing in scope talks to them beyond the probe.
start_service_stubs() {
    python3 "$HELPER" listen 15432,14444,14445 &
    STUB_PID=$!
    trap 'kill "$STUB_PID" 2>/dev/null || true' EXIT
    export TEST_PG_HOST=127.0.0.1 TEST_PG_PORT=15432
    export TEST_OAUTH_HOST=127.0.0.1 TEST_OAUTH_PUBLIC_PORT=14444 TEST_OAUTH_ADMIN_PORT=14445
}

run_tests() {
    local rc=0 c
    local -a filters=()
    rm -rf "$REPO/build/test-results/test"
    compile_tests
    while IFS= read -r c; do
        [ -n "$c" ] && filters+=(--tests "$c")
    done < <(python3 "$HELPER" classes "$1")
    echo "ST_TEST_CLASSES=${filters[*]}"
    if [ "${#filters[@]}" -gt 0 ]; then
        start_service_stubs
        $GRADLE :[[REPO]]:test --rerun "${filters[@]}" > /tmp/st_test.log 2>&1 || rc=$?
        grep -E ' > .* (PASSED|FAILED|SKIPPED)$|Results: |BUILD (SUCCESSFUL|FAILED)|^\* What went wrong' /tmp/st_test.log | tail -n 300 || true
    fi
    cat /tmp/st_removed
    python3 "$HELPER" junit
    echo "ST_GRADLE_EXIT=$rc"
    if [ "${#filters[@]}" -eq 0 ] && [ -s /tmp/st_removed ]; then
        return 1
    fi
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


class SupertokensCoreImageBase(Image):
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


class SupertokensCoreImageDefault(Image):
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
        return SupertokensCoreImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pi_sha, pi_merges, pi_tree = _PI_BY_BASE[sha]
        pi_merge_cmds = "".join(
            f"git -C /home/supertokens-plugin-interface fetch -q --depth 5 {_PI_REPO} {m}\n"
            "git -C /home/supertokens-plugin-interface -c user.name=msb -c user.email=msb@localhost "
            f"merge -q --no-edit {m}\n"
            for m in pi_merges
        )
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
            File(".", "st_tests.py", tests_py),
            File(".", "st_tests.sh", tests_sh),
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
export CI=true
export NO_COLOR=1
export TERM=dumb

fetch_at() {{  # <dir> <url> <40-hex sha>
  local ok=0 attempt
  rm -rf "$1"
  mkdir -p "$1"
  git -C "$1" init -q
  for attempt in 1 2 3; do
    if git -C "$1" fetch -q --depth 5 "$2" "$3"; then ok=1; break; fi
    sleep 15
  done
  test "$ok" = 1
  git -C "$1" checkout -q --detach FETCH_HEAD
  test "$(git -C "$1" rev-parse HEAD)" = "$3"
}}

# The plugin-interface checkout is a build dependency in its own directory, not
# the graded core tree; for 1392 two pre-base plugin-interface commits are merged
# there (see _PI_BY_BASE) and the resulting tree hash is asserted.
# supertokens-root layout with /home as the root (core's tests use "../").
fetch_at /tmp/supertokens-root {_ROOT_REPO} {_ROOT_SHA}
fetch_at /home/supertokens-plugin-interface {_PI_REPO} {pi_sha}
{pi_merge_cmds}test "$(git -C /home/supertokens-plugin-interface rev-parse 'HEAD^{{tree}}')" = "{pi_tree}"
rm -rf /home/supertokens-plugin-interface/.git
cp -r /tmp/supertokens-root/gradlew /tmp/supertokens-root/gradle /tmp/supertokens-root/build.gradle /home/
rm -rf /tmp/supertokens-root
# settings.gradle as ./loadModules writes it for modules core + plugin-interface.
cat > /home/settings.gradle <<'EOF'
include 'supertokens-core:cli'
include 'supertokens-core:downloader'
include 'supertokens-core:ee'
include '{repo}'
include 'supertokens-plugin-interface'
EOF
# Gradle daemon heap (the root gradle.properties asks for 12g; not copied).
mkdir -p "$HOME/.gradle"
printf 'org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8\\norg.gradle.daemon=false\\norg.gradle.console=plain\\n' > "$HOME/.gradle/gradle.properties"

cd /home
GRADLE="./gradlew --no-daemon --console=plain"
# setupTestEnv --local: build jars (no tests, no generateDependenciesJson which
# rewrites tracked files) and resolve every dependency, with retries.
ok=0
for attempt in 1 2 3; do
  if $GRADLE :supertokens-plugin-interface:jar :supertokens-plugin-interface:copyJars \\
      :{repo}:jar :{repo}:copyJars :{repo}:testClasses \\
      :{repo}:cli:jar :{repo}:cli:copyJars :{repo}:downloader:jar :{repo}:ee:jar; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1

mkdir -p core agent plugin plugin-interface cli downloader ee temp
cp {repo}/build/libs/* core/
cp {repo}/build/dependencies/* core/
cp {repo}/src/main/resources/* agent/
# Optional: plugin-interface has no runtime deps and the cli dependency dir is only
# used by the CLI installer, so a missing build/dependencies dir is tolerated.
cp supertokens-plugin-interface/build/libs/* plugin-interface/
cp supertokens-plugin-interface/build/dependencies/* plugin-interface/ 2>/dev/null || true
cp {repo}/cli/build/libs/* cli/
cp {repo}/cli/build/dependencies/* cli/ 2>/dev/null || true
cp {repo}/downloader/build/libs/* downloader/
cp {repo}/ee/build/libs/* ee/
cp {repo}/install install
cp {repo}/LICENSE.md LICENSE.md
core_version=$(grep -E '^version *=' {repo}/build.gradle | head -n1 | cut -d'"' -f2)
pi_version=$(grep -E '^version *=' supertokens-plugin-interface/build.gradle | head -n1 | cut -d'"' -f2)
printf 'core_version: %s\\nplugin_interface_version: %s\\n' "$core_version" "$pi_version" > version.yaml
# createConfigFileForTesting: the core's devConfig.yaml (sqlite: no plugin).
cp {repo}/devConfig.yaml temp/config.yaml
touch .testEnvRunning

# Resolve the test runtime classpath and Gradle's test workers (no test runs).
$GRADLE :{repo}:test --tests 'io.supertokens.StWarmupNoSuchTest'
rm -rf {repo}/build/test-results

# Section 3: gate
cd /home/{repo}
test "$(git rev-parse HEAD)" = "{sha}"
cd /home
test -s version.yaml && test -s temp/config.yaml && ls ee/*.jar core/*.jar > /dev/null
$GRADLE --offline -q :{repo}:testClasses
test -d {repo}/build/classes/aspectj/test/io || test -d {repo}/build/classes/java/test/io
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
export NO_COLOR=1
export TERM=dumb

cd /home/{repo}
bash /home/st_tests.sh /home/test.patch
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
export NO_COLOR=1
export TERM=dumb

cd /home/{repo}
git apply {apply} /home/test.patch
bash /home/st_tests.sh /home/test.patch
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
export NO_COLOR=1
export TERM=dumb

cd /home/{repo}
git apply {apply} /home/test.patch /home/fix.patch
bash /home/st_tests.sh /home/test.patch
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


@Instance.register("supertokens", "supertokens-core")
class SupertokensCore(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SupertokensCoreImageDefault(self.pr, self._config)

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
            r"^ST_TEST (PASSED|FAILED|SKIPPED) (\S+#\S+?)(?: \(test source does not compile\))?\s*$"
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
