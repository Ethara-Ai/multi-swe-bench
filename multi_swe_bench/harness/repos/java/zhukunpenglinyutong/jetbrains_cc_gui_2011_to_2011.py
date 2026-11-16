'''zhukunpenglinyutong/jetbrains-cc-gui -- single-project IntelliJ Platform plugin build (PR 2011).

Only the JUnit tests under src/test/java are graded; the ai-bridge (node) and webview (vitest) test files in the same patch are not part of the Gradle test task. -PskipWebview=true is the build's own switch that keeps the npm webview build out of the Java test path, and -x installAiBridgeDependencies keeps the ai-bridge `npm install` (which the test task depends on, and which the JUnit tests do not use) out of it too.

Architecture: PER-PR BASE. ImageBase.image_tag() is f"base-pr-{N}" and the base
inherits the harness default Image.dockerfile(), which clones "${REPO_URL}",
checks out ${BASE_COMMIT} and runs Image._HARDENING_BLOCK (pin + history scrub)
in the base layer. DockerfileEnhancer injects the build args / proxy / CA trust
into that base. The PR image (never enhanced) only COPYs the scripts and RUNs
prepare.sh once.

Grading scope: every stage runs ONE shared command (/home/run_tests.sh). It
runs the test task only of the Gradle projects that own a file the test patch
touches under src/test/ or the fix patch touches under src/main/, filtered to
the test classes the test patch touches. The filter, ignoreFailures and the
never-up-to-date rule come from an init script (/home/msb_init.gradle), so no
build file of the repo is edited. Both lists are read from the file
headers of /home/test.patch and /home/fix.patch; neither patch is applied
outside test-run.sh / fix-run.sh. A whole-repo `gradle test` would not finish
inside the harness's 1800 s stage timeout on this repo, and unrelated
environment-dependent tests would only add PASS->FAIL noise.
'''

import os
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "zhukunpenglinyutong"
_REPO = "jetbrains-cc-gui"
_REGISTER_KEY = "jetbrains_cc_gui_2011_to_2011"

# Single-PR routing (DS3 multi-arch set). Dataset rows carry no number_interval, so without
# this the row would resolve to the "{org}/{repo}" catch-all config. Only this exact PR
# number of this repo is tagged with this file's interval; every other PR is unaffected.
from multi_swe_bench.harness import pull_request as _pull_request

_INTERVAL = _REGISTER_KEY
_BOUNDS = re.search(r"_(\d+)_to_(\d+)$", _INTERVAL)
_PR_MIN = int(_BOUNDS.group(1))
_PR_MAX = int(_BOUNDS.group(2))
_ROUTING_FLAG = "_" + _INTERVAL + "_routed"

if not getattr(_pull_request.PullRequest, _ROUTING_FLAG, False):
    _orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _routed_from_json(cls, json_str):
        pr = _orig_from_json(cls, json_str)
        if (
            pr.org == _ORG
            and pr.repo == _REPO
            and not pr.number_interval
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return pr

    _pull_request.PullRequest.from_json = classmethod(_routed_from_json)
    setattr(_pull_request.PullRequest, _ROUTING_FLAG, True)
_JDK_IMAGE = "eclipse-temurin:17-jdk-noble"


# ----------------------------------------------------------------------------
# Shell environment shared by prepare.sh and run_tests.sh.
# ----------------------------------------------------------------------------
_MSB_ENV = r"""# Sourced by prepare.sh and run_tests.sh -- one definition of the toolchain.
export CI=true
export GRADLE_OPTS="-Dorg.gradle.daemon=false -Dfile.encoding=UTF-8"
GRADLE="bash ./gradlew"
GRADLE_FLAGS="--no-daemon --continue --console=plain --no-configuration-cache -Dorg.gradle.jvmargs=-Xmx4g -I /home/msb_init.gradle -PskipWebview=true -x installAiBridgeDependencies"
MSB_TIMEOUT="timeout -k 60 1500"
"""

# Reads only the FILE LISTS from the two patch headers -- nothing is applied.
_MSB_TARGETS = r"""# Sourced by prepare.sh and run_tests.sh.
#   SEL         = project dirs owning a file the test patch touches under
#                 src/test/ or the fix patch under src/main/, restricted to dirs
#                 that have a Gradle build file on disk at this stage.
#   CLASSES     = fully-qualified names of the test sources the test patch touches
#                 in the graded source sets (src/test/ unless the config says more).
#   TEST_TASKS / COMPILE_TASKS = the matching "<project>:<task>" lists.
# A project the fix CREATES has no build file before the fix lands, so it is
# absent in the run/test stages: an honest NONE, never an unknown-task crash.
_msb_paths() {
    [ -f "$1" ] || return 0
    sed -nE 's#^diff --git a/.* b/(.*)$#\1#p' "$1"
}
_msb_has_build() { [ -f "$1/build.gradle" ] || [ -f "$1/build.gradle.kts" ]; }
_msb_add_sel() {
    case "$1" in MSB_NO_EXCLUDES) return 0 ;; esac
    case ",$SEL," in *",$1,"*) ;; *) SEL="${SEL:+$SEL,}$1" ;; esac
}
SEL=""
CLASSES=""
while IFS= read -r _p; do
    # Graded test source sets: test (an extended regex over src/<set>/).
    _m="$(printf '%s' "$_p" | sed -nE 's#^(.*)/src/(test)/.*$#\1#p; s#^src/(test)/.*$#.#p')"
    [ -n "$_m" ] || continue
    if _msb_has_build "$_m"; then _msb_add_sel "$_m"; fi
    _c="$(printf '%s' "$_p" | sed -nE 's#^(.*/)?src/(test)/(java|kotlin|groovy)/(.*)\.(java|kt|groovy)$#\4#p' | sed 's#/#.#g')"
    if [ -n "$_c" ]; then
        case ",$CLASSES," in *",$_c,"*) ;; *) CLASSES="${CLASSES:+$CLASSES,}$_c" ;; esac
    fi
done < <(_msb_paths /home/test.patch)
while IFS= read -r _p; do
    case "$_p" in
        src/main/*) _m=. ;;
        */src/main/*) _m="${_p%%/src/main/*}" ;;
        *) continue ;;
    esac
    if _msb_has_build "$_m"; then _msb_add_sel "$_m"; fi
done < <(_msb_paths /home/fix.patch)
TEST_TASKS=""
COMPILE_TASKS=""
for _m in ${SEL//,/ }; do
    if [ "$_m" = "." ]; then _proj=""; else _proj=":${_m//\//:}"; fi
    TEST_TASKS="$TEST_TASKS $_proj:test"
    COMPILE_TASKS="$COMPILE_TASKS $_proj:testClasses"
done
export MSB_TEST_CLASSES="$CLASSES"
"""

# Applied with -I to every Gradle invocation. Filters the Test tasks to the
# graded classes, never fails the build on a test failure or on a filter that
# matches nothing (a class the test patch adds does not exist yet in the run
# stage), and never lets a Test task be skipped as UP-TO-DATE -- a skipped task
# writes no fresh report and would read as zero tests.
_INIT_GRADLE = r"""def msbClasses = (System.getenv('MSB_TEST_CLASSES') ?: '').split(',').collect { it.trim() }.findAll { it }
allprojects {
    tasks.withType(Test).configureEach { t ->
        t.ignoreFailures = true
        t.outputs.upToDateWhen { false }
        t.filter.failOnNoMatchingTests = false
        msbClasses.each { c ->
            t.filter.includeTestsMatching(c)
            t.filter.includeTestsMatching(c + '$*')
        }
        t.reports.junitXml.required = true
    }
}
"""

# Prints every JUnit XML testcase as one tab-separated line. Parsing the XML
# with a real parser (instead of regex over concatenated XML) is what keeps
# <system-out> payloads, entities and multi-line display names from producing
# phantom or mangled test ids.
_EMIT_RESULTS = r"""import os
import sys
import xml.etree.ElementTree as ET

root = os.path.abspath(sys.argv[1])
rows = set()
for dp, dns, fns in os.walk(root):
    dns[:] = [d for d in dns if d not in (".git", "node_modules")]
    base = os.path.basename(dp)
    parent = os.path.dirname(dp)
    if base in ("surefire-reports", "failsafe-reports") and os.path.basename(parent) == "target":
        moddir = os.path.dirname(parent)
    elif os.path.basename(parent) == "test-results" and os.path.basename(os.path.dirname(parent)) == "build":
        moddir = os.path.dirname(os.path.dirname(parent))
    else:
        continue
    mod = os.path.relpath(moddir, root)
    if mod in ("", "."):
        mod = "."
    for fn in sorted(fns):
        if not (fn.startswith("TEST-") and fn.endswith(".xml")):
            continue
        try:
            tree = ET.parse(os.path.join(dp, fn))
        except ET.ParseError as exc:
            print("MSB_REPORT_PARSE_ERROR\t%s\t%s\t%s" % (mod, fn, exc))
            continue
        for tc in tree.iter("testcase"):
            cls = " ".join((tc.get("classname") or "").split())
            name = " ".join((tc.get("name") or "").split())
            if not cls or not name:
                continue
            tags = {child.tag for child in tc}
            if tags & {"failure", "error"}:
                status = "FAIL"
            elif "skipped" in tags:
                status = "SKIP"
            else:
                status = "PASS"
            rows.add((mod, cls, name, status))
for row in sorted(rows):
    print("MSB_TESTCASE\t" + "\t".join(row))
"""

_CHECK_GIT_CHANGES = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE = r"""#!/bin/bash
set -e

# ---- Section 1: PIN ---------------------------------------------------------
cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

# ---- Section 2: PROVISION (the tree may get dirty from here on) -------------
source /home/msb_env.sh
source /home/msb_targets.sh
echo "MSB_SEL=$SEL"
echo "MSB_CLASSES=$CLASSES"
if [ -z "$SEL" ]; then
    echo "MSB_NO_GRADED_PROJECT_AT_BASE: the test patch touches no src/test file of a Gradle project"
    exit 1
fi
# Compile the graded projects' main + test sources and everything they depend
# on: this downloads the Gradle distribution, the plugins and the dependencies.
timeout -k 60 3600 $GRADLE $GRADLE_FLAGS $COMPILE_TASKS
# Run the graded classes once at baseline so the test runtime classpath and the
# JUnit launcher artifacts are cached too. Tolerated: failures are expected and
# irrelevant here; the gate below re-checks the classpath offline.
timeout -k 60 3600 $GRADLE $GRADLE_FLAGS $TEST_TASKS || echo "MSB_WARMUP_TEST_RUN_EXIT=$?"
find . -type d -path '*/build/test-results' -prune -exec rm -rf {} +

# ---- Section 3: GATE (hard, never tolerated) --------------------------------
$GRADLE $GRADLE_FLAGS --offline -q $COMPILE_TASKS
echo "DEPS_OK"
"""



_RUN_TESTS = r"""#!/bin/bash
# The single test command shared verbatim by run.sh, test-run.sh and fix-run.sh.
# Not `set -e`: a failing compile or test run is data to report, not a reason to
# stop before the results are printed. Real runner failures are surfaced by the
# exit status at the end, never swallowed.
set -o pipefail
cd /home/@@REPO@@
source /home/msb_env.sh
source /home/msb_targets.sh
echo "MSB_SEL=$SEL"
echo "MSB_CLASSES=$CLASSES"
if [ -z "$SEL" ] || [ -z "$CLASSES" ]; then
    echo "MSB_NO_TEST_TARGETS_AT_THIS_STAGE"
    exit 0
fi

# Reports left by prepare.sh or an earlier stage must never be replayed here.
find . -type d -path '*/build/test-results' -prune -exec rm -rf {} + 2>/dev/null

# The patch-touched test methods of one gold test file, read from the test patch.
# Per hunk: added declarations credit only themselves (git's hunk header names
# the PRECEDING method when code is appended, so trusting it there credits an
# innocent neighbour); a hunk that only edits a body credits the enclosing
# method its header names. A declaration counts when it carries a JUnit test
# annotation or follows the JUnit 3 test* naming. Lifecycle methods are dropped.
_gold_methods() {
    awk -v rel="$1" '
        function mname(s,   m) {
            if (match(s, /(^|[^A-Za-z0-9_$.])void[ \t]+[A-Za-z_$][A-Za-z0-9_$]*[ \t]*\(/)) {
                m = substr(s, RSTART, RLENGTH); sub(/^.*void[ \t]+/, "", m); sub(/[ \t]*\($/, "", m); return m
            }
            if (match(s, /(^|[^A-Za-z0-9_])fun[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]*\(/)) {
                m = substr(s, RSTART, RLENGTH); sub(/^.*fun[ \t]+/, "", m); sub(/[ \t]*\($/, "", m); return m
            }
            return ""
        }
        function istest(s) { return (s ~ /@(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)([^A-Za-z0-9_]|$)/) }
        function flush(   i) {
            if (nadd > 0) { for (i = 1; i <= nadd; i++) print addl[i] }
            else if (ctx != "") { print ctx }
            nadd = 0; ctx = ""; annot = 0
        }
        BEGIN { tail = " b/" rel }
        index($0, "diff --git ") == 1 { flush(); inf = (substr($0, length($0) - length(tail) + 1) == tail); next }
        !inf { next }
        index($0, "@@") == 1 { flush(); h = $0; sub(/^@@[^@]*@@[ \t]*/, "", h); ctx = mname(h); next }
        index($0, "+++") == 1 || index($0, "---") == 1 { next }
        index($0, "+") == 1 {
            if (istest($0)) annot = 1
            m = mname($0)
            if (m != "") { if (annot || m ~ /^test/) addl[++nadd] = m; annot = 0 }
            next
        }
        index($0, " ") == 1 { if (istest($0)) annot = 1; else if (mname($0) != "") annot = 0; next }
        END { flush() }
    ' /home/test.patch | sort -u \
        | grep -Ev '^(setUp|setup|tearDown|teardown|before|after|beforeEach|afterEach|beforeAll|afterAll|init|initMocks)$' || true
}

# Compile the graded modules' tests first. A gold test source that does not
# compile at this stage (the test stage, before the fix exists) would otherwise
# take its whole module down with it; drop just that file and retry, and report
# its patch-touched methods as failures below.
DROPPED=""
for _attempt in 1 2 3 4 5 6; do
    _rc=0
    _out="$($MSB_TIMEOUT $GRADLE $GRADLE_FLAGS $COMPILE_TASKS 2>&1)" || _rc=$?
    if [ "$_rc" -eq 0 ]; then
        break
    fi
    _bad="$(printf '%s\n' "$_out" \
        | sed -nE -e 's#^(/home/@@REPO@@/([^:]*/)?src/test/[^:]*\.java):[0-9]+: error:.*#\1#p' \
                  -e 's#^e: (file://)?(/home/@@REPO@@/([^:]*/)?src/test/[^:]*\.kt):.*#\2#p' \
        | sort -u)"
    if [ -z "$_bad" ]; then
        echo "MSB_TEST_COMPILE_FAILED_OUTSIDE_TEST_SOURCES (attempt $_attempt):"
        printf '%s\n' "$_out" | grep -E -A3 'error:|^e: |What went wrong' | head -60
        break
    fi
    echo "MSB_DROPPED_UNCOMPILABLE_TEST_SOURCES (attempt $_attempt):"
    printf '%s\n' "$_bad" | while IFS= read -r _l; do
        [ -n "$_l" ] || continue
        echo "  $_l"
        printf '%s\n' "$_out" | grep -F "$_l" | head -8 | sed 's/^/    /'
    done
    DROPPED="$DROPPED $_bad"
    printf '%s\n' "$_bad" | xargs -r rm -f
done

rc=0
$MSB_TIMEOUT $GRADLE $GRADLE_FLAGS $TEST_TASKS || rc=$?

python3 /home/msb_emit_results.py /home/@@REPO@@

# A gold test source that does not compile is a FAILING test, not an absent one.
for _f in $DROPPED; do
    _rel="${_f#/home/@@REPO@@/}"
    case "$_rel" in src/test/*) _mod=. ;; *) _mod="${_rel%%/src/test/*}" ;; esac
    _fq="$(printf '%s' "$_rel" | sed -E 's#^(.*/)?src/test/(java|kotlin|groovy)/##; s#\.(java|kt|groovy)$##; s#/#.#g')"
    case ",$CLASSES," in *",$_fq,"*) ;; *) continue ;; esac
    _methods="$(_gold_methods "$_rel")"
    if [ -z "$_methods" ]; then
        echo "MSB_SYNTHETIC_FAILURES_SKIPPED $_fq (no patch-touched test method found)"
        continue
    fi
    echo "MSB_SYNTHESIZED_COMPILE_FAILURES $_fq"
    for _m in $_methods; do
        printf 'MSB_TESTCASE\t%s\t%s\t%s\tFAIL\n' "$_mod" "$_fq" "$_m"
    done
done

if [ "$rc" -ne 0 ]; then
    echo "MSB_TEST_RUNNER_DID_NOT_COMPLETE exit=$rc (results above may be partial)"
    exit "$rc"
fi
"""


_APPLY_FN = r"""# Apply one patch file. Each file gets its own context ladder: full context
# first; only if the base commit moved under the patch's context (the PR
# branch was cut from an older parent than base.sha) retry with 2, then 1,
# lines. git apply is atomic per file, so a retry never meets half-applied
# hunks of the same file; if no attempt applies, the stage fails loudly.
# Entries of the form "Binary files A and B differ" (the patch was exported
# without --binary) carry no bytes at all and can never apply; only those
# paths are excluded, every other hunk is applied as-is.
_msb_apply() {
    local _ex=() _f
    while IFS= read -r _f; do
        [ -n "$_f" ] && _ex+=("--exclude=$_f")
    done < <(sed -nE 's#^Binary files (.*) and (.*) differ$#\1\n\2#p' "$1" \
                 | grep -v '^/dev/null$' | sed -E 's#^[ab]/##' | sort -u)
    git apply --whitespace=nowarn "${_ex[@]}" "$1" \
        || git apply --whitespace=nowarn -C2 "${_ex[@]}" "$1" \
        || git apply --whitespace=nowarn -C1 "${_ex[@]}" "$1"
}
"""


def _apply(patches: str) -> str:
    return "\n".join(f"_msb_apply {p}" for p in patches.split())


def _stage(apply_line: str) -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "export CI=true\n"
        "\n"
        + (_APPLY_FN + "\n" if apply_line else "")
        + "cd /home/@@REPO@@\n"
        + (apply_line + "\n" if apply_line else "")
        + "bash /home/run_tests.sh\n"
    )


_RUN = _stage("")
_TEST_RUN = _stage(_apply("/home/test.patch"))
_FIX_RUN = _stage(_apply("/home/test.patch /home/fix.patch"))


def _render(body: str, pr: PullRequest) -> str:
    """Fill @@...@@ placeholders. Placeholders rather than str.format(): these
    bodies are full of ${VAR}, $(...) and awk braces."""
    return body.replace("@@REPO@@", pr.repo).replace("@@SHA@@", pr.base.sha)


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
        # build.gradle sets the Java toolchain languageVersion 17 at base commit fdf0dbe5c3 (PR 2011); running Gradle on JDK 17 satisfies it without a toolchain download.
        # Official multi-arch (amd64 + arm64) Ubuntu 24.04 image.
        return _JDK_IMAGE

    def image_tag(self) -> str:
        # PR-scoped: the default dockerfile() pins ${BASE_COMMIT} and scrubs
        # history in this layer, so it must never be shared across PRs.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["unzip", "zip"]

    def extra_setup(self) -> str:
        return ""


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
        pr = self.pr
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "msb_env.sh", _render(_MSB_ENV, pr)),
            File(".", "msb_targets.sh", _render(_MSB_TARGETS, pr)),
            File(".", "msb_init.gradle", _INIT_GRADLE),
            File(".", "msb_emit_results.py", _EMIT_RESULTS),
            File(".", "prepare.sh", _render(_PREPARE, pr)),
            File(".", "run_tests.sh", _render(_RUN_TESTS, pr)),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_TEST_RUN, pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN, pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


_CASE_RE = re.compile(r"^MSB_TESTCASE\t([^\t\n]*)\t([^\t\n]*)\t([^\t\n]*)\t(PASS|FAIL|SKIP)[ \t\r]*$", re.MULTILINE)
_DIFF_B_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SRC_EXT = (".java", ".kt", ".groovy")


def _gold_test_files(test_patch: str) -> list[str]:
    out = []
    for path in _DIFF_B_RE.findall(test_patch or ""):
        if ("/src/test/" in path or path.startswith("src/test/")) and path.endswith(_SRC_EXT):
            out.append(path)
    return sorted(set(out))


def _test_id(mod: str, cls: str, name: str, gold_files: list[str]) -> str:
    """"<repo-relative test source>::<method>" -- the path-embedded id shape
    report.py's matchers split on "::", so a credited test maps back to exactly
    the file the test patch touches (n2p detection, cheating guard). A nested
    class keeps its own segment so Outer/Inner methods never collide."""
    outer, _, nested = cls.partition("$")
    rel = outer.replace(".", "/")
    prefix = "" if mod == "." else mod + "/"
    path = None
    for f in gold_files:
        stem = os.path.splitext(f)[0]
        if f.startswith(prefix) and stem.endswith("/" + rel):
            path = f
            break
    if path is None:
        path = prefix + "src/test/java/" + rel + ".java"
    method = name[:-2] if name.endswith("()") else name
    return f"{path}::{nested}::{method}" if nested else f"{path}::{method}"


@Instance.register(_ORG, _REGISTER_KEY)
class JetbrainsCcGui(Instance):
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
        clean = _ANSI_RE.sub("", test_log)
        gold_files = _gold_test_files(self.pr.test_patch)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for mod, cls, name, status in _CASE_RE.findall(clean):
            test_id = _test_id(mod, cls, name, gold_files)
            if status == "FAIL":
                failed_tests.add(test_id)
            elif status == "SKIP":
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

        # Failure wins over pass, pass wins over skip: an id observed failing is
        # never credited as passing, and the three buckets never overlap.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
