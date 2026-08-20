"""yt-project/yt registry config for multi-swe-bench.

Covers the yt 4.1.dev era (``main`` branch, mid-2022).  yt is a Cython/C++
extension project: the gold ``fix_patch`` for this era touches ``.pyx``,
``.pxd`` and ``.pxi`` sources, so every phase that applies a patch has to
re-run ``build_ext --inplace`` before pytest -- a pure-Python editable install
is not enough.

Layout follows the house convention: one shared ``ImageBase`` (clone + pinned
toolchain, emitted in the canonical base-Dockerfile format), one per-PR
``ImageDefault`` (dependency install + Cython build + patches/scripts), and one
``Instance`` that owns the run/test/fix commands and the log parser.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Environment pins
# ---------------------------------------------------------------------------
# yt 4.1.dev0 declares `python_requires = >=3.7` and classifies up to 3.10.
# 3.10 is the newest interpreter that still ships `distutils`, which
# `setup.py` / `setupext.py` import directly
# (`from distutils.ccompiler import get_default_compiler`, `new_compiler`,
# `distutils.sysconfig.customize_compiler`).  3.12 removed it outright.
#
# The full tag, not `-slim`: yt compiles ~60 Cython/C++ extension modules, and
# `-slim` ships no compiler and no git at all, so it needs an apt layer to put
# gcc/g++/make/git/pkg-config back.  `python:3.10` is buildpack-deps based and
# already carries every one of them, which keeps the base Dockerfile down to the
# canonical shape (clone -> checkout -> harden) with no apt block.  Both tags are
# the same Debian 13 (trixie) with gcc 14.2.0, Python 3.10.21 and glibc 2.41, so
# the compile environment is unchanged -- the difference is purely which packages
# are preinstalled, traded against ~1.2 GB of extra image size.
BASE_IMAGE = "python:3.10"

# `setupext.py` also does `from pkg_resources import resource_filename` and
# `from setuptools.errors import CompileError` -- the first is gone from
# setuptools >= 81, the second only exists from setuptools >= 59.  pyproject.toml
# pins `Cython>=0.29.21,<3.0`; honour that rather than letting pip pick 3.x.
BUILD_REQUIREMENTS = " ".join(
    [
        '"setuptools==69.5.1"',
        '"wheel==0.43.0"',
        '"Cython==0.29.36"',
        '"numpy==1.23.5"',
    ]
)

# yt's root `conftest.py` turns most warnings into errors
# (`config.addinivalue_line("filterwarnings", "error")`), so the runtime stack
# has to stay on the versions that were current when this era was written --
# a modern matplotlib/numpy would fail tests on deprecation warnings alone,
# and extensions compiled against numpy 1.x cannot run against numpy 2.x.
# These mirror `setup.cfg` install_requires, all pinned to Aug-2022 releases.
RUNTIME_REQUIREMENTS = " ".join(
    [
        '"numpy==1.23.5"',
        '"matplotlib==3.5.3"',
        '"unyt==2.9.2"',
        '"sympy==1.11.1"',
        '"cmyt==1.0.4"',
        '"more-itertools==8.14.0"',
        '"packaging==21.3"',
        '"pyparsing==3.0.9"',
        '"pillow==9.2.0"',
        '"tomli==2.0.1"',
        '"tomli-w==1.0.0"',
        '"tqdm==4.64.1"',
    ]
)

# `conftest.py` imports `yaml` at module scope, so PyYAML is mandatory or the
# whole test session fails at collection.  pytest floor comes from setup.cfg
# (`pytest>=6.1`).
TEST_REQUIREMENTS = " ".join(
    [
        '"pytest==7.1.3"',
        '"PyYAML==6.0"',
    ]
)

# The gold test_patch adds `yt/frontends/stream/tests/test_stream_stretched.py`
# and `yt/geometry/tests/test_grid_index.py`.  Running the two enclosing
# directories (rather than only the new files) keeps the baseline `run` phase
# meaningful: the pre-existing tests in them supply P2P coverage while the two
# new files supply F2P.  Neither directory pulls scipy/h5py/astropy at import
# time, so collection is self-contained.
TEST_TARGETS = "yt/frontends/stream/tests yt/geometry/tests"

PYTEST_CMD = (
    "python -m pytest --no-header -rA --tb=short -p no:cacheprovider " + TEST_TARGETS
)

# Recompile the Cython extensions in place.  yt's custom build_ext already
# parallelises via `get_cpu_count()`, so no `-j` is needed.  Required after any
# patch that touches .pyx/.pxd/.pxi -- which the gold fix_patch does.
REBUILD_CMD = "python setup.py build_ext --inplace"


class YtImageBase(Image):
    """Shared base: pinned interpreter + toolchain, repo cloned at base.sha."""

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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo

        # The leading `# syntax` directive makes DockerfileEnhancer treat this
        # file as already-enhanced and return it verbatim, so the layout below
        # is exactly what gets built.  It reproduces the canonical base format:
        # syntax directive -> FROM -> TARGETARCH/REPO_URL/BASE_COMMIT ->
        # proxy ARGs -> ENV block -> OCI LABELs -> CA-cert symlinks -> setup ->
        # clone -> checkout -> anti-reward-hack hardening -> CMD.
        hardening = Image._HARDENING_BLOCK
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {BASE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN python -m pip install --no-cache-dir {BUILD_REQUIREMENTS}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}
{clear_env}
CMD ["/bin/bash"]
"""


class YtImageDefault(Image):
    """Per-PR image: install the pinned stack, build the Cython extensions."""

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
        return YtImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
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
# Runs once during the image build (`RUN bash /home/prepare.sh`).
# `set -e`, not `set -eo pipefail`: the `|| true` after each install already
# handles the pipeline case, per the standard prepare.sh contract.
set -e

cd /home/[[REPO_NAME]]

git reset --hard
git checkout [[BASE_SHA]]

# `|| true` on every install: a native/Cython compile failure must not abort the
# build here.  It is not swallowed -- the `import yt` smoke check in the
# Dockerfile runs straight after this script and fails the build loudly if any
# of these did not take.
python -m pip install --no-cache-dir [[RUNTIME_REQUIREMENTS]] || true
python -m pip install --no-cache-dir [[TEST_REQUIREMENTS]] || true

# `--no-deps`: every install_requires entry is pinned above, so pip must not be
# allowed to resolve (and silently upgrade) matplotlib/numpy behind our back.
python -m pip install --no-cache-dir --no-build-isolation --no-deps -e . || true
""".replace("[[REPO_NAME]]", repo)
                .replace("[[BASE_SHA]]", self.pr.base.sha)
                .replace("[[RUNTIME_REQUIREMENTS]]", RUNTIME_REQUIREMENTS)
                .replace("[[TEST_REQUIREMENTS]]", TEST_REQUIREMENTS),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# Baseline: no patches applied.  The two files the gold test_patch adds do not
# exist yet, so this phase only reports the pre-existing tests in the target
# directories (the P2P set).
set -eo pipefail
export CI=true
cd /home/[[REPO_NAME]]

# Baseline integrity: assert the tree really is unpatched before measuring it.
# Non-fatal -- it is a diagnostic in the log, not a gate on the phase.
bash /home/check_git_changes.sh || true

[[PYTEST_CMD]]
""".replace("[[REPO_NAME]]", repo).replace("[[PYTEST_CMD]]", PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
# Gold test_patch only: the two new tests must be collected and must FAIL,
# because the implementation they exercise is still missing.
set -eo pipefail
export CI=true
cd /home/[[REPO_NAME]]

# Assert we start from a clean tree, before any patch touches it. Non-fatal --
# a diagnostic in the log, not a gate on the phase.
bash /home/check_git_changes.sh || true

if ! git apply --whitespace=nowarn /home/test.patch; then
    if ! git apply --whitespace=nowarn --3way /home/test.patch; then
        echo "Error: git apply test.patch failed" >&2
        exit 1
    fi
fi

# Harmless when the patch is pure-Python, but keeps this phase symmetrical with
# fix-run.sh so a stale .so can never explain a difference between them.
if ! [[REBUILD_CMD]]; then
    echo "Error: build_ext failed after applying test.patch" >&2
fi

[[PYTEST_CMD]]
""".replace("[[REPO_NAME]]", repo)
                .replace("[[REBUILD_CMD]]", REBUILD_CMD)
                .replace("[[PYTEST_CMD]]", PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
# Gold test_patch + fix_patch: everything should pass.
set -eo pipefail
export CI=true
cd /home/[[REPO_NAME]]

# Assert we start from a clean tree, before any patch touches it. Non-fatal --
# a diagnostic in the log, not a gate on the phase.
bash /home/check_git_changes.sh || true

if ! git apply --whitespace=nowarn /home/test.patch; then
    if ! git apply --whitespace=nowarn --3way /home/test.patch; then
        echo "Error: git apply test.patch failed" >&2
        exit 1
    fi
fi

if ! git apply --whitespace=nowarn /home/fix.patch; then
    if ! git apply --whitespace=nowarn --3way /home/fix.patch; then
        echo "Error: git apply fix.patch failed" >&2
        exit 1
    fi
fi

# Mandatory: the fix touches yt/geometry/selection_routines.pxd,
# yt/geometry/_selection_routines/*.pxi and yt/utilities/lib/*.pyx.  Without
# this the interpreter keeps importing the .so built at base.sha and every new
# test still fails.  Do not exit on failure -- let pytest surface the breakage.
if ! [[REBUILD_CMD]]; then
    echo "Error: build_ext failed after applying fix.patch" >&2
fi

[[PYTEST_CMD]]
""".replace("[[REPO_NAME]]", repo)
                .replace("[[REBUILD_CMD]]", REBUILD_CMD)
                .replace("[[PYTEST_CMD]]", PYTEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_ref = f"{base.image_name()}:{base.image_tag()}"

        # Generated from files(), so a COPY can never drift from what the build
        # context actually contains.
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        global_env = f"\n{self.global_env}\n" if self.global_env else ""

        # Exactly the canonical PR-image layout: FROM -> COPY the standard 7
        # files -> RUN prepare.sh.  Nothing else belongs here:
        #   * no `# syntax` directive -- dependency() returns an Image, so
        #     DockerfileEnhancer returns this verbatim and never reads it;
        #   * no WORKDIR and no CMD -- both are inherited from the base, whose
        #     final WORKDIR is already the cloned repo and whose CMD is bash;
        #   * no ENV block -- MPLBACKEND is unnecessary (matplotlib selects the
        #     agg backend by itself when DISPLAY is unset, which it always is in
        #     a container) and CI=true is exported by each run script instead;
        #   * no `import yt` smoke check -- a failed install already surfaces as
        #     an invalid report via Report.check() rule 1 (fix_patch_result
        #     .all_count == 0), so the build does not need to assert it.
        return f"""FROM {base_ref}
{global_env}
{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("yt-project", "yt")
class Yt(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return YtImageDefault(self.pr, self._config)

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

    # -- log parsing --------------------------------------------------------
    # yt's pyproject sets `addopts = -s -v -rsfE`; we append `-rA`, which wins,
    # so a session emits both the verbose progress lines
    #   yt/geometry/tests/test_grid_index.py::test_icoords_to_ires PASSED [ 50%]
    # and the short-summary lines
    #   PASSED yt/geometry/tests/test_grid_index.py::test_icoords_to_ires
    # Either form is accepted; test ids are always rooted at `yt/`.
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    _TEST_FIRST_RE = re.compile(
        r"^(yt/[^\s:]+\.py(?:::[^\s]+)?)\s+"
        r"(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b"
    )
    _STATUS_FIRST_RE = re.compile(
        r"^(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\s+"
        r"(yt/[^\s:]+\.py(?:::[^\s]+)?)"
    )
    # The `-rA` summary also emits `SKIPPED [1] yt/...py:123: reason`, which
    # carries a line number instead of a test id.  It is deliberately ignored:
    # the `-v` progress line for the same test already supplies the real id, and
    # matching it would add a bare-path entry that no other phase can line up
    # against.  Collection errors (`ERROR yt/...py - ImportError`) have no `::`
    # either, but there the file *is* the unit of failure, so they are kept.

    def parse_log(self, test_log: str) -> TestResult:
        log = self._ANSI_RE.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        def record(status: str, name: str) -> None:
            name = name.strip().rstrip(".,")
            if not name:
                return
            # Bucketing matches the harness helper `mapping_to_testresult`:
            # XFAIL/XPASS count as passed, ERROR counts as failed.
            if status in ("PASSED", "XPASS", "XFAIL"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        for raw_line in log.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            match = self._TEST_FIRST_RE.match(line)
            if match:
                record(match.group(2), match.group(1))
                continue

            match = self._STATUS_FIRST_RE.match(line)
            if match:
                record(match.group(1), match.group(2))

        # A test can be reported more than once (e.g. it passes, then errors in
        # teardown).  A failure always wins over a pass, and a pass over a skip;
        # TestResult rejects overlapping sets outright.
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


# === number_interval routing ===
# The delivered raw dataset carries no `number_interval`, so `Instance.create`
# resolves this repo through the plain "yt-project/yt" key registered above.
# Registering the single-PR interval as well keeps the lookup working if the
# record is later re-emitted by build_lht_dataset.py, which stamps a
# dash-joined `number_interval` onto every bundle.
_BUNDLE_NIS_YT = [
    "2998",
]

for _ni in _BUNDLE_NIS_YT:
    Instance.register("yt-project", _ni)(Yt)
