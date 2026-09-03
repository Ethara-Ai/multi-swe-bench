"""pypa/setuptools - pytest suite, ONE shared base image for the whole dataset.

WHY THIS FILE EXISTS ALONGSIDE THE SIX RANGE CONFIGS
-----------------------------------------------------
`python/pypa/` already holds setuptools_809_to_716, _1103_to_809, _1365_to_1364,
_1520_to_1365, _3212_to_2878 and _4985_to_4565. None of them can serve this dataset:
its five PRs are 3230, 3779, 3904, 4000 and 4021, which fall in the GAP between
3212 and 4565.

Instance.create (harness/instance.py:41) routes on `pr.number_interval` and only falls
back to `<org>/<repo>` when it is empty. Every record here has number_interval: None,
so the lookup is `pypa/setuptools` - which nothing registered. This file registers it.
The raw dataset is read-only input and was NOT edited to add an interval.

BASE IMAGE: ONE PER DATASET
---------------------------
Tagged by FLAVOUR (`base-pytest`), not per base commit. Multiple base images is the
problem the team is removing - each one costs a full clone and several GB - so the base
is shared and `prepare.sh` fetches whichever commit its PR needs. See the comment on
`prepare.sh` below for the mechanism that makes that work.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# setuptools declares its test extra in setup.cfg/pyproject as `testing`. `-e` because
# several tests introspect the working tree rather than an installed copy.
#
# The install runs in EVERY graded script, not once in the base: three of the five PRs
# touch files that change what gets installed or how (build_ext.py, build_py.py,
# egg_info.py), and PR 3904 rewrites _core_metadata.py. Installing only in the base
# would leave each stage exercising the base commit's build backend.
_INSTALL = r"""
# ---------- install ----------
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_ROOT_USER_ACTION=ignore

pip install -e '.[testing]' 2>&1 \
    || pip install -e '.[tests]' 2>&1 \
    || pip install -e . 2>&1 \
    || true

# setuptools' own suite imports these directly; on older tags they are not always
# pulled in by the extra.
python -c "import pytest" 2>/dev/null || pip install pytest 2>&1 || true
python -c "import jaraco.envs" 2>/dev/null || pip install jaraco.envs jaraco.path 2>&1 || true

# Remove the plugins that setuptools' `testing` extra pulls in and that pytest-enabler
# then auto-activates. This is why "reverting parallelism" on the command line did not
# work: pytest-enabler injects `-n auto` for xdist at RUNTIME, so run.sh never had to
# mention -n to get workers - and it got them. Measured on pr-3230 (2026-09-02):
#     [gw6] PASSED setuptools/tests/test_integration.py::test_pyuri   (run)
#     [gw2] PASSED setuptools/tests/test_integration.py::test_pyuri   (test)
#     [gw3] FAILED setuptools/tests/test_integration.py::test_pyuri   (fix)
# a different worker each stage, which is Rule 2 and voided the instance.
#
# The same mechanism grades pytest-black and pytest-mypy AS TESTS - 171 ::black and 171
# ::mypy pseudo-tests per stage - whose outcomes track whichever black/mypy version pip
# resolved that day rather than the code under test.
#
# Uninstalling rather than `-p no:...` because it is verifiable: a plugin that is not
# installed cannot inject anything, whereas `-p no:<name>` silently does nothing when
# the entry-point name is wrong. Must run AFTER the install above, which is what pulls
# them in. Environment-only - it changes no file in the work tree.
pip uninstall -y pytest-enabler pytest-xdist pytest-black pytest-mypy pytest-cov \
    pytest-perf pytest-checkdocs pytest-flake8 2>&1 || true

# setuptools' `testing` extra lists `wheel` UNPINNED, so pip takes today's newest.
# Measured on pr-3230 (2026-09-02) it resolved wheel 0.45.1 against a setuptools 61.2.0
# base commit from April 2022, and all 32 parametrisations of
# TestWheelCompatibility::test_dist_info_is_the_same_as_in_wheel failed with
#     subprocess.CalledProcessError: Command '[... 'bdist_wheel']' returned non-zero exit status 1
# Those 32 are NEW tests added by the test patch, so failing at the fix stage they
# scored nothing: the instance came back f2p=1, n2p=0 instead of n2p~32.
#
# `wheel` provided the bdist_wheel command until setuptools started shipping its own at
# ~70.1; recent wheel releases dropped it, which leaves a 2022-era setuptools with no
# bdist_wheel at all. <0.44 is the newest range that still provides it, chosen over an
# exactly-contemporaneous pin (0.37.1) because the same _INSTALL serves base commits
# from 2022 through 2023 and an over-old wheel risks breaking the newer ones.
#
# Runs AFTER the extra install, so it downgrades whatever that resolved.
pip install 'wheel<0.44' 2>&1 || true
# ---------- end install ----------
"""

# -v            one line per test, which parse_log reads
# --no-header   drops the platform/rootdir preamble
# -rA           short summary for every outcome, so a test that errors is still named
# --tb=no       tracebacks add tens of thousands of lines and parse_log never reads them
# -p no:cacheprovider   no .pytest_cache written into the work tree
#
# No `|| true`: a non-zero exit is expected at the run and test stages and the harness
# reads the log, not the status.
# -p no:enabler is belt-and-braces alongside the uninstall in _INSTALL: if the extra
# ever reinstalls pytest-enabler, this still stops it injecting -n auto and the
# black/mypy pseudo-tests. Harmless when the plugin is absent.
#
# --continue-on-collection-errors is REQUIRED, not hygiene. pytest treats a collection
# error as fatal and aborts the whole session, so one un-importable file takes every
# other test down with it. Measured on pr-3904 (2026-09-02): its test patch adds
# setuptools/tests/test_core_metadata.py, which does
#     from setuptools._core_metadata import rfc822_escape, rfc822_unescape
# and its fix patch is what CREATES setuptools/_core_metadata.py (new file mode 100644).
# So at the test stage - test patch applied, fix patch not - that import cannot resolve.
# That is correct and expected for the datapoint, but without this flag pytest printed
#     collected 1122 items / 2 errors
#     !!! Interrupted: 2 errors during collection !!!
# and the stage reported (0, 0, 0). An empty test stage then makes every test that
# passes at fix look like a none-to-pass transition, which tripped the bias guard
# ("Fix patch modified file(s) containing credited test(s)") and voided the instance.
# With the flag, the un-importable file is reported as an error, the other ~1120 tests
# still run, and its tests appear only at the fix stage as genuine n2p.
_PYTEST_FLAGS = (
    "-v --no-header -rA --tb=no -p no:cacheprovider -p no:enabler "
    "--continue-on-collection-errors"
)

# Files deselected in ALL THREE stages, so the run/test/fix comparison is untouched.
#
# Each of these drives real subprocesses - it builds a virtualenv, or installs a package
# from PyPI - so its outcome depends on the network and on what runs beside it, not on
# the patch under test. Measured on pr-3230 (2026-09-02), identical in all three stages:
# 24 errors in test_distutils_adoption.py, 16 in integration/test_pip_install_sdist.py,
# 14 in test_virtualenv.py. Those contribute no outcomes at all, so dropping them
# removes noise rather than signal.
#
# test_integration.py is the one that actually cost an instance: test_pyuri downloads
# from PyPI and went PASS -> PASS -> FAIL across the three stages. None of the five fix
# patches in this dataset touch code these files exercise.
#
# This is NOT the path-argument shortcut the docstring on _test_cmd warns about. That
# one replaced the whole collection root with `setuptools/tests`, silently dropping
# pkg_resources/tests and the doctest targets - 1252 collected down to 941. These four
# --ignore entries remove only named, measured-unstable files.
_IGNORES = (
    "setuptools/tests/test_integration.py",
    "setuptools/tests/integration",
    "setuptools/tests/test_distutils_adoption.py",
    "setuptools/tests/test_virtualenv.py",
)


def _fix_patch_sources(pr) -> list[str]:
    """Source files the fix patch edits - their doctests are authored BY the fix.

    setuptools' own pytest.ini carries `--doctest-modules`, so pytest collects doctests
    out of ordinary source modules and grades them as tests. When the fix patch edits a
    source file it can therefore ADD a graded test, and crediting the fix for a test the
    fix wrote is exactly what the harness bias guard refuses:

        Fix patch modified file(s) containing credited test(s)
        Touched credited tests: setuptools/_normalization.py::setuptools._normalization.safe_extra

    Measured on pr-3904 (2026-09-02): _normalization.py yielded 10 doctest outcome lines
    at run and test and 12 at fix - the fix patch adds `safe_extra` - and that single
    doctest voided an instance carrying ~30 genuine n2p from
    setuptools/tests/test_core_metadata.py.

    Excluding these paths removes ONLY their doctests: the real tests live under
    setuptools/tests/, which this never touches (paths under a tests/ directory are
    filtered out). It is the same rule the bias guard enforces, applied at collection
    time so a fix-authored doctest cannot enter the comparison in the first place,
    instead of voiding the whole datapoint after the fact.

    Derived per PR from fix_patch, and used by all three graded scripts identically, so
    the run/test/fix comparison stays sound.
    """
    out = set()
    for p in re.findall(r"^diff --git a/(\S+)", pr.fix_patch or "", re.M):
        if p.endswith(".py") and "/tests/" not in p and not p.startswith("tests/"):
            out.add(p)
    return sorted(out)


def _test_cmd(pr) -> str:
    """The whole suite, serially. Both shortcuts were tried and both were reverted.

    SCOPING to the files the test_patch touches: REVERTED. The full-suite report for
    pr-3230 put all 12 of its f2p tests in setuptools/tests/test_build_meta.py, while
    its test_patch touches only setuptools/tests/test_dist_info.py - the fix edits
    setuptools/command/dist_info.py and egg_info.py, and the tests that flip are
    pre-existing ones in another file that call into that code. Scoping would have
    scored that instance 0 f2p / 0 n2p and lost it. Transitions are NOT confined to the
    files the test patch touches, so the suite has to run whole.

    PARALLELISM via pytest-xdist: REVERTED. Measured on pr-3230 (2026-09-02), serial
    gave valid=True with f2p=12 and p2p=1026; the SAME suite under `-n auto
    --dist loadfile` gave valid=False with f2p=0, rejected on Rule 2, "Before applying
    the fix patch, the test passed; however, after applying the fix patch, the test
    failed". Stage counts drifted a few tests in BOTH directions across the three stages
    (782P/119F -> 780P/130F -> 781P/129F) because setuptools tests share cwd, tmpdirs
    and the filesystem, so an outcome depends on which tests run beside it. f2p is a set
    difference over three runs, so one spurious PASS->FAIL voids the instance;
    --dist loadfile was not enough to contain it. The win was only 13% anyway (670s vs
    775s) - this suite is subprocess-bound (venv creation, wheel builds, pip), so idle
    cores rather than serialisation are the bottleneck.

    NO PATH ARGUMENT. `pytest` bare collects 1252 tests; `pytest setuptools/tests`
    collects 941, dropping pkg_resources/tests and the doctest targets. That is not a
    harmless reduction in p2p: the validated run's outcomes totalled 1253 and both runs
    made with the scoped path totalled 942 and came back valid=False on Rule 2. This
    suite is order-dependent - tests share cwd, tmpdirs and installed state - so which
    other tests ran first changes the result of the ones that remain. Adding a path here
    looks like a safe narrowing and is not.

    ~13 min/stage is the real cost of this repo. Do not re-try either shortcut.
    """
    paths = list(_IGNORES) + _fix_patch_sources(pr)
    ignores = " ".join(f"--ignore={p}" for p in paths)
    return f"pytest {_PYTEST_FLAGS} {ignores}"


class SetuptoolsImageBase(Image):
    """The ONE base image for this dataset.

    Tagged `base-pytest` by flavour, not by base commit, so all five PRs share it.
    `dockerfile()` is deliberately NOT overridden: Image.dockerfile() plus
    DockerfileEnhancer already emit the syntax pin, ARGs, proxy ENV, CA symlinks, OCI
    labels, apt block, clone, BASE_COMMIT checkout and the history-scrub assert. Only
    the hooks below are ours.
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

    def dependency(self) -> Union[str, "Image"]:
        # A str dependency is what makes DockerfileEnhancer apply to this layer.
        return "python:3.9-slim"

    def image_tag(self) -> str:
        return "base-pytest"

    def workdir(self) -> str:
        return "base-pytest"

    def extra_packages(self) -> list[str]:
        # -slim ships no compiler; several test deps build C extensions from sdist.
        return ["gcc", "libc6-dev"]

    def extra_setup(self) -> str:
        # Nothing here on purpose. The editable install has to run per PR, in
        # prepare.sh, because the PRs change what gets installed - see _INSTALL.
        return ""

    def files(self) -> list[File]:
        return []


class SetuptoolsImageDefault(Image):
    """The thin per-PR layer: patches, scripts, and the prepare.sh install."""

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
        return SetuptoolsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

# The shared base image is pruned by the enhancer's hardening block to ONE
# BASE_COMMIT's ancestry, so this PR's commit is very likely absent. Re-attach the
# remote and fetch just that sha before checking out - this is what lets a single base
# image serve every PR in the dataset instead of building one base per commit.
git remote add origin https://github.com/{org}/{repo}.git 2>/dev/null || true
git fetch --depth=1 origin {sha} 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout {sha}
bash /home/check_git_changes.sh

# An editable install writes *.egg-info/ and .eggs/ into the work tree. Those are
# UNTRACKED, so `git checkout -- .` below cannot clear them and the clean-tree assert
# would fail. .git/info/exclude is git's own per-clone ignore list and is NOT a tracked
# file, so this changes nothing about the code under test.
printf '*.egg-info/\\n.eggs/\\n.pytest_cache/\\n__pycache__/\\n' >> .git/info/exclude
{install}
# The install rewrites TRACKED files (setuptools regenerates its own metadata), leaving
# the tree dirty AFTER the asserts above have already passed. A later `git apply` then
# fails with "patch does not apply" and the whole stage is lost. Restoring tracked files
# keeps the installed package - the egg-link and site-packages entry live outside the
# work tree - while returning the tree to exactly BASE_COMMIT, which is the state every
# `git apply` in the three run scripts expects. The assert turns a dirty tree into a
# loud build failure instead of a silent one.
git checkout -- .
bash /home/check_git_changes.sh
""".format(
                    repo=self.pr.repo,
                    org=self.pr.org,
                    sha=self.pr.base.sha,
                    install=_INSTALL,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{install}
{test}
""".format(repo=self.pr.repo, install=_INSTALL, test=_test_cmd(self.pr)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{install}
{test}
""".format(repo=self.pr.repo, install=_INSTALL, test=_test_cmd(self.pr)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{install}
{test}
""".format(repo=self.pr.repo, install=_INSTALL, test=_test_cmd(self.pr)),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
{prepare_commands}

{self.clear_env}

"""


@Instance.register("pypa", "setuptools")
class Setuptools(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SetuptoolsImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest colours its output when it believes it has a tty.
        log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", log)

        # `-v` line:      setuptools/tests/test_sdist.py::TestSdistTest::test_x PASSED [ 12%]
        # `-rA` summary:  PASSED setuptools/tests/test_sdist.py::TestSdistTest::test_x
        #
        # The id is matched up to the LAST whitespace before the status rather than with
        # \S+, because a parametrised id can contain spaces inside its brackets.
        verbose_re = re.compile(
            r"^(?P<name>\S+::.+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        summary_re = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<name>\S+::\S+)"
        )

        def record(name: str, status: str) -> None:
            name = name.strip()
            if not name:
                return
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:  # SKIPPED, XFAIL
                skipped_tests.add(name)

        for raw in log.splitlines():
            line = raw.rstrip()
            m = verbose_re.match(line) or summary_re.match(line)
            if m:
                # A slow test gets a "(0.12s)" suffix that varies run to run; stripping
                # it keeps the id stable across stages.
                name = re.sub(r"\s*\(\d+\.\d+s\)$", "", m.group("name"))
                record(name, m.group("status"))

        # `-rA` echoes every test that the verbose run already reported, and a rerun can
        # emit one twice. Enforce one bucket each, worst status winning, or the stage
        # comparison double-counts and invents transitions.
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
