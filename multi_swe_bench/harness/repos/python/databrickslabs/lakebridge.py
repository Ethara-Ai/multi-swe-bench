import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


# apply_patch.sh -- plain `git apply` first, `--3way` only as an announced
# fallback. Which path applied matters: FLOW VERDICT DISCIPLINE requires a
# failure to be counted from the PRIMARY apply, so a silent --3way rescue would
# hide a genuinely broken patch. Measured on this dataset: both patches apply
# cleanly with plain `git apply`, 0 binary hunks, so neither fallback fires.
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/lakebridge
EXCL=/tmp/excl.$$
restore_binaries() {
  local patch="$1" path="" new=""
  : > "$EXCL"
  while IFS= read -r line; do
    case "$line" in
      "diff --git "*) path="${line#*" b/"}" ;;
      "index "*)      new="${line#*..}"; new="${new%% *}" ;;
      "Binary files "*)
        printf -- '--exclude=%s\\n' "$path" >> "$EXCL"
        if [[ "$new" =~ ^0+$ ]]; then rm -f "$path"
        elif git cat-file -e "$new" 2>/dev/null; then
          mkdir -p "$(dirname "$path")"; git cat-file blob "$new" > "$path"
        else
          echo "apply_patch: WARNING blob $new for $path not available"
        fi ;;
    esac
  done < "$patch"
}
for patch in "$@"; do
  restore_binaries "$patch"
  EX=()
  if [ -s "$EXCL" ]; then mapfile -t EX < "$EXCL"; fi
  if ! git apply --whitespace=nowarn "${EX[@]}" "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "${EX[@]}" "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
rm -f "$EXCL"
"""


# conftest_report.py -- a pytest plugin that records one line per test.
#
# WHY NOT PARSE pytest's CONSOLE OUTPUT: this project's own pyproject.toml sets
#
#     addopts = "-s -p no:warnings -vv --cache-clear"
#
# so every result line carries a trailing progress percentage:
#
#     tests/unit/transpiler/test_execute.py::test_x PASSED   [ 11%]
#
# That percentage MOVES when the test count changes -- and this PR's test patch
# adds 3 tests, so every percentage after them shifts between the test and fix
# acts. Folding that varying suffix into the test name would manufacture a false
# transition for every test in the file (audit 4B).
#
# The plugin records the nodeid and the outcome directly, so a name is
# byte-identical across all three acts. It is loaded with `-p` from /home, so it
# never lands in the work tree and cannot dirty `git status` (FLOW Issue 7).
_CONFTEST_REPORT_PY = '''"""pytest plugin: write `<nodeid>\\t<outcome>` per test to $PYTEST_REPORT_FILE."""
import os


def pytest_configure(config):
    config._mswe_path = os.environ.get("PYTEST_REPORT_FILE", "/tmp/pytest_report.tsv")
    config._mswe_seen = {}


def pytest_runtest_logreport(report):
    cfg = getattr(report, "config", None)
    # `when` matters: a test that errors during setup never reaches "call", and
    # must still be recorded -- otherwise it silently vanishes from the results
    # and looks like it was never collected (the shape FLOW GATE 0 catches).
    store = _store()
    if store is None:
        return
    nodeid = report.nodeid
    if report.when == "call":
        store[nodeid] = "passed" if report.passed else ("skipped" if report.skipped else "failed")
    elif report.when in ("setup", "teardown"):
        if report.failed:
            store[nodeid] = "failed"
        elif report.skipped and nodeid not in store:
            store[nodeid] = "skipped"


_STORE = {}


def _store():
    return _STORE


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("PYTEST_REPORT_FILE", "/tmp/pytest_report.tsv")
    with open(path, "w", encoding="utf-8") as fh:
        for nodeid, outcome in sorted(_STORE.items()):
            fh.write("%s\\t%s\\n" % (nodeid, outcome))
'''


# pytest_test_report.py -- convert the plugin's TSV into the trailing-keyword
# lines parse_log reads. Exit status mirrors a runner: 0 all passed, 1 at least
# one failed, 2 the run produced NO tests at all (the runner never started --
# the case FLOW GATE 0 exists to catch, and which must never be confused with
# "zero tests passed").
_PYTEST_TEST_REPORT_PY = '''"""Turn the conftest plugin's TSV into per-test result lines.

usage: pytest_test_report.py <tsv>

    pytest:tests/unit/transpiler/test_execute.py::test_encoding_error_lookup_error PASSED
"""
import sys

STATUS = {"passed": "PASSED", "failed": "FAILED", "skipped": "SKIPPED"}


def main():
    path = sys.argv[1]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            rows = [l.rstrip("\\n").split("\\t") for l in fh if l.strip()]
    except OSError as exc:
        sys.stderr.write("pytest_test_report: cannot read %s: %s\\n" % (path, exc))
        return 2

    if not rows:
        sys.stderr.write(
            "pytest_test_report: the run reported no tests; the runner never started\\n")
        return 2

    failed = False
    for row in rows:
        if len(row) != 2:
            continue
        nodeid, outcome = row
        status = STATUS.get(outcome, "FAILED")
        print("pytest:%s %s" % (nodeid, status))
        if status == "FAILED":
            failed = True

    sys.stderr.write("pytest_test_report: %d test(s) recorded\\n" % len(rows))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


# run_tests.sh -- the graded command, identical in all three acts.
#
# SCOPE: `tests/unit/transpiler/`, not the whole `tests/unit/` tree. This is
# deliberate and it is about ARCHITECTURE, not speed.
#
# `tests/unit/test_cli_analyze.py` drives `databricks-bb-analyzer`, whose bundled
# binary is x86-64 ONLY:
#
#   .../bladespector/Analyzer/Linux/analyzer:
#       ELF 64-bit LSB executable, x86-64 ... stripped
#
# On aarch64 it dies with SIGTRAP and those 3 tests fail; on amd64 they pass. The
# full suite's result set is therefore ARCH-DEPENDENT, which is unacceptable for
# an image published for both platforms -- the same dataset entry would describe
# two different outcomes. Measured full suite: 791 passed / 3 failed (arm64).
#
# `tests/unit/transpiler/` contains no reference to that analyzer (verified by
# grep), runs in ~8s, and holds EVERY test this PR touches, so the scope loses no
# f2p evidence while keeping 499 passing tests as p2p guards.
#
# `set -e` is absent (only -uo pipefail): pytest exits non-zero whenever a test
# fails, which is EXPECTED in the test act by design. Under -e the act would
# abort before the report script ran and score a silent 0/0/0 (GATE 0).
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/lakebridge
# PYTHONPATH=/home is REQUIRED, not cosmetic: `-p conftest_report` imports the
# plugin as a MODULE, and the acts run from /home/lakebridge, so /home is not on
# sys.path by default. Without it pytest aborts before collecting anything --
#   ImportError: Error importing plugin "conftest_report": No module named ...
# which produces a 0/0/0 act (measured; GATE 0 caught it). The plugin lives in
# /home rather than the work tree precisely so it cannot dirty `git status`
# (FLOW Issue 7), which is what makes this export necessary.
export PYTHONPATH=/home
export PYTEST_REPORT_FILE=/tmp/pytest_report.tsv
rm -f "$PYTEST_REPORT_FILE"
python -m pytest tests/unit/transpiler/ -p conftest_report --timeout=300
pytest_rc=$?
echo "pytest exit=${pytest_rc}"
echo "----- per-test results -----"
python /home/pytest_test_report.py "$PYTEST_REPORT_FILE"
"""


class LakebridgeImageBase(Image):
    """Shared base image, one per PR (`base-pr-<N>`).

    dockerfile() is deliberately NOT overridden: the harness's own
    Image.dockerfile() already emits the mandated order (FROM -> apt -> clone ->
    WORKDIR -> reset -> checkout ${BASE_COMMIT} -> extra_setup -> hardening
    scrub + its four assertions -> CMD), and DockerfileEnhancer then prepends the
    BuildKit directive, the build ARGs (BASE_COMMIT left EMPTY), the env block,
    the labels and the cert links.

    The tag carries the PR number because the base's CONTENT is per-PR (it is
    checked out at that PR's base commit). A shared `:base` tag would be one name
    for two different images -- FLOW Issue 25.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    # python:3.10 is the project's OWN pin, not a guess. pyproject.toml declares
    # `requires-python = ">=3.10"` and the hatch default env pins `python="3.10"`.
    # -bookworm (not -slim) because the dependency tree compiles C extensions
    # (numpy, pandas, pyspark's py4j) and the slim image lacks the headers; the
    # full image builds them without an apt round trip. Pinned, not :latest.
    def dependency(self) -> Union[str, "Image"]:
        return "python:3.10-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # The harness already installs ca-certificates, curl, build-essential,
        # git, gnupg, make, python3, sudo and wget. build-essential covers the C
        # extensions; this suite needs no database, browser or JDK.
        return []

    def extra_setup(self) -> str:
        # Runs AFTER `git checkout ${BASE_COMMIT}`, so pyproject.toml is this
        # PR's own.
        #
        # Two installs, and the split is deliberate:
        #
        # 1. `pip install -e .` -- the package under test, editable so the fix
        #    patch's edit to src/databricks/labs/lakebridge/transpiler/execute.py
        #    takes effect without reinstalling between acts.
        #
        # 2. The dev/test dependencies, taken verbatim from the project's own
        #    [tool.hatch.envs.default] block rather than invented. `databricks-connect`
        #    is NOT optional decoration: tests/conftest.py does
        #    `from pyspark.sql import DataFrame` at import time, so WITHOUT it
        #    pytest cannot even collect -- measured:
        #        ModuleNotFoundError: No module named 'pyspark'
        #    pytest-asyncio is required because pyproject sets
        #    `asyncio_mode = "auto"`, and pytest-timeout because the graded
        #    command passes --timeout.
        #
        # NOT `|| true`: this is the toolchain, not a cache. A warm-up that fails
        # quietly ships an image whose acts cannot run (FLOW Issue 14, GATE 1).
        # The import probes below prove it took effect and make GATE 1 mechanical.
        return (
            "RUN python -m pip install --no-cache-dir --upgrade pip && \\\n"
            "    python -m pip install --no-cache-dir -e . && \\\n"
            "    python -m pip install --no-cache-dir \\\n"
            '        "pytest~=8.3.5" "pytest-asyncio~=0.26.0" "pytest-xdist~=3.5.0" \\\n'
            '        "pytest-timeout~=2.4.0" "databricks-connect==15.1" \\\n'
            '        "databricks-labs-pytester>=0.3.0" "numpy~=1.26.4" \\\n'
            '        "pandas~=2.3.1" "cattrs>=25.2.0" && \\\n'
            "    python -c 'import pyspark, pytest, pytest_asyncio' && \\\n"
            "    python -c 'import databricks.labs.lakebridge as m; print(m.__file__)' && \\\n"
            "    python -m pytest tests/unit/transpiler/ --collect-only -q > /tmp/collect.txt && \\\n"
            "    tail -1 /tmp/collect.txt"
        )


class LakebridgeImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and the act scripts, run
    prepare.sh -- and nothing else. The clone, the checkout and the history scrub
    already happened in the base."""

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
        return LakebridgeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH),
            File(".", "conftest_report.py", _CONFTEST_REPORT_PY),
            File(".", "pytest_test_report.py", _PYTEST_TEST_REPORT_PY),
            File(".", "run_tests.sh", _RUN_TESTS_SH),
            # The warm-up is a non-destructive IMPORT PROBE, never a reinstall.
            # `pip install -e .` here would be pointless (already done in the
            # base) and a plain `pip install` could REPLACE the editable install,
            # so the fix patch's source edit would stop taking effect -- the
            # Python analogue of the `npm ci` that wiped a sibling config's
            # node_modules and produced a silent 0/0/0.
            #
            # `git clean -fdq` is required, not decorative: pytest writes
            # .pytest_cache and __pycache__ into the tree, and without the clean
            # the next act aborts on a dirty tree (FLOW Issue 4). Neither is
            # gitignored away by the acts, so the clean is what keeps
            # check_git_changes.sh green.
            #
            # The asserts are the guard: if the base's site-packages were ever
            # lost, the IMAGE BUILD fails loudly instead of the acts quietly
            # scoring nothing (GATE 0).
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh
python -m pip --version || true
python --version
python -c 'import pyspark, pytest, pytest_asyncio'
python -c 'import databricks.labs.lakebridge'
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/lakebridge
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/lakebridge
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/lakebridge
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh

""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image_name}

{copies}
RUN bash /home/prepare.sh

"""


@Instance.register("databrickslabs", "lakebridge")
class Lakebridge(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LakebridgeImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured status keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section the report script printed, when present.
        # pytest's own -vv output shares the log and its lines END in the same
        # status words, so a whole-log scan would double-count every test AND
        # absorb the trailing progress percentage into the name. The fallback to
        # the whole text keeps a bare sequence of result lines parseable (the
        # config audit's 4C probe has no marker).
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what pytest_test_report.py prints. The
        # name is captured non-greedily BEFORE the keyword, so no percentage or
        # duration can leak in and manufacture a false transition between acts.
        result_res = [
            (re.compile(r"^(.+?)\s+PASSED$"), "pass"),
            (re.compile(r"^(.+?)\s+FAILED$"), "fail"),
            (re.compile(r"^(.+?)\s+SKIPPED$"), "skip"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            for rx, kind in result_res:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                if kind == "pass":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif kind == "fail":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                else:
                    skipped_tests.add(name)
                break

        # TestResult requires the three sets to be disjoint, else it raises.
        # Reconciliation resolves toward the CONSERVATIVE outcome -- a name seen
        # both passing and skipping is counted as skipped, never claimed as a pass.
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
