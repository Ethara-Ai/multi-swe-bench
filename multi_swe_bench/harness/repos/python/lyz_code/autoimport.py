"""Repo config for lyz-code/autoimport.

Written against handoff/DOCKERFILE_FORMAT.md (the approved shape):

  * The base image's dependency() returns a *string*, so DockerfileEnhancer
    rewrites it -- that is the only image that receives the syntax directive,
    the proxy ARGs, the CA-certificate symlinks, the OCI labels and the
    REPO_URL / BASE_COMMIT build args. The repo fetch therefore lives here.
  * Every toolchain RUN sits ABOVE the clone line, because
    _standardize_repo_fetch replaces that line with a block ending in
    CMD ["/bin/bash"]; anything below would land past the CMD and never run.
  * The PR image is minimal: COPY the patches and scripts, RUN prepare.sh.
    No clone, no ARG BASE_COMMIT, no hardening block, no CMD.

Repository notes (base commit d53bcfbf9b76bbd49f942c60cb2de54e3fb46503):

  * setup.py declares python_requires=">=3.7" and CI ran the suite on 3.7/3.8.
    BASE_IMAGE is python:3.9-slim -- close enough to that era for the pinned
    dependencies, new enough for prebuilt wheels, and verified to ship
    /etc/ssl/certs/ca-certificates.crt (QC item D8).
  * requirements.txt pins the runtime deps but omits `maison`, which setup.py
    does declare, so the version is entirely up to this config. It is pinned to
    exactly 1.1.0, and the pin is load-bearing in both directions:
      - maison 2.0 renamed ProjectConfig to UserConfig, and
        src/autoimport/entrypoints/cli.py imports `from maison.config import
        ProjectConfig`, so >=2 breaks every e2e test on import.
      - maison <=1.0.0 and >=1.3.0 change how a config file passed via
        --config-file is discovered, which makes
        tests/e2e/test_cli.py::test_config_path_argument fail at the base
        commit. A test failing before the fix patch for reasons unrelated to
        the PR corrupts grading, so the baseline must be green.
    Measured at this base commit: 0.1.0 and 1.0.0 fail, 1.1.0 and 1.2.3 give
    75 passed / 0 failed, 1.3.0 and 1.4.3 fail.
  * tests/conftest.py (as amended by the test patch) does
    `from py._path.local import LocalPath`, so the standalone `py` package is
    required; recent pytest no longer guarantees it.
  * pyproject.toml sets addopts = "-vv --tb=short -n auto". The `-n auto` runs
    the suite under pytest-xdist, which interleaves worker output and makes
    per-test parsing unreliable. TEST_CMD clears addopts via --override-ini so
    the suite runs serially with one line per test, as the format doc requires.
"""

from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# THE TEST COMMAND
#
# --override-ini="addopts=" drops the repo's own "-vv --tb=short -n auto", so
# the suite runs serially and prints exactly one line per test.
# -p no:cacheprovider keeps pytest from writing .pytest_cache into the tree,
# which would otherwise make check_git_changes.sh see a dirty worktree.
# --timeout=1800 makes a hung suite fail instead of blocking the whole run.
#
# --continue-on-collection-errors is essential for THIS PR. The test patch adds
# tests/unit/test_entrypoints.py, which imports FileOrDir, flatten and get_files
# -- names the fix patch introduces. Under the test patch alone that import
# fails, and pytest's default response to a collection error is to abort the
# entire session ("Interrupted: 1 error during collection"), so all 76 other
# tests never run and vanish from the counts. The two new tests in
# tests/e2e/test_cli.py would then never be recorded as failing before the fix
# and passing after, which is exactly the signal grading depends on.
# ---------------------------------------------------------------------------
TEST_CMD = (
    'python -m pytest tests/ -v --tb=short --override-ini="addopts=" '
    "-p no:cacheprovider --continue-on-collection-errors --timeout=1800"
)

BASE_IMAGE = "python:3.9-slim"

# Only system-level setup belongs here: it runs before the repo exists.
# Python dependencies are installed in prepare.sh, after the checkout.
TOOLCHAIN_SETUP = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel
"""


class LyzCodeAutoimportImageBase(Image):
    """Level 1: per-PR base image -- toolchain plus the repository checkout.

    Tagged `base-pr-<number>` rather than a shared `base`: a shared tag would
    bake in one BASE_COMMIT and stay pinned to whichever PR built it first, so
    any later PR whose base commit is unreachable from that sha would die in
    prepare.sh with `fatal: unable to read tree`.
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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # TOOLCHAIN_SETUP must stay above `code`: the enhancer replaces that
        # single line with clone + checkout + hardening + CMD.
        return (
            f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

"""
            + TOOLCHAIN_SETUP
            + f"""
{code}

{self.clear_env}

"""
        )


class LyzCodeAutoimportImageDefault(Image):
    """Level 2: per-PR image -- patches, run scripts, and the warm-up build."""

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
        return LyzCodeAutoimportImageBase(self.pr, self._config)

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
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Runtime deps, pinned by the repo's own lock file.
pip install --no-cache-dir -r requirements.txt

# requirements.txt omits maison even though setup.py requires it, so the
# version is chosen here. Pinned exactly: >=2 renames ProjectConfig (which the
# CLI imports by the old name) and breaks every e2e test on import, while
# <=1.0.0 and >=1.3.0 change --config-file discovery and make
# test_config_path_argument fail at the base commit. Only 1.1.0 and 1.2.3 give
# a green baseline; a red baseline would corrupt grading.
pip install --no-cache-dir "maison==1.1.0"

# --no-deps: everything setup.py declares is already pinned above, and a bare
# editable install would happily pull an unpinned maison back in.
pip install --no-cache-dir --no-deps -e .

# Test-only deps. `py` is needed because tests/conftest.py imports
# py._path.local.LocalPath directly.
pip install --no-cache-dir pytest==6.2.5 pytest-timeout==2.1.0 py==1.11.0

# Warm the caches so the three grading stages are fast. Skipped on a foreign
# architecture, where this runs under QEMU at roughly 10x slower and buys
# nothing: grading always happens on the native arch.
if [ "$(uname -m)" = "x86_64" ]; then
  {test_cmd} || true
else
  echo "prepare.sh: $(uname -m) is not the grading architecture -- skipping the"
  echo "prepare.sh: test warm-up."
fi

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD),
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


# ---------------------------------------------------------------------------
# LOG PARSING
#
# pytest -v emits one line per test:
#     tests/unit/test_services.py::test_foo PASSED   [ 12%]
# and repeats failures in the summary:
#     FAILED tests/unit/test_services.py::test_foo - AssertionError: ...
#     ERROR tests/e2e/test_cli.py::test_bar
#
# A module that fails to import produces a bare collection error with no test
# id. That runs no tests at all, so it would silently vanish from the counts;
# it is recorded as a failure against the file instead.
# ---------------------------------------------------------------------------
_RE_INLINE = re.compile(
    r"^(tests/\S+?::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_RE_SUMMARY = re.compile(r"^(?:FAILED|ERROR)\s+(tests/\S+?::\S+?)(?:\s|$)")
_RE_COLLECT_ERROR = re.compile(r"^ERROR\s+(tests/\S+\.py)\s*$")

# Tests that flip between pass and fail on identical code. Empty for this repo:
# the suite is deterministic once addopts drops the xdist parallelism.
KNOWN_FLAKY_TESTS: frozenset[str] = frozenset()


def parse_pytest_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(status: str, test_id: str) -> None:
        if test_id in KNOWN_FLAKY_TESTS:
            return
        if status in ("PASSED", "XPASS"):
            # A test that failed earlier in the same log stays failed.
            if test_id in failed_tests:
                return
            skipped_tests.discard(test_id)
            passed_tests.add(test_id)
        elif status in ("FAILED", "ERROR"):
            passed_tests.discard(test_id)
            skipped_tests.discard(test_id)
            failed_tests.add(test_id)
        elif status in ("SKIPPED", "XFAIL"):
            if test_id not in passed_tests and test_id not in failed_tests:
                skipped_tests.add(test_id)

    for line in log.splitlines():
        line = line.rstrip()

        match = _RE_INLINE.match(line)
        if match:
            record(match.group(2), match.group(1))
            continue

        match = _RE_SUMMARY.match(line)
        if match:
            record("FAILED", match.group(1))
            continue

        match = _RE_COLLECT_ERROR.match(line)
        if match:
            record("FAILED", match.group(1))
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("lyz-code", "autoimport")
class LyzCodeAutoimport(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LyzCodeAutoimportImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_pytest_log(test_log)