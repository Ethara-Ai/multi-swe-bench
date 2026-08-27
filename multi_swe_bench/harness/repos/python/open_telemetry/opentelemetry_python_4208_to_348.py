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


# apply_patch.sh -- plain `git apply` first, `--3way` only as an ANNOUNCED
# fallback. FLOW VERDICT DISCIPLINE requires a failure to be counted from the
# PRIMARY apply, so a silent --3way rescue would hide a genuinely broken patch.
# This PR's test patch contains a RENAME (see _RUN_TESTS_SH), which `git apply`
# handles natively. Measured: both patches apply cleanly, 0 binary hunks.
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/opentelemetry-python
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


# conftest_report.py -- a pytest plugin recording one line per test.
#
# WHY NOT PARSE pytest's CONSOLE OUTPUT: every `-v` result line carries a
# trailing progress percentage --
#     .../test_otcollector_exporter.py::Test::test_x PASSED  [ 60%]
# -- and that percentage MOVES when the test count changes. This PR takes the
# suite from 5 tests to 10, so every percentage shifts between acts. Folding it
# into the name would manufacture a false transition for every test (audit 4B).
#
# The plugin records the nodeid and outcome directly, so a name is
# byte-identical across acts. It lives in /home, never the work tree, so it
# cannot dirty `git status` (FLOW Issue 7) -- which is why run_tests.sh must
# export PYTHONPATH=/home for `-p conftest_report` to import it.
_CONFTEST_REPORT_PY = '''"""pytest plugin: write `<nodeid>\\t<outcome>` per test to $PYTEST_REPORT_FILE."""
import os

_STORE = {}


def pytest_runtest_logreport(report):
    # `when` matters: a test that errors during SETUP never reaches "call" and
    # must still be recorded, or it silently vanishes and looks uncollected --
    # the shape FLOW GATE 0 exists to catch.
    nodeid = report.nodeid
    if report.when == "call":
        _STORE[nodeid] = "passed" if report.passed else ("skipped" if report.skipped else "failed")
    elif report.when in ("setup", "teardown"):
        if report.failed:
            _STORE[nodeid] = "failed"
        elif report.skipped and nodeid not in _STORE:
            _STORE[nodeid] = "skipped"


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("PYTEST_REPORT_FILE", "/tmp/pytest_report.tsv")
    with open(path, "w", encoding="utf-8") as fh:
        for nodeid, outcome in sorted(_STORE.items()):
            fh.write("%s\\t%s\\n" % (nodeid, outcome))
'''


_PYTEST_TEST_REPORT_PY = '''"""Turn the conftest plugin's TSV into per-test result lines.

usage: pytest_test_report.py <tsv>

    pytest:ext/.../tests/test_otcollector_metrics_exporter.py::Test::test_export PASSED

Exit 0 all passed, 1 at least one failed, 2 the run produced NO tests at all
(the runner never started -- the case FLOW GATE 0 exists to catch, and which
must never be confused with "zero tests passed").
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
# `--continue-on-collection-errors` IS LOAD-BEARING, not tidiness. This PR's
# test patch adds test_otcollector_metrics_exporter.py, which imports
# `metrics_exporter` -- a module only the FIX patch creates. Without the flag,
# pytest treats that ONE uncollectable file as fatal for the WHOLE session:
#
#   ImportError: cannot import name 'metrics_exporter' ...
#   !!!! Interrupted: 1 error during collection !!!!
#   ===== no tests collected, 1 error =====
#
# i.e. a flat 0/0/0 test act that discards the 5 OTHER tests which ran fine --
# textbook FLOW **Issue 28**. With the flag: `5 passed, 1 error`, and the
# genuinely-blocked file is the only thing lost.
#
# NOTE the test patch also RENAMES test_otcollector_exporter.py ->
# test_otcollector_trace_exporter.py. pytest nodeids embed the file path, so
# those 5 tests carry different names before and after. They are NOT lost
# evidence: they pass in the test act under the new name, so the classifier's
# `test == PASS -> p2p` branch takes them, and they do not inflate n2p
# (FLOW Issues 9 / 27).
#
# `set -e` is absent (only -uo pipefail): pytest exits non-zero on a collection
# error, which is EXPECTED in the test act. Under -e the act would abort before
# the report script ran and score a silent 0/0/0 (GATE 0).
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/opentelemetry-python
export PYTHONPATH=/home
export PYTEST_REPORT_FILE=/tmp/pytest_report.tsv
rm -f "$PYTEST_REPORT_FILE"

# SCOPE IS ERA-ADAPTIVE, and it has to be. This file registers the PLAIN key, and
# prep_dataset.py strips number_interval (FLOW STEP 1), so EVERY opentelemetry-python
# entry resolves here -- not just the PR this recipe was first measured on. The repo
# was reorganised between eras:
#
#   2020 (PR #454 base 4b6a52d6) : ext/opentelemetry-ext-otcollector/...   <- exists
#   present day                   : ext/ IS GONE -> exporter/ propagator/ shim/
#
# A hardcoded `pytest ext/opentelemetry-ext-otcollector/tests/` therefore collects
# ZERO tests on any later PR. The detection below keeps the narrow, fully-measured
# scope for the era that has it, and falls back to the repo-wide layout otherwise.
TARGETS=""
if [ -d ext/opentelemetry-ext-otcollector/tests ]; then
    TARGETS="ext/opentelemetry-ext-otcollector/tests/"
else
    for d in ext/*/tests exporter/*/tests propagator/*/tests shim/*/tests \
             instrumentation/*/tests opentelemetry-api/tests opentelemetry-sdk/tests tests; do
        [ -d "$d" ] && TARGETS="$TARGETS $d"
    done
fi
echo "pytest targets: ${TARGETS}"

python -m pytest ${TARGETS} -p conftest_report --continue-on-collection-errors
pytest_rc=$?
echo "pytest exit=${pytest_rc}"
echo "----- per-test results -----"
python /home/pytest_test_report.py "$PYTEST_REPORT_FILE"
"""


class OpentelemetryPythonImageBase(Image):
    """Shared base image, one per PR (`base-pr-<N>`).

    The previous version of this config was SINGLE-STAGE -- one `ImageDefault`
    with `image_tag() -> pr-<N>` and no base layer at all, so every act rebuilt
    the whole environment. This splits it into the mandated two stages.

    dockerfile() is deliberately NOT overridden: the harness's own
    Image.dockerfile() emits FROM -> apt -> clone -> WORKDIR -> reset ->
    checkout ${BASE_COMMIT} -> extra_setup -> scrub + its four assertions -> CMD,
    and DockerfileEnhancer prepends the BuildKit directive, the ARGs
    (BASE_COMMIT left EMPTY), the env block, labels and cert links.

    The tag carries the PR number because the base's CONTENT is per-PR -- a
    shared `:base` tag would be one name for two different images (FLOW Issue 25).
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

    # base_commit is 2020-03-10 and setup.py declares `python_requires >= 3.4`
    # with CI matrices naming 3.6/3.8. 3.8 is the newest interpreter for which
    # this 2020 dependency stack still resolves. -bookworm (not -slim) because
    # grpcio has no wheel for this pin on arm64 and compiles from source.
    def dependency(self) -> Union[str, "Image"]:
        return "python:3.8-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # The harness already installs ca-certificates, curl, build-essential,
        # git, gnupg, make, python3, sudo and wget. build-essential covers
        # grpcio's C extension; nothing further is needed.
        return []

    def extra_setup(self) -> str:
        # Runs AFTER `git checkout ${BASE_COMMIT}`, so the tree is this PR's own.
        #
        # EVERYTHING ERA-SPECIFIC HERE IS CONDITIONAL, and it must be. This file
        # registers the PLAIN key and prep_dataset.py strips number_interval, so
        # EVERY opentelemetry-python entry resolves here -- across a repo that was
        # reorganised between eras (2020: `ext/...`; today: `ext/` is gone,
        # replaced by `exporter/ propagator/ shim/`). A hardcoded
        # `pip install -e ./ext/opentelemetry-ext-otcollector` would fail the IMAGE
        # BUILD outright on any later PR.
        #
        # THE protobuf PIN, and why it is gated rather than global: the otcollector
        # extension declares `protobuf >= 3.8.0` with NO upper bound, so a fresh
        # install today pulls protobuf 5.x and the 2020-era generated _pb2.py files
        # cannot be read by it --
        #     TypeError: Descriptors cannot not be created directly.
        #     -> "no tests collected, 1 error"
        # Measured: unpinned -> protobuf 5.29.6, 0 tests collectable;
        #           `protobuf<3.20` -> 3.19.6, 5 tests collected at base_commit.
        # But a MODERN otel PR needs modern protobuf, so the pin is applied only
        # when the 2020-era layout is actually present.
        #
        # NOT `|| true` on the core installs: this is the toolchain, not a cache.
        # A warm-up that fails quietly ships an image whose acts cannot run
        # (FLOW Issue 14, GATE 1). The collect probe at the end proves it took
        # effect and makes GATE 1 mechanical.
        #
        # RESIDUAL LIMIT, stated plainly: `dependency()` returns ONE base image, so
        # the interpreter cannot adapt per era. python:3.8 suits the 2020 era this
        # recipe was measured on; a recent otel PR would need a newer interpreter.
        # Spanning 2020..today properly requires era-ranged configs -- which is what
        # the five sibling `opentelemetry_python_*_to_*.py` files exist for.
        return (
            "RUN set -eux; \\\n"
            "    python -m pip install --no-cache-dir --upgrade pip; \\\n"
            "    python -m pip install --no-cache-dir -e ./opentelemetry-api; \\\n"
            "    python -m pip install --no-cache-dir -e ./opentelemetry-sdk; \\\n"
            "    if [ -d ext/opentelemetry-ext-otcollector ]; then \\\n"
            "        python -m pip install --no-cache-dir -e ./ext/opentelemetry-ext-otcollector; \\\n"
            '        python -m pip install --no-cache-dir "protobuf<3.20"; \\\n'
            "    elif [ -f scripts/eachdist.py ]; then \\\n"
            "        python scripts/eachdist.py develop; \\\n"
            "    elif [ -f pyproject.toml ] || [ -f setup.py ]; then \\\n"
            "        python -m pip install --no-cache-dir -e .; \\\n"
            "    fi; \\\n"
            '    python -m pip install --no-cache-dir "pytest~=7.4"; \\\n'
            "    python -c 'import pytest; print(pytest.__version__)'; \\\n"
            "    python -c 'import opentelemetry.sdk'; \\\n"
            "    if [ -d ext/opentelemetry-ext-otcollector ]; then \\\n"
            "        python -c 'import google.protobuf, grpc; print(google.protobuf.__version__)'; \\\n"
            "        python -c 'from opentelemetry.ext.otcollector import trace_exporter'; \\\n"
            "    fi"
        )


class OpentelemetryPythonImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and the act scripts, run
    prepare.sh -- and nothing else. The clone, the checkout and the history
    scrub already happened in the base."""

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
        return OpentelemetryPythonImageBase(self.pr, self._config)

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
            # A plain `pip install` here could REPLACE the base's EDITABLE
            # install, after which the fix patch's edit to
            # src/opentelemetry/ext/otcollector/metrics_exporter/__init__.py
            # would stop taking effect -- the Python analogue of the `npm ci`
            # that wiped a sibling config's node_modules and scored a silent
            # 0/0/0.
            #
            # `git clean -fdq` is required, not decorative: pytest writes
            # .pytest_cache/ and __pycache__/ into the tree, and the editable
            # installs leave *.egg-info/ and *.egg-link artefacts. Without the
            # clean the next act aborts on a dirty tree (FLOW Issue 4).
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
python -c 'import pytest, opentelemetry.sdk'
if [ -d ext/opentelemetry-ext-otcollector ]; then
  python -c 'import google.protobuf, grpc'
  python -c 'from opentelemetry.ext.otcollector import trace_exporter'
fi
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

cd /home/opentelemetry-python
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/opentelemetry-python
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

cd /home/opentelemetry-python
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


@Instance.register("open-telemetry", "opentelemetry-python")
class OpentelemetryPython(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpentelemetryPythonImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first (the previous version omitted this -- audit 4C): a coloured
        # status keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section the report script printed, when present.
        # pytest's own output shares the log and its lines END in the same status
        # words, so a whole-log scan would double-count every test AND absorb the
        # trailing progress percentage into the name. The fallback to the whole
        # text keeps a bare sequence of result lines parseable (the config
        # audit's 4C probe has no marker).
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

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
        # Resolved toward the CONSERVATIVE outcome -- a name seen both passing
        # and skipping is counted as skipped, never claimed as a pass.
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
