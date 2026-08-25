"""Repo config for panoptes/POCS (Python 3.12 / pytest via uv).

Runner
------
Upstream CI (`.github/workflows/pythontest.yaml`) is::

    uv sync --all-extras --group testing
    uv run pytest

on Python 3.12, and that is what is reproduced. `uv.lock` is committed, so
`--frozen` reproduces the exact resolved tree rather than re-resolving against
whatever PyPI serves today; `--all-extras` is kept because `--doctest-modules`
collects every module under `src`, including the ones that import the optional
`google` and `weather` dependencies -- dropping those extras turns clean
doctests into collection errors.

Tests run through `.venv/bin/python -m pytest` rather than `uv run pytest`:
`uv run` re-checks the environment and can reach for the network, which the
evaluation container does not have. Invoking the interpreter uv already built
skips that entirely.

Overriding `-x`
---------------
`[tool.pytest.ini_options].addopts` carries `-x`. Left in place it is fatal to
this instance, not merely inconvenient: the test stage's whole job is to run the
gold tests against unfixed code, and `-x` stops the session at the *first*
failure, so every test after it -- including the ~200 that should be reported as
passing -- goes uncollected. `Report` would then see a p2p set that shrinks
between stages and reject the run. Command-line arguments are applied after
`addopts`, so the `--maxfail=0` below wins and restores "run everything".

`--no-cov` is passed for the same reason `-p no:cacheprovider` is: the coverage
addopts (`--cov`, `--cov-report=xml:build/coverage.xml`) cost time and write
files, and coverage has no bearing on whether a test passed.

Hardware and network
--------------------
The suite looks hardware-bound and is not. The root `conftest.py` starts the
PANOPTES config server *in process* on `localhost:8765`, so there is no external
service to provision, and `pytest_collection_modifyitems` skips every
`with_<hardware>` test unless `--with-hardware` names it -- the default is no
hardware, which is exactly the evaluation container.

`conftest.pytest_configure` also calls `download_iers_a_file()`, which reads as a
network dependency and is not one: the function only points astropy's
`iers_auto_url` at a mirror and sets `iers_conf.auto_download` from
`scheduler.iers_auto`, which defaults to False. No fetch is issued; astropy
falls back to its bundled table.

`logs/` is created in prepare.sh because `conftest` opens
`logs/panoptes-testing.log` and then `os.chmod`s it. loguru would create the
directory itself, but the chmod runs against a path that must already exist by
then, and a build-time mkdir is cheaper than discovering that at run time.

Test identity
-------------
`<file>::<test>` as pytest reports it, e.g.::

    tests/test_observation.py::test_observation_tags

`--tb=no -rA` gives one status line per test in the short summary and no
traceback noise between them, which is what `parse_log` matches. `-v` is
redundant with the `-vv` already in `addopts` but is passed explicitly so the
output shape does not depend on a pyproject edit.

Toolchain pin
-------------
`python:3.12-bookworm`: `requires-python = ">=3.12"` and CI's matrix pins
`"3.12"`. The scientific stack this repo depends on (numpy>=2, scipy,
scikit-image, astropy>=7) publishes cp312 manylinux and aarch64 wheels, so no
compiler work happens at install time on either architecture.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Mirrors the package list baked into Image.dockerfile() (image.py), so the
# hand-written Dockerfile below installs exactly the canonical toolchain.
_DEFAULT_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
]

# `--maxfail=0` cancels the `-x` in pyproject's addopts (see module docstring).
# `--continue-on-collection-errors` keeps one bad module from taking the whole
# session with it, so a stage still reports the tests that did collect.
PYTEST_CMD = (
    ".venv/bin/python -m pytest -v --no-header -rA --tb=no "
    "-p no:cacheprovider --continue-on-collection-errors --maxfail=0 --no-cov"
)


class PocsImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- Python 3.12 plus the repo at BASE_COMMIT.

    Tagged per PR because the hardening block prunes the repo to a single
    commit, which pins the image to one BASE_COMMIT. Every layer above the
    clone is byte-identical across PRs of this repo, so Docker's layer cache
    reuses them and only the clone is rebuilt.
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

    def dependency(self) -> str:
        return "python:3.12-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_DEFAULT_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        return f"""\
FROM {base_img}

{self.global_env}

WORKDIR /home/

{apt_command}

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{self.clear_env}

CMD ["/bin/bash"]
"""


class PocsImageDefault(Image):
    """Per-PR image -- resolves the locked dependency tree into `.venv`."""

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
        return PocsImageBase(self.pr, self._config)

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# `-e` is load-bearing: without it the check_git_changes.sh calls below are
# advisory, and an assertion that cannot fail the build is not an assertion.
set -euxo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# uv resolves `uv.lock` into .venv. `--frozen` forbids re-resolving, so the
# tree that gets installed is the one the lockfile pins rather than whatever
# PyPI serves today -- and it also keeps uv from rewriting uv.lock, which is
# tracked and would dirty the check below.
#
# `|| true` on both installs, then an explicit assertion. The tolerance is
# there because a partial-but-usable install is a real outcome (a wheel that
# fails to build for one optional extra should not kill the image), but a
# bare `|| true` alone would be worse than useless here: without .venv the
# interpreter the run scripts invoke does not exist, so all three stages
# report zero tests and the instance is rejected only after a full build and
# three container runs. The check below turns that into a build-time failure.
pip install --no-cache-dir uv || true
uv sync --all-extras --group testing --frozen || true

if [ ! -x .venv/bin/python ]; then
    echo "prepare.sh: uv sync produced no .venv/bin/python" >&2
    exit 1
fi
.venv/bin/python -c "import pytest, panoptes.pocs"

# conftest.py opens logs/panoptes-testing.log and then os.chmod()s it; the
# chmod needs the path to exist by that point.
mkdir -p logs

# pyproject sets `version_file = "src/panoptes/pocs/_version.py"`, so the local
# install above makes setuptools_scm write that file. `.gitignore` only grew a
# matching entry *after* BASE_COMMIT, so at this commit the generated file
# lands untracked -- and `git status --porcelain` reports `??` lines too, not
# just edits to tracked paths, so the check below would fail on it. Recording
# the exclusion in `.git/info/exclude` (itself untracked, and outside the
# worktree the patches apply to) drops that one false positive without
# loosening the assertion for anything else.
echo 'src/panoptes/pocs/_version.py' >> .git/info/exclude

# .venv/, logs/ and build/ are all gitignored, so a dirty tree here means an
# install wrote somewhere the repo does not expect -- a TRACKED path, or a new
# untracked one -- and every later `git apply` would then be laid on top of
# unexplained edits.
#
# Deliberately the last command: no `exit 0` follows, so the script's exit
# status is this check's status.
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{pytest_cmd}
""".format(pr=self.pr, pytest_cmd=PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{pytest_cmd}
""".format(pr=self.pr, pytest_cmd=PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{pytest_cmd}
""".format(pr=self.pr, pytest_cmd=PYTEST_CMD),
            ),
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

WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("panoptes", "POCS")
class Pocs(Instance):
    """Instance handler for panoptes/POCS.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create``
    resolves on.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PocsImageDefault(self.pr, self._config)

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

        # Strip ANSI colour codes FIRST. pytest emits them whenever stdout is a
        # TTY; without this they are captured *into* the test name, so the same
        # test can carry a different name in different stages and Report's
        # cross-stage union splits it into two entries.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Both patterns are anchored per line and matched one line at a time.
        # An unanchored whole-log scan is the standard failure mode here: in the
        # `-rA` summary block the test name ending a PASSED line binds to the
        # status word starting the next line, putting the same test in both
        # passed and failed -- which TestResult.__post_init__ rejects outright.
        #
        # Progress form: tests/test_x.py::test_y PASSED   [ 50%]
        re_progress = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        # Summary form: FAILED tests/test_x.py::test_y - AssertionError: ...
        re_summary = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)"
        )

        def record(status: str, name: str) -> None:
            # Requiring "::" keeps free-form log lines that happen to start with
            # a status word out of the result sets.
            if "::" not in name:
                return
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        for line in test_log.splitlines():
            line = line.strip()
            m = re_progress.match(line)
            if m:
                record(m.group(2), m.group(1))
                continue
            m = re_summary.match(line)
            if m:
                record(m.group(1), m.group(2))

        # A test that failed anywhere is failed, whatever another line claimed.
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
