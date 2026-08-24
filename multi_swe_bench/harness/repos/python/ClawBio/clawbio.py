import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ClawBio/ClawBio PR #38 ("feat(scrna): add 10x Matrix Market input support").
# The gold test lives in skills/scrna-orchestrator/tests/test_scrna_orchestrator.py
# and drives the Scanpy-based single-cell pipeline end to end
# (QC -> normalize/log1p -> HVG -> PCA -> neighbors -> UMAP -> Leiden -> markers).
#
# The repo has NO setup.py/pyproject.toml -- it ships a top-level requirements.txt
# and a `clawbio` package resolved via cwd (pytest.ini sets `pythonpath = .`), so
# we install with `pip install -r requirements.txt` (NOT `pip install -e .`).
#
# requirements.txt lists biopython/pandas/numpy/scikit-learn/matplotlib/openai but
# NOT the scrna deps the test exercises, so they are installed explicitly:
#   scanpy + anndata  -> the pipeline / AnnData I/O and 10x mtx loading (scipy.io)
#   leidenalg + igraph -> sc.tl.leiden clustering (default doublet/annotate are
#                         "none", so scrublet/CellTypist are NOT needed)
#   umap-learn        -> sc.tl.umap
# MPLBACKEND=Agg keeps sc.pl.* (dotplot/umap/violin) headless in the container.

# Same pytest invocation in all three run scripts (only the applied patches differ),
# scoped to the scrna-orchestrator test tree that contains the two new gold tests --
# this keeps p2p coverage within that skill without pulling in the other skills'
# (unrelated, uninstalled) test suites listed in pytest.ini.
_PYTEST = (
    "python -m pytest -v --no-header -rA --tb=no -p no:cacheprovider "
    "skills/scrna-orchestrator/tests"
)

# CLAWBIO_SCRNA_DEMO_SOURCE=synthetic mirrors the test harness default and keeps
# any demo path off the network; MPLBACKEND=Agg makes matplotlib headless-safe.
_ENV = "export CLAWBIO_SCRNA_DEMO_SOURCE=synthetic\nexport MPLBACKEND=Agg"


class ClawBioImageBase(Image):
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
        # ClawBio @ 2dd6a2e6 is a modern (2025) Scanpy project; scanpy requires
        # Python >=3.10 and leidenalg/igraph/umap-learn ship cp311 wheels.
        return "python:3.11-slim"

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

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential pkg-config \\
    curl wget \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class ClawBioImageDefault(Image):
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
        return ClawBioImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# ClawBio has no setup.py/pyproject: install the repo's runtime requirements,
# then the scrna deps the gold test exercises but requirements.txt omits.
# `|| true` keeps a transient/arch-specific wheel hiccup from aborting the build
# (convention); the hard import gate below then fails loudly if an essential dep
# is genuinely missing.
pip install --no-cache-dir --upgrade pip || true
pip install --no-cache-dir -r requirements.txt || true
# pytest is the test runner (repo ships pytest.ini) but is NOT in requirements.txt
# and is not pulled by scanpy — install it explicitly or the run scripts fail with
# "No module named pytest".
pip install --no-cache-dir scanpy anndata leidenalg igraph umap-learn pytest || true

# Hard gate: the gold end-to-end tests call pytest.importorskip("scanpy"/"anndata"),
# so a missing scrna dep would SILENTLY SKIP the f2p tests (yielding an invalid
# instance with no fail->pass transition) rather than error. Under `set -e` this
# line stops the build if any essential runtime import is absent.
python -c "import pytest, numpy, pandas, scipy, matplotlib, scanpy, anndata, leidenalg, igraph, umap"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{env}
{pytest}

""".format(pr=self.pr, env=_ENV, pytest=_PYTEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{env}
{pytest}

""".format(pr=self.pr, env=_ENV, pytest=_PYTEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{env}
{pytest}

""".format(pr=self.pr, env=_ENV, pytest=_PYTEST),
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


@Instance.register("ClawBio", "ClawBio")
class ClawBio(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ClawBioImageDefault(self.pr, self._config)

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
        # Strip ANSI colour codes first so matching is robust against pytest's
        # coloured output.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest `-v`/`-rA` renders lines as either "<nodeid> STATUS" or
        # "STATUS <nodeid>". A nodeid always carries "::" (file::test), which
        # keeps a wrapped status line or stray output from being mistaken for a
        # test. nodeids here look like
        # "skills/scrna-orchestrator/tests/test_scrna_orchestrator.py::test_x".
        status = r"(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
        pattern1 = re.compile(rf"(\S+::\S+)\s+{status}\b")
        pattern2 = re.compile(rf"\b{status}\s+(\S+::\S+)(?:\s+-\s.*)?$")
        for line in log.splitlines():
            line = line.strip()
            m = pattern1.match(line)
            if m:
                test_name, st = m.group(1), m.group(2)
            else:
                m = pattern2.match(line)
                if not m:
                    continue
                st, test_name = m.group(1), m.group(2)

            if st in ("PASSED", "XFAIL", "XPASS"):
                # xfail_strict is not set, so an expected failure is not a failure.
                passed_tests.add(test_name)
            elif st in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif st == "SKIPPED":
                skipped_tests.add(test_name)

        # Enforce TestResult invariants: the three sets must be disjoint. A test
        # reported more than one way (e.g. passed then errored in teardown)
        # counts as failed.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
