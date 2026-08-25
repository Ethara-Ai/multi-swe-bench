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


# apply_patch.sh -- the dataset's diffs carry an index hash but no binary
# payload. Measured on this dataset: 0 binary hunks in both patches, so the
# restore path never fires here; it is kept because it costs nothing and this
# repo ships genetic-map archives a future PR could touch. Plain `git apply`
# first, `--3way` only as a fallback -- the primary apply is what a failure must
# be counted from (FLOW VERDICT DISCIPLINE).
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/stdpopsim
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


# pytest_test_report.py -- turn pytest's machine-readable report into the
# trailing-keyword lines parse_log reads.
#
# Why a report file and not the console output: pytest's `-v` line is
# `tests/test_slim_engine.py::TestAPI::test_bad_params PASSED  [ 11%]`, which
# carries a trailing progress percentage that CHANGES as the test count changes
# between acts. This dataset's test patch adds a test, so the percentages shift
# for every test after it -- parsing the console line would fold that varying
# suffix into the name and manufacture a false transition for the whole file
# (FLOW audit 4B). The nodeid from the report carries no percentage and no
# duration, so a name is byte-identical across the three acts.
_PYTEST_TEST_REPORT_PY = '''"""Turn one pytest run into per-test results.

usage: pytest_test_report.py <report-lines-file>

Reads the newline-delimited `<nodeid>\\t<outcome>` file written by the tiny
conftest hook in run_tests.sh, and emits the trailing-keyword form parse_log
reads:

    pytest:tests/test_slim_engine.py::TestAPI::test_bad_params PASSED
    pytest:tests/test_slim_engine.py::TestCLI::test_simulate FAILED

Exit status mirrors a test runner: 0 = everything passed, 1 = at least one test
failed, 2 = the run produced no tests at all (the runner never started -- NOT
"zero tests passed"). 2 is the case FLOW GATE 0 exists to catch.
"""
import sys

STATUS = {"passed": "PASSED", "failed": "FAILED", "error": "FAILED",
          "skipped": "SKIPPED", "xfailed": "SKIPPED", "xpassed": "PASSED"}


def main():
    try:
        fh = open(sys.argv[1], "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.stderr.write("pytest_test_report: cannot read %s: %s\\n"
                         % (sys.argv[1], exc))
        return 2

    seen, failed = {}, False
    with fh:
        for raw in fh:
            raw = raw.rstrip("\\n")
            if not raw or "\\t" not in raw:
                continue
            nodeid, outcome = raw.rsplit("\\t", 1)
            status = STATUS.get(outcome.strip().lower())
            if status is None:
                continue
            # A test reports setup/call/teardown phases; failure is the stronger
            # signal and must win, exactly as parse_log resolves it.
            if seen.get(nodeid) == "FAILED":
                continue
            seen[nodeid] = status

    if not seen:
        sys.stderr.write(
            "pytest_test_report: no test outcomes recorded; the runner never "
            "started\\n")
        return 2

    for nodeid, status in seen.items():
        print("pytest:%s %s" % (nodeid, status))
        if status == "FAILED":
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


# conftest_report.py -- a pytest hook dropped in as a plugin (-p) that records
# one `<nodeid>\ttab<outcome>` line per test. Written as a plugin rather than a
# repo conftest.py so it never lands inside the work tree: a file created under
# /home/stdpopsim would make `git status --porcelain` dirty and abort
# check_git_changes.sh (FLOW Issue 4).
_CONFTEST_REPORT_PY = '''"""pytest plugin: record one `<nodeid>\\t<outcome>` line per test."""
import os

_OUT = os.environ.get("MSB_PYTEST_REPORT", "/tmp/pytest_report.txt")


def pytest_runtest_logreport(report):
    # `call` is the test body; a setup/teardown error still has to be recorded
    # or an erroring test would silently vanish from the results.
    if report.when == "call" or (report.when in ("setup", "teardown")
                                 and report.outcome == "failed"):
        with open(_OUT, "a", encoding="utf-8") as fh:
            fh.write("%s\\t%s\\n" % (report.nodeid, report.outcome))
'''


# run_tests.sh -- scoped to tests/test_slim_engine.py, which is the ONLY file
# this PR's test patch touches.
#
# The repo's own CI (.circleci/config.yml) runs the whole `tests/` tree, and that
# was measured first: it exceeded 15 minutes without finishing a single pass on
# this machine, because stdpopsim's other suites run real coalescent simulations
# and download genetic maps. Three acts of that is not viable, and the extra
# suites contribute no evidence for this PR -- every changed test lives in
# test_slim_engine.py. The scoped command is identical across all three acts, so
# the f2p/n2p comparison stays sound (audit 3B/P7); it runs in ~65s.
#
# No `-p no:cacheprovider` needed beyond MSB_PYTEST_REPORT living in /tmp: the
# reporting plugin is loaded from /home, never written into the work tree, so the
# tree stays clean for check_git_changes.sh.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/stdpopsim
export MSB_PYTEST_REPORT=/tmp/pytest_report.txt
export PYTHONPATH=/home:${PYTHONPATH:-}
rm -f "$MSB_PYTEST_REPORT"
OUT=/tmp/pytest.out
python -m pytest tests/test_slim_engine.py -v --no-header \\
    -p conftest_report -p no:cacheprovider > "$OUT" 2>&1
pytest_rc=$?
cat "$OUT"
echo "pytest exit=${pytest_rc}"
echo "----- per-test results -----"
python3 /home/pytest_test_report.py "$MSB_PYTEST_REPORT"
"""


class StdpopsimImageBase(Image):
    """Shared base image, one per PR (`base-pr-<N>`).

    dockerfile() is deliberately NOT overridden: the harness's own
    Image.dockerfile() already emits the mandated order (FROM -> apt -> clone ->
    WORKDIR -> reset -> checkout ${BASE_COMMIT} -> extra_setup -> hardening
    scrub + its four assertions -> CMD), and DockerfileEnhancer then prepends
    the BuildKit directive, the build ARGs (BASE_COMMIT left EMPTY), the env
    block, the labels and the cert links. Nothing commit-specific is written
    here: build_dataset passes BASE_COMMIT and REPO_URL as docker build args,
    read straight from the dataset.

    The tag carries the PR number because the base's CONTENT is per-PR (it is
    checked out at that PR's base commit). A shared `:base` tag would be one
    name for two different images -- FLOW Issue 25.
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

    # base_commit is dated 2020-03-30 and the code targets the msprime 0.7 API.
    # CI of that era used `circleci/python:3.6-stretch`; 3.6 is long past pip's
    # support horizon, and 3.8 is the newest interpreter for which the pinned
    # 2020 stack still resolves and builds -- measured: msprime 0.7.4 /
    # tskit 0.2.3 / pyslim 0.403 install clean and 7 of 9 tests pass at
    # base_commit. Publishes both linux/amd64 and linux/arm64.
    def dependency(self) -> Union[str, "Image"]:
        return "python:3.8-bullseye"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # libgsl-dev  : msprime 0.7.x links GSL and has no wheel for this era,
        #               so it is compiled from source (the repo's CI installs it
        #               for exactly this reason).
        # libhdf5-dev : pyslim 0.403 pulls in h5py, whose build needs the HDF5
        #               headers. Without it the install dies with
        #               "ERROR: Failed building wheel for h5py" -- measured.
        # pkg-config  : how h5py locates those headers.
        # All three are arch-neutral package names, so audit 2D stays clean.
        return ["libgsl-dev", "libhdf5-dev", "pkg-config"]

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}`, so setup.py is this PR's own.
        #
        # The versions are PINNED rather than resolved from requirements, and
        # that is the single most important decision in this recipe. The repo
        # asks only for `msprime>=0.7.1` / `pyslim>=0.401`, so an unpinned
        # install today resolves to msprime 1.3.1 / pyslim 1.1.0 -- a major API
        # break against 2020 code. Measured: unpinned, the suite cannot even be
        # collected; pinned, 7 of 9 tests pass at base_commit.
        #
        # `--no-deps -e .` installs the package itself without letting setup.py's
        # loose bounds pull the pinned versions forward again.
        #
        # setuptools is pinned too, and that pin is not cosmetic. setup.py
        # declares `setup_requires=['setuptools_scm']`, so setuptools resolves it
        # at build time and fetches the LATEST into .eggs/ -- setuptools_scm
        # 10.2.1, which calls into distutils internals Python 3.8 does not have:
        #
        #     .eggs/setuptools_scm-10.2.1-py3.8.egg/.../egg_info.py
        #     AttributeError: ignore_egg_info_in_manifest
        #     error: metadata-generation-failed
        #
        # That killed the first image build here. `pip install --upgrade pip` is
        # deliberately NOT run for the same reason: the newer pip changes build
        # isolation and is what pulled the incompatible setuptools_scm in.
        #
        # NOT `|| true`: this is the toolchain, not a nice-to-have cache. A
        # warm-up that fails quietly ships an image whose acts cannot import the
        # package under test (FLOW Issue 14, GATE 1). The import probe is what
        # proves the stack actually resolved.
        return (
            "RUN pip install --no-cache-dir \\\n"
            '        "setuptools<60" "setuptools_scm<6" && \\\n'
            "    pip install --no-cache-dir \\\n"
            '        "msprime==0.7.4" "tskit==0.2.3" "pyslim==0.403" \\\n'
            '        "attrs" "appdirs" "humanize" "pytest" && \\\n'
            "    pip install --no-cache-dir --no-deps -e . && \\\n"
            '    python -c "import msprime, tskit, pyslim, stdpopsim; '
            "print(msprime.__version__, tskit.__version__, pyslim.__version__)\" && \\\n"
            "    python -m pytest --version"
        )


class StdpopsimImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and the act scripts, run
    prepare.sh -- and nothing else, which is the shape of
    Main_Tasks/pr_specific dockerfile.dockerfile. The clone, the checkout and
    the history scrub already happened in the base."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return StdpopsimImageBase(self.pr, self._config)

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
pip install --no-cache-dir --no-deps -e . || true
python -c "import msprime, tskit, pyslim, stdpopsim"
python -m pytest --version
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

cd /home/stdpopsim
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/stdpopsim
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

cd /home/stdpopsim
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


# The plain `<org>/<repo>` key. Seven era-ranged keys already exist for this repo
# (stdpopsim_40_to_40 ... stdpopsim_1664_to_1416) and PR 450 falls inside
# stdpopsim_547_to_412 -- but those are reached only when the dataset carries a
# `number_interval`, and prep_dataset.py strips it precisely so every entry
# resolves to this plain, self-contained recipe (FLOW STEP 1). The keys are
# distinct strings, so registering this one shadows none of them: verified,
# Instance._registry still lists all seven alongside it.
@Instance.register("popsim-consortium", "stdpopsim")
class Stdpopsim(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StdpopsimImageDefault(self.pr, self._config)

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
        # The raw pytest output shares the log and its own `-v` lines end in the
        # same status words, so a whole-log scan would count each test twice --
        # once with a trailing progress percentage baked into the name. The
        # fallback to the whole text keeps a bare sequence of result lines
        # parseable (the config audit's 4C probe has no marker).
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what pytest_test_report.py prints. The
        # name is captured non-greedily BEFORE the keyword, so no duration or
        # progress percentage can leak in and manufacture a false transition.
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
