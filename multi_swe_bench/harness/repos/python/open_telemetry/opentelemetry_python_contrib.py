"""Repo config for open-telemetry/opentelemetry-python-contrib.

Written against handoff/DOCKERFILE_FORMAT.md:

  * The base image's dependency() returns a *string*, so DockerfileEnhancer
    rewrites it and it receives the REPO_URL / BASE_COMMIT build args. The repo
    fetch therefore lives there.
  * Every toolchain RUN sits ABOVE the clone line, because
    _standardize_repo_fetch replaces that line with a block ending in
    CMD ["/bin/bash"].
  * The PR image is minimal: COPY patches and scripts, RUN prepare.sh.

THE THING THAT MAKES THIS REPO UNUSUAL: it cannot install from PyPI alone
---------------------------------------------------------------------------
`opentelemetry-instrumentation-botocore/setup.cfg` at this commit declares:

    install_requires =
        opentelemetry-api == 0.16.dev0
        opentelemetry-instrumentation == 0.16.dev0

Those are *unreleased* dev versions. They do not exist on PyPI and never will,
so a plain `pip install .[test]` fails to resolve. This is the standard shape
for a contrib repo: it is developed against the tip of its core repo, not
against a published release.

Upstream solves it by checking out a SECOND repository beside this one. From
`.github/workflows/test.yml` at this commit:

    env:
      CORE_REPO_SHA: 47483865854c7adae7455f8441dab7f814f4ce2a
    ...
    - uses: actions/checkout@v2
      with:
        repository: open-telemetry/opentelemetry-python
        ref: ${{ env.CORE_REPO_SHA }}
        path: opentelemetry-python-core

and `tox.ini` then installs the core packages from that directory:

    pip install {toxinidir}/opentelemetry-python-core/opentelemetry-api \
                {toxinidir}/opentelemetry-python-core/opentelemetry-sdk \
                {toxinidir}/opentelemetry-python-core/tests/util \
                {toxinidir}/opentelemetry-python-core/opentelemetry-instrumentation

prepare.sh reproduces exactly that.

WHY THE CORE SHA IS READ FROM THE REPO, NOT HARDCODED HERE
----------------------------------------------------------
`CORE_REPO_SHA` is committed *inside the repo*, so the value checked out at any
given BASE_COMMIT is by definition the core revision that PR was developed and
tested against. Reading it at build time therefore stays correct for every PR
of this repo, across eras, with no config change -- and it removes the single
most dangerous guess available here. Pinning a version by hand is exactly how
the `maison` and WordPress-core mismatches happened on other configs in this
project: an unpinned or hand-guessed dependency silently resolved to something
the era never used, and the failure surfaced much later as inscrutable test
errors. Here the repo tells us the answer; the config just has to read it.

WHY THE CORE CLONE LIVES OUTSIDE THE REPO
-----------------------------------------
Upstream puts it at `{toxinidir}/opentelemetry-python-core`, i.e. inside the
working tree. Doing that here would leave an untracked directory in the tree,
`git status --porcelain` would report it, and check_git_changes.sh -- which
guards every stage -- would fail. Since we invoke pip directly rather than
through tox, the location is free, so it is cloned to /home/ instead and the
worktree stays pristine.

WHICH TESTS RUN
---------------
The fix patch touches BOTH instrumentation packages:

    instrumentation/opentelemetry-instrumentation-boto/src/.../__init__.py
    instrumentation/opentelemetry-instrumentation-botocore/src/.../__init__.py

(it moves `add_span_arg_tags` / `flatten_dict` out of botocore and into boto,
and switches both to the shared `instrumentation.utils.unwrap`). The test patch
only touches the botocore tests, but boto has its own suite that exercises the
functions being moved. Both suites are therefore run: botocore supplies the
f2p signal, boto supplies the regression coverage that proves the move did not
break the other package. Running the whole monorepo instead is not viable --
every other instrumentation would need its own framework installed (django,
flask, celery, grpc, ...), which is why upstream's tox runs one package per env.
"""

from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# THE TEST COMMAND
#
# Both affected packages, one pytest invocation, identical in all three stages.
# -p no:cacheprovider stops pytest writing .pytest_cache into the tree, which
# would make check_git_changes.sh see a dirty worktree.
# --timeout is deliberately absent: pytest-timeout is not a dependency of this
# repo at this commit, and adding an unpinned plugin to a 2020 dependency set is
# a worse risk than a hung suite. The harness still bounds the container.
# ---------------------------------------------------------------------------
BOTO_TESTS = "instrumentation/opentelemetry-instrumentation-boto/tests"
BOTOCORE_TESTS = "instrumentation/opentelemetry-instrumentation-botocore/tests"
# log_cli=false is NOT cosmetic. pytest.ini at this commit sets `log_cli = true`,
# which prints live log records BETWEEN a test's id and its status:
#
#   tests/test_boto_instrumentation.py::TestBotoInstrumentor::test_double_patch
#   -------------------------------- live log call ---------------------------
#   WARNING  opentelemetry...instrumentor.py:81 Attempting to instrument ...
#   PASSED [ 27%]
#
# parse_pytest_log matches `<id> <STATUS>` on ONE line, so every test that logs
# during its call is dropped. Failures survive by accident, recovered from the
# short summary; PASSES ARE LOST OUTRIGHT. Measured on the previous run: pytest
# reported 23 passed, the parser recorded 19 -- a silent 4-test undercount in
# every stage, in a report that still claims to be valid.
PYTEST_FLAGS = (
    '-v --tb=short --override-ini="addopts=" -o log_cli=false -p no:cacheprovider'
)

# One pytest invocation PER PACKAGE, each with the package directory as CWD.
#
# Both packages ship a `tests/__init__.py`, so pytest derives the module name
# `tests.<file>` for both. Run together from the repo root, the first one to load
# claims the name `tests` and the second dies during collection with
#   ModuleNotFoundError: No module named 'tests.test_botocore_instrumentation'
# Upstream never hits this because scripts/eachdist.py runs pytest once per
# package directory; this does the same.
#
# The two test files have different basenames, so ids stay unique across the two
# runs: `tests/test_boto_instrumentation.py::...` vs
# `tests/test_botocore_instrumentation.py::...`.
#
# The loop deliberately does NOT `set -e`: a package whose tests fail must not
# prevent the other package from running. The worst exit code is propagated so
# callers can still distinguish "tests failed" (1) from "suite could not run"
# (>=2).
RUN_TESTS_SH = """#!/bin/bash
PYTEST_RC=0
for pkg in \
  instrumentation/opentelemetry-instrumentation-boto \
  instrumentation/opentelemetry-instrumentation-botocore
do
  echo "=== multi-swe-bench: pytest in $pkg ==="
  ( cd "$pkg" && python -m pytest tests {flags} )
  rc=$?
  if [ "$rc" -gt "$PYTEST_RC" ]; then
    PYTEST_RC=$rc
  fi
done
exit $PYTEST_RC
""".format(flags=PYTEST_FLAGS)

TEST_CMD = "bash /home/run_tests.sh"

# python:3.8 (NOT -slim). tox runs botocore on py3.6-3.8 and boto on py3.5-3.8,
# so 3.8 is the newest both support at this commit. The full variant is chosen
# over -slim because moto~=1.0 and the 2020 dependency set predate universal
# wheels and need a C toolchain; -slim strips gcc. Verified to ship
# /etc/ssl/certs/ca-certificates.crt (QC item D8), plus git and gcc.
BASE_IMAGE = "python:3.8"

# The core repo, cloned in prepare.sh at the SHA the repo itself pins.
CORE_REPO_URL = "https://github.com/open-telemetry/opentelemetry-python.git"
CORE_DIR = "/home/opentelemetry-python-core"

TOOLCHAIN_SETUP = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates git build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV CI=true
"""


class OpenTelemetryPythonContribImageBase(Image):
    """Level 1: per-PR base image -- toolchain plus the repository checkout.

    Tagged `base-pr-<number>` rather than a shared tag: the base image scrubs
    git history down to one BASE_COMMIT's ancestry, so a shared base stays
    pinned to whichever PR built it first and any later PR whose base commit is
    unreachable from that sha dies in prepare.sh with
    `fatal: unable to read tree`.
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

    def dependency(self) -> str | Image:
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
        # line with clone + checkout + hardening + CMD.
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


class OpenTelemetryPythonContribImageDefault(Image):
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
        return OpenTelemetryPythonContribImageBase(self.pr, self._config)

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

# ---------------------------------------------------------------------------
# The core repo. See the module docstring for why this is required at all --
# setup.cfg pins opentelemetry-api==0.16.dev0, which exists only in the core
# repo's tree, never on PyPI.
#
# The SHA is read from the repo's own CI workflow rather than hardcoded here,
# so it is automatically the core revision THIS commit was developed against.
# ---------------------------------------------------------------------------
CORE_SHA=$(grep -m1 -oE 'CORE_REPO_SHA:[[:space:]]*[0-9a-f]{{40}}' \\
    .github/workflows/test.yml | grep -oE '[0-9a-f]{{40}}')

if [ -z "$CORE_SHA" ]; then
  echo "prepare.sh: FATAL -- could not read CORE_REPO_SHA from" >&2
  echo "prepare.sh: .github/workflows/test.yml. The core repo revision is not" >&2
  echo "prepare.sh: guessable; refusing to continue with a wrong one." >&2
  exit 1
fi
echo "prepare.sh: core repo pinned by this commit -> $CORE_SHA"

git clone {core_url} {core_dir}
cd {core_dir}
git checkout "$CORE_SHA"
cd /home/{pr.repo}

# ---------------------------------------------------------------------------
# Install: core packages first (they satisfy the ==0.16.dev0 pins), then the
# two instrumentation packages under test with their [test] extras.
#
# `|| true` on installs, per the rulebook -- native builds are a common and
# non-fatal failure on the foreign arch. The hard gate below turns a genuinely
# broken install into a loud build failure instead of a silent one.
# ---------------------------------------------------------------------------
pip install --no-cache-dir {core_dir}/opentelemetry-api || true
pip install --no-cache-dir {core_dir}/opentelemetry-sdk || true
pip install --no-cache-dir {core_dir}/tests/util || true
pip install --no-cache-dir {core_dir}/opentelemetry-instrumentation || true

# ORDER IS LOAD-BEARING: botocore BEFORE boto.
#
# At this commit `opentelemetry-instrumentation-boto/setup.cfg` declares
# `opentelemetry-instrumentation-botocore == 0.16.dev0`. That version exists
# only as the sibling source directory below -- it was never published, and
# PyPI's oldest release is 0.12b0. Installing boto first therefore sends pip to
# the index for a version that does not exist and the install dies with
# "No matching distribution found for opentelemetry-instrumentation-botocore
# ==0.16.dev0". Installing botocore first satisfies the pin from the local
# build, and boto then resolves it without touching the network.
#
# `--no-build-isolation` is deliberately NOT used: these are plain setuptools
# packages and the isolated build env is what keeps the 2020 pins honest.
# EDITABLE (-e), and that is the whole point of this block.
#
# A regular install copies the package into site-packages at BUILD time. The
# three grading stages then `git apply` their patches to the source tree at
# /home/{pr.repo} -- a tree nothing imports. The result is silent and wrong: the
# fix stage scores IDENTICALLY to the test stage (measured: 11 passed / 11 failed
# in both), no test goes FAIL -> PASS, and gen_report discards the instance as
# invalid. The fix patch here edits src/ of BOTH packages, so both must be
# editable for the stages to mean anything.
#
# Safe against the tree-cleaning below: `-e` writes *.egg-info, which .gitignore
# already lists, so `git clean -fd` (no -x) leaves it alone and it never shows up
# in `git status --porcelain`.
pip install --no-cache-dir -e "instrumentation/opentelemetry-instrumentation-botocore[test]" || true
pip install --no-cache-dir -e "instrumentation/opentelemetry-instrumentation-boto[test]" || true

# The runner is NOT part of either package's [test] extra -- those declare only
# moto, boto/botocore and opentelemetry-test. pytest has to be installed here or
# every stage dies with "No module named pytest" and reports (0, 0, 0).
#
# Pinned, not floated. dev-requirements.txt says only `pytest!=5.2.3`, which on
# Python 3.8 resolves to a modern 8.x released years after this commit. 6.2.5 is
# the last of the 6.x line and is contemporary with the 0.16 tag.
pip install --no-cache-dir "pytest==6.2.5" || true

# Era-pin the AWS SDK. `moto~=1.0` resolves to 1.3.16 -- correct for this commit
# -- but `botocore~=1.0` resolves to a 2025 release, and the two disagree about
# what regions exist. Modern botocore advertises ap-south-2 and eu-central-2
# (both launched in 2022); moto 1.3.16's availability-zone table is hardcoded and
# predates them, so merely IMPORTING moto raises KeyError: 'ap-south-2' and every
# boto test errors during collection. Installed AFTER the [test] extras so this
# pin wins over the modern botocore they drag in.
pip install --no-cache-dir "botocore==1.19.63" "boto3==1.16.63" "moto==1.3.16" || true

# HARD GATE. Every pip call above ends in `|| true`, which is required for arm64
# resilience but also hides a real failure. Without this check a broken install
# surfaces much later as a collection error in every stage, which reads like a
# repo problem rather than an environment one.
python - <<'PYCHECK'
import sys
missing = []
for mod in ("opentelemetry.trace", "opentelemetry.sdk.trace",
            "opentelemetry.instrumentation.boto",
            "opentelemetry.instrumentation.botocore",
            "moto", "boto", "botocore", "pytest"):
    try:
        __import__(mod)
    except Exception as exc:
        missing.append(f"{{mod}} ({{exc.__class__.__name__}}: {{exc}})")
if missing:
    print("prepare.sh: FATAL -- environment incomplete:", file=sys.stderr)
    for m in missing:
        print("  " + m, file=sys.stderr)
    sys.exit(1)
print("prepare.sh: all required modules import cleanly")
PYCHECK

# The tree must be pristine again before grading: pip may have written egg-info
# or build artefacts into the package directories, and every stage starts with
# `git apply`, which needs the exact BASE_COMMIT state.
git checkout -- .
git clean -fd -e opentelemetry-python-core
bash /home/check_git_changes.sh

# Warm the caches so the three grading stages are fast. Skipped on a foreign
# architecture, where this runs under QEMU at roughly 10x slower and buys
# nothing: grading always happens on the native arch.
if [ "$(uname -m)" = "x86_64" ]; then
  set +e
  {test_cmd} > /tmp/warmup.log 2>&1
  WARMUP_RC=$?
  set -e
  tail -40 /tmp/warmup.log

  # `|| true` here previously swallowed a total failure of the runner: the
  # warm-up printed "No module named pytest", the build succeeded, and all three
  # grading stages reported (0, 0, 0) -- which gen_report rejects as an invalid
  # report with no indication of the cause.
  #
  # pytest exit codes: 0 = all passed, 1 = some tests failed, 2 = interrupted,
  # 3 = internal error, 4 = usage error, 5 = no tests collected. 0 and 1 are BOTH
  # legitimate at the base commit -- the tests this PR fixes are expected to fail
  # here, and treating that as an error would break the benchmark. Anything >= 2
  # means the suite could not be run at all, which is an environment defect.
  if [ "$WARMUP_RC" -ge 2 ]; then
    echo "prepare.sh: FATAL -- pytest could not run the suite (exit $WARMUP_RC)." >&2
    tail -40 /tmp/warmup.log >&2
    exit 1
  fi
else
  echo "prepare.sh: $(uname -m) is not the grading architecture -- skipping the"
  echo "prepare.sh: test warm-up."
fi

""".format(
                    pr=self.pr,
                    test_cmd=TEST_CMD,
                    core_url=CORE_REPO_URL,
                    core_dir=CORE_DIR,
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                RUN_TESTS_SH,
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
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
# LOG PARSING  (pytest -v)
#
#   instrumentation/.../tests/test_botocore_instrumentation.py::TestX::test_y PASSED [ 12%]
#   FAILED instrumentation/.../test_botocore_instrumentation.py::TestX::test_y - AssertionError
#   ERROR  instrumentation/.../test_boto_instrumentation.py
#
# Two things this parser must get right, both learned from real failures on
# other configs in this project:
#
#   * The trailing "[ 12%]" progress counter is VARIABLE between stages -- the
#     same test sits at a different percentage once the test patch adds tests.
#     Left in the id, one test becomes two different names across stages, the
#     cross-stage comparison silently collapses, and p2p collapses with it.
#     It is stripped.
#   * A module that fails to IMPORT produces a bare collection error with no
#     test id and runs nothing, so it would vanish from the counts entirely.
#     It is recorded against the file instead.
# ---------------------------------------------------------------------------

_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_INLINE = re.compile(
    r"^(\S+\.py::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_RE_SUMMARY = re.compile(r"^(?:FAILED|ERROR)\s+(\S+\.py::\S+?)(?:\s|$)")
_RE_COLLECT_ERROR = re.compile(r"^ERROR\s+(\S+\.py)\s*$")

KNOWN_FLAKY_TESTS: frozenset[str] = frozenset()


def parse_pytest_log(log: str) -> TestResult:
    log = _RE_ANSI.sub("", log)

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
            record("FAILED", f"{match.group(1)}::[collection error]")
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("open-telemetry", "opentelemetry-python-contrib")
class OpenTelemetryPythonContrib(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenTelemetryPythonContribImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_pytest_log(test_log)
