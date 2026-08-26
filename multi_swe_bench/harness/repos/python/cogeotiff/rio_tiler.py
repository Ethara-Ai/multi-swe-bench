"""Repo config for cogeotiff/rio-tiler (Python / pytest).

Runner
------
``tox.ini`` drives CI, and its one test command is::

    python -m pytest --cov rio_tiler --cov-report xml --cov-report term-missing \\
        --benchmark-skip --ignore=venv

The run scripts keep ``--benchmark-skip`` and drop the coverage flags: coverage
adds runtime and writes ``coverage.xml`` for a reporter nothing here consumes.
``--benchmark-skip`` is *not* optional -- ``pytest-benchmark`` is installed via
the ``test`` extra and without the flag its benchmark tests run for minutes and
report timing-dependent results, which is exactly the kind of cross-stage
instability ``Report.check()`` rule 2 exists to catch.

Dependencies come from ``.[dev]`` rather than ``.[test]``. The two extras differ
by one package that matters here: ``pytest-asyncio`` is in ``dev`` only, and
``tests/test_io_async.py`` -- one of the five files the gold test patch edits --
is built entirely on ``async def test_async()``. With ``.[test]`` those cases do
not run at all.

Test identity
-------------
pytest node IDs already have the required ``<source file>::<test name>`` shape,
so ``parse_log`` reports them verbatim::

    tests/test_io_cogeo.py::test_area_valid

No marker scaffolding needed; ``-v`` prints the node ID and outcome on one line
and the ID is rootdir-relative, i.e. relative to the repo root.

Toolchain
---------
``python:3.8``:

* The CI matrix at this commit is ``[3.6, 3.7, 3.8]`` and ``setup.py`` declares
  ``python_requires=">=3.6"``. 3.8 is the newest version upstream tests.
* On amd64 no apt layer is strictly needed -- ``rasterio``'s manylinux wheel
  bundles GDAL, and the suite was first verified on a stock ``python:3.8`` with
  nothing but pip packages. ``prepare.sh`` installs ``libgdal-dev`` anyway,
  purely so the **arm64** leg can build rasterio from sdist; there is no
  linux-aarch64 wheel for the pinned version. On amd64 pip still prefers the
  wheel, so that apt layer costs build time and nothing else.

Dependency pinning
------------------
The base commit is **17 Feb 2021** and ``setup.py`` pins almost nothing --
``pydantic``, ``numpy``, ``pystac`` and ``rasterio>=1.1.7`` are all open-ended.
A natural install today resolves to versions that cannot even import the
package, so four constraints are applied *after* ``.[dev]`` (which pulls latest
first, then gets constrained back):

``rasterio>=1.2,<1.3``
    The load-bearing one, and the least obvious. ``morecantile`` -- which
    ``setup.py`` *does* pin, to ``>=2.1,<2.2`` -- declares
    ``class CRSType(CRS, AnyHttpUrl)`` where ``CRS`` is ``rasterio.crs.CRS``.
    In rasterio 1.3 that became a C extension type, so the multiple inheritance
    now raises ``TypeError: multiple bases have instance lay-out conflict`` at
    import. It presents as a pydantic problem and is not one.

``pydantic>=1.7,<1.9``
    ``morecantile`` 2.1 uses pydantic v1 idioms. Under pydantic 2 it dies on
    ``Field(..., const=True)`` (``const`` removed); under 1.10 the ``CRSType``
    inheritance above breaks for a second, unrelated reason.

``pystac<1``
    ``rio_tiler.io.stac`` targets the 0.5 API; pystac 1.0 (Jun 2021) reshaped it.

``numpy<1.24``
    1.24 removed the deprecated scalar aliases and predates the rasterio 1.2
    wheels' build environment.

Verified resolution: rasterio 1.2.10, pydantic 1.8.2, pystac 0.5.6,
numpy 1.23.5, morecantile 2.1.4.

Gold signal
-----------
Not a new test -- an expectation rewrite, which is why a "did the test patch add
a test function?" screen misreads this PR as ungradable.

The fix corrects bounds so they align to the raster's internal block grid
(``rio_tiler/utils.py``, ``rio_tiler/reader.py``), which shifts output array
shapes by one pixel in several places. The test patch updates ~30 assertions
across five files to the corrected values::

    -assert data.shape == (1, 11, 41)        -assert vrt_width == 100
    +assert data.shape == (1, 11, 40)        +assert vrt_width == 99

so the old code fails the new assertions and the fixed code satisfies them.
Measured before this config was written, by applying the patches by hand in a
stock ``python:3.8`` container:

===========================  ==========================================
stage                        result
===========================  ==========================================
run (no patches)             103 passed, 64 skipped, **0 failed**
test (test.patch)            94 passed, 64 skipped, **9 failed**
fix (test.patch+fix.patch)   103 passed, 64 skipped, **0 failed**
===========================  ==========================================

A clean ``f2p = 9`` on a healthy baseline. The 64 skips are stable across all
three stages -- they are the tests gated on credentials/network that skip
themselves rather than fail.
"""

import re
import shlex
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)

# `.+?` rather than `\S+?` for the node ID: pytest parametrisation embeds the
# repr of the parameter, so `test_x[a b]` is a legitimate node ID containing
# spaces. Anchoring on `^` plus the `.py::` infix keeps this from matching the
# trailing short-summary lines, which lead with the status instead.
_VERBOSE_RE = re.compile(
    r"^(?P<nodeid>\S+\.py::.+?)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)",
    re.MULTILINE,
)

_SUMMARY_RE = re.compile(
    r"^(?P<status>FAILED|ERROR)\s+(?P<nodeid>\S+\.py::.+?)(?:\s+-\s|\s*$)",
    re.MULTILINE,
)


def _gold_test_exclude_flags(test_patch: str) -> str:
    """``git apply --exclude`` flags for every file the gold test patch touches.

    Reward-hacking guard, defence in depth for
    ``test_result.fix_patch_tampers_with_tests``: that pre-run check reads
    ``get_modified_files``, which drops entries whose ``---`` side is
    ``/dev/null`` and is therefore blind to gold tests the test patch
    *creates*. Both halves are collected here.
    """
    text = (test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    paths = {m.group(2) for m in _DIFF_GIT_RE.finditer(text)}
    paths |= set(get_modified_files(test_patch or ""))
    return " ".join(f"--exclude={shlex.quote(p)}" for p in sorted(paths))


# `|| pytest_status=$?` rather than `|| true`: a non-zero exit is the *expected*
# outcome of the test stage, so the script must survive it -- but the code
# itself is the verdict and is preserved verbatim, which `|| true` would
# discard. It keeps 1 ("tests failed") distinguishable from 2/127 ("pytest never
# started"), which would otherwise reach parse_log as an indistinguishable
# empty log.
_RUN_PYTEST = """pytest_status=0
timeout -k 60 1800 python -m pytest -v --color=no --benchmark-skip \\
    -p no:cacheprovider --continue-on-collection-errors 2>&1 || pytest_status=$?
printf '##### MSWEB-PYTEST-EXIT: %s\\n' "$pytest_status\""""


class RioTilerImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- clones the repo on top of Python 3.8.

    Tagged per PR rather than with a bare ``:base``: a single shared tag would
    be rewritten by every other instance of this repo, silently changing the
    foundation an already-verified instance was built against.

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance``
    rewrites the ``git clone`` line below into the standard
    clone + ``checkout ${BASE_COMMIT}`` + hardening sequence and supplies
    ``REPO_URL`` / ``BASE_COMMIT`` as build args.
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
        return "python:3.8"

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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class RioTilerImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT and installs the era-correct dependency set."""

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
        return RioTilerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        gold_excludes = _gold_test_exclude_flags(self.pr.test_patch)

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
# `-e` is load-bearing, not decoration. Without it the three
# `check_git_changes.sh` calls below are advisory: a dirty tree or a failed
# checkout prints its complaint, the script runs on, and the image builds
# green -- an assertion that cannot fail the build is not an assertion. The
# commands that are *allowed* to fail carry their own `|| true`.
set -euxo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

python -m pip install --upgrade pip

# GDAL headers, for arm64's benefit only. rasterio 1.2.10 publishes 8 wheels and
# every one is x86_64 or macOS -- there is no linux-aarch64 wheel, and the
# `rasterio<1.3` pin below cannot be relaxed to reach one (see the comment
# there). So on arm64 pip must build rasterio from sdist, which needs
# gdal-config and the GDAL headers. rio-color 1.0.4 is in the same position.
#
# On amd64 this changes nothing: pip still prefers the manylinux wheel and never
# compiles. Verified that rasterio 1.2.10 does build against bookworm's GDAL
# 3.6.2 before this was added, rather than assuming a 2021 release would accept
# a 2023 GDAL.
apt-get update
apt-get install -y --no-install-recommends libgdal-dev gdal-bin
rm -rf /var/lib/apt/lists/*
gdal-config --version

# `.[dev]`, not `.[test]`: the two extras differ by pytest-asyncio, and
# tests/test_io_async.py -- one of the five files the gold test patch edits --
# is entirely `async def`. Under `.[test]` those cases never run.
#
# Retried once, then tolerated. This is NOT where correctness is decided; the
# import gate below is.
pip install --no-cache-dir -e ".[dev]" \\
    || pip install --no-cache-dir -e ".[dev]" \\
    || true

# setup.py leaves pydantic / numpy / pystac / rasterio open-ended, and this
# commit is from Feb 2021, so the line above resolves to versions that cannot
# import the package at all. Constrain it back afterwards -- installing these
# first would just let `.[dev]` pull them forward again.
#
# rasterio<1.3 is the load-bearing pin and the least obvious: morecantile
# (which setup.py *does* pin, to >=2.1,<2.2) declares
# `class CRSType(CRS, AnyHttpUrl)` over rasterio.crs.CRS, and CRS became a C
# extension type in rasterio 1.3 -- so the multiple inheritance now raises
# `TypeError: multiple bases have instance lay-out conflict` at import time.
# It presents as a pydantic failure and is not one.
pip install --no-cache-dir \\
    'rasterio>=1.2,<1.3' \\
    'pydantic>=1.7,<1.9' \\
    'pystac<1' \\
    'numpy<1.24' \\
    || true

# The real assertions, deliberately not tolerant. A bad resolution leaves the
# package un-importable, which would yield an identical collection error in all
# three stages -- Report.check() would reject on `all_count == 0` only after
# three full stage runs. Failing here turns that into one legible build error.
python -c "import rio_tiler; print(rio_tiler.__version__)"
python -c "import rasterio, pydantic, pystac, numpy, morecantile"
python -m pytest --version

# .gitignore covers *.egg-info/, __pycache__/ and the coverage files, so an
# editable install leaves the tree pristine. A dirty tree here means an install
# wrote into a tracked path, and every later `git apply` would then be laid on
# top of unexplained edits.
#
# Deliberately the last command: no `exit 0` follows it, so the script's exit
# status *is* this check's status.
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

{run_pytest}
""".format(pr=self.pr, run_pytest=_RUN_PYTEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_pytest}
""".format(pr=self.pr, run_pytest=_RUN_PYTEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

# Canonical stage order: gold tests first, fix patch on top.
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# At evaluation time this patch is the *agent's*, so it is applied with every
# gold test file excluded -- a fix patch that edits the tests grading it cannot
# take effect. The gold fix patch touches only CHANGES.md and two rio_tiler
# modules, so the exclusions are a no-op for dataset generation.
if ! git apply --whitespace=nowarn {gold_excludes} /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_pytest}
""".format(
                    pr=self.pr,
                    gold_excludes=gold_excludes,
                    run_pytest=_RUN_PYTEST,
                ),
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

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


def parse_pytest_verbose(test_log: str) -> TestResult:
    """Classify every test in a ``pytest -v`` log by its node ID.

    Two passes over the same log, because one is not enough:

    * ``_VERBOSE_RE`` reads the per-test progress lines, which is where every
      normally-collected test appears.
    * ``_SUMMARY_RE`` reads the trailing ``short test summary info`` block,
      the only place a *collection* error surfaces with a node ID --
      ``--continue-on-collection-errors`` keeps the run alive past one, but the
      affected tests never produce a progress line.

    The summary pass runs second and never overwrites a verdict already
    recorded: it repeats failures the first pass has already classified, so
    letting it win would be a no-op at best and an ``XFAIL`` downgrade at worst.

    ANSI is stripped first. The run scripts pass ``--color=no``, so in practice
    the log arrives clean -- but both patterns anchor on ``^``, and one leaked
    escape at the start of a line would silently drop every match rather than
    fail loudly, which is the worst possible failure mode for a parser.
    """
    text = ANSI_ESCAPE.sub("", test_log or "")

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    def record(nodeid: str, status: str) -> None:
        if nodeid in passed or nodeid in failed or nodeid in skipped:
            return
        if status in ("PASSED", "XPASS"):
            passed.add(nodeid)
        elif status in ("FAILED", "ERROR"):
            failed.add(nodeid)
        elif status in ("SKIPPED", "XFAIL"):
            skipped.add(nodeid)

    for m in _VERBOSE_RE.finditer(text):
        record(m.group("nodeid"), m.group("status"))

    for m in _SUMMARY_RE.finditer(text):
        record(m.group("nodeid"), m.group("status"))

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("cogeotiff", "rio-tiler")
class RioTiler(Instance):
    """Instance handler for cogeotiff/rio-tiler.

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
        return RioTilerImageDefault(self.pr, self._config)

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
        return parse_pytest_verbose(test_log)
