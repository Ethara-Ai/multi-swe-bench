import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# --------------------------------------------------------------------------- #
# scrapy/scrapy - consolidated registry.
#
# The pre-existing per-era modules (scrapy_<hi>_to_<lo>.py) could not route: the
# dataset carries no number_interval, so Instance.create looks up "scrapy/scrapy",
# which none of them registered. Their PR-number ranges also overlap heavily
# (PR 4803 falls inside three of them), so there was no deterministic dispatch.
#
# This module keeps each era's proven toolchain and test command but:
#   * registers "scrapy/scrapy" and dispatches by an explicit lead-PR -> era map
#     (built by assigning each PR to the NARROWEST era range containing it),
#   * registers every dash-joined bundle number_interval for delivery routing,
#   * uses one shared base image per era instead of a full clone+install per PR,
#   * follows the PIPELINE.md reference format (syntax opt-out so the enhancer
#     injects no proxy/CA scaffolding, ARG TARGETARCH/REPO_URL, TZ, ethara LABEL,
#     canonical hardening pinned to the literal base.sha, CMD),
#   * uses the mswebench image prefix required by the ECR tar naming convention.
#
# Interpreter note: four eras originally used `FROM ubuntu:latest` plus the
# deadsnakes PPA to obtain Python 3.8 inside a venv. That is a floating base tag,
# depends on a third-party PPA, and deadsnakes does not publish 3.8 for current
# Ubuntu releases, so it is neither reproducible nor reliably multi-arch. Those
# eras use the official multi-arch `python:3.8-slim` image instead; the Python
# version and the installed package set are unchanged.
# --------------------------------------------------------------------------- #

# Lead PR -> era key. Explicit because the era ranges overlap.
_ERA_OF_PR = {
    2215: "scrapy_2655_to_2215",
    2510: "scrapy_2655_to_2215",
    2639: "scrapy_2655_to_2215",
    2655: "scrapy_2655_to_2215",
    2918: "scrapy_5328_to_2918",
    # pr-3426 is NOT a 3722_to_3377 record despite its lead number falling in
    # that range. Its base commit is 2024-11-18 and declares
    # python_requires=">=3.9", while its nominal era-mates are 2018 and 2020
    # commits on python:3.8-slim -> `pip install -e .` fails outright with
    # "Package 'scrapy' requires a different Python: 3.8.20 not in '>=3.9'".
    # Its bundle runs to PR 6780, so it belongs with the 2024-era toolchain.
    3426: "scrapy_6240_to_6240",
    3550: "scrapy_3722_to_3377",
    3667: "scrapy_3722_to_3377",
    3873: "scrapy_4036_to_3824",
    4436: "scrapy_4686_to_4406",
    4716: "scrapy_5118_to_4716",
    4803: "scrapy_4803_to_4803",
    5118: "scrapy_5118_to_4716",
    5260: "scrapy_5260_to_5260",
    5328: "scrapy_5328_to_2918",
    5436: "scrapy_5528_to_5405",
    5589: "scrapy_5589_to_5589",
    6240: "scrapy_6240_to_6240",
    # pr-6769 (base commit 2025-11-17) declares requires-python ">=3.9" but its
    # FIX patch imports typing.TypeAlias (Python 3.10+) and its patched conftest
    # needs pytest-twisted's --reactor option, which only registers correctly on
    # the newer plugin in this era. On python:3.9 the fix stage dies with
    # "ImportError: cannot import name 'TypeAlias'". Proven in-container on 3.11:
    # test stage collects 2116, fix stage 2316. Same wrong-era-Python issue as
    # pr-3426, so it moves to the 3.11 toolchain; the self-healing --reactor probe
    # (enabled for this era) installs pytest-twisted only at the patched stages.
    6769: "scrapy_6240_to_6240",
    6911: "scrapy_6911_to_6748",
    7212: "scrapy_7395_to_6912",
}

_APT_FULL = (
    "build-essential python3-dev libssl-dev libffi-dev "
    "libxml2-dev libxslt1-dev zlib1g-dev"
)
_APT_LIBS = "build-essential libssl-dev libxml2-dev libxslt1-dev zlib1g-dev"

# Long pytest invocation shared by the coverage-based eras.
_COV = (
    "pytest -v --cov-config=pyproject.toml --cov=scrapy --cov-report="
    " --cov-report=term-missing --cov-report=xml --junitxml=testenv.junit.xml"
    " -o junit_family=legacy --durations=10"
)

# Per era: base OS image, apt packages, pip steps (run per PR after checkout,
# because several of them read files out of the checked-out tree), test command.
_ERA_SPEC = {
    "scrapy_2655_to_2215": {
        "base_os": "python:3.8-slim",
        "apt": f"{_APT_FULL}",
        "pip": [
            "pip install --upgrade pip",
            "if [ -f requirements-py3.txt ]; then pip install -r requirements-py3.txt; fi",
            "pip install pytest six parsel testfixtures PyDispatcher pillow"
            " 'Twisted<22.0.0' pyOpenSSL==20.0.1 cryptography==3.4.8",
        ],
        "test": "pytest -v tests/",
    },
    "scrapy_3722_to_3377": {
        "base_os": "python:3.8-slim",
        "apt": f"{_APT_LIBS}",
        "pip": [
            "pip install --upgrade pip",
            "if [ -f requirements-py3.txt ]; then pip install -r requirements-py3.txt; fi",
            # pytest-twisted IS required here, but only for the base stage: this
            # era's unpatched conftest reads --reactor (registered by the plugin),
            # while the TEST PATCH adds its own addoption for it - so with the
            # plugin still loaded the patched stages die with
            # "argparse.ArgumentError: argument --reactor: conflicting option
            # string". The requirement inverts once the patch is applied, so
            # test-run.sh / fix-run.sh uninstall the plugin after patching (see
            # _UNINSTALL_PYTEST_TWISTED). service_identity is pinned for the same
            # cryptography.hazmat.asn1 mismatch fixed in era 5528_to_5405.
            "pip install 'Twisted==21.7.0' 'pyOpenSSL==21.0.0' 'cryptography==3.4.8'"
            " 'service_identity==23.1.0' testfixtures Pillow pytest pytest-twisted",
            # scrapy itself must be installed. Two of this era's three records
            # (pr-3426, pr-3667) have no requirements-py3.txt, so the guarded
            # install above is skipped and scrapy's own dependencies (parsel,
            # w3lib, queuelib, ...) never arrive; pr-3550 has the file but still
            # ends up without parsel. Every subprocess-based test then dies with
            # "ModuleNotFoundError: No module named 'parsel'" - pr-3550 collected
            # 110 tests with 162 errors instead of the ~1200 its era-mates run.
            "pip install -e .",
            # Re-pin after `-e .` for the same reason as era 4036_to_3824: the
            # editable install resolves scrapy's own >= bounds and would other-
            # wise upgrade these past the versions this era needs.
            "pip install 'Twisted==21.7.0' 'pyOpenSSL==21.0.0' 'cryptography==3.4.8'",
        ],
        "test": "pytest tests -v",
    },
    "scrapy_4686_to_4406": {
        "base_os": "python:3.8-slim",
        "apt": f"{_APT_FULL}",
        "pip": [
            "pip install --upgrade pip",
            "pip install -e . twisted==20.3.0 pyOpenSSL==19.1.0 cryptography==3.4.8"
            " testfixtures Pillow pytest-twisted pytest",
        ],
        "test": "pytest tests/ --verbose --no-header -rA --tb=no -p no:cacheprovider",
    },
    "scrapy_4803_to_4803": {
        "base_os": "python:3.8-slim",
        "apt": f"{_APT_FULL}",
        "pip": [
            "pip install --upgrade pip",
            "if [ -f tests/requirements-py3.txt ]; then"
            " pip install -e . -r tests/requirements-py3.txt;"
            " else pip install -e .; fi",
            # Twisted pinned: this era's setup.py only says Twisted>=17.9.0, so an
            # unpinned install pulls a 2025+ release whose _sslverify needs
            # OpenSSL.SSL.TLS_METHOD, absent from the pyOpenSSL 19.1.0 pinned here
            # ("AttributeError: module 'OpenSSL.SSL' has no attribute
            # 'TLS_METHOD'" at pytest_configure). 20.3.0 matches the sibling
            # 4686_to_4406 era and collects 2330 tests.
            "pip install 'Twisted==20.3.0' pyOpenSSL==19.1.0 cryptography==3.4.8"
            " testfixtures Pillow pytest-twisted pytest",
        ],
        "test": "pytest tests/ --verbose --no-header -rA --tb=no -p no:cacheprovider",
    },
    "scrapy_4036_to_3824": {
        "base_os": "python:3.9-slim",
        "apt": f"{_APT_FULL}",
        "pip": [
            "if [ -f requirements-py3.txt ]; then pip install -r requirements-py3.txt; fi",
            "pip install pyOpenSSL==19.1.0 Twisted==19.10.0 cryptography==3.4.8 Pillow"
            " testfixtures w3lib==1.19.0 pytest",
            "pip install -e .",
            # Re-pin AFTER `-e .`: setup.py declares cryptography>=2.0, so the
            # editable install silently upgrades cryptography past the pin above
            # to a release that dropped lib.GEN_EMAIL, which pyOpenSSL 19.1.0
            # imports at module load -> every test errors (0 passed / 442 failed).
            # pytest-twisted supplies the --reactor option this era's conftest
            # reads; without it collection dies with "no option named '--reactor'".
            "pip install 'cryptography==3.4.8' 'pyOpenSSL==19.1.0' pytest-twisted",
        ],
        "test": "pytest -v scrapy tests",
    },
    "scrapy_5328_to_2918": {
        "base_os": "python:3.9-slim",
        "apt": f"{_APT_FULL}",
        "pip": [
            "if [ -f tests/requirements.txt ]; then pip install -r tests/requirements.txt; fi",
            "pip install -e .",
            # Probed: unpinned pulls Twisted 26.4.0 -> 8 collection errors.
            # 22.10.0 collects 3006 tests with none.
            "pip install 'Twisted==22.10.0'",
        ],
        "test": (
            "pytest -v --cov=scrapy --cov-report=xml --cov-report= --durations=10"
            " docs scrapy tests"
        ),
    },
    "scrapy_5589_to_5589": {
        "base_os": "python:3.9-slim",
        "apt": f"{_APT_FULL}",
        "pip": [
            "if [ -f tests/requirements.txt ]; then pip install -r tests/requirements.txt; fi",
            "pip install -e .",
            # Twisted pinned: setup.py says only Twisted>=18.9.0, so an unpinned
            # install pulled 26.4.0, which no longer exports the _sslverify
            # internals scrapy imports at this commit (_setAcceptableProtocols,
            # verifyHostname) -> 14 collection errors. 22.10.0 collects 3004 tests.
            "pip install 'Twisted==22.10.0'",
        ],
        "test": (
            "pytest -v --cov=scrapy --cov-report=xml --cov-report= --durations=10"
            " docs scrapy tests"
        ),
    },
    "scrapy_5118_to_4716": {
        "base_os": "python:3.9-slim",
        "apt": f"{_APT_FULL}",
        "pip": [
            "if [ -f tests/requirements.txt ]; then pip install -r tests/requirements.txt; fi",
            "pip install -e .",
            # Probed: unpinned pulls Twisted 26.4.0 -> 4 collection errors.
            # 22.10.0 collects 3057 tests with none.
            "pip install 'Twisted==22.10.0'",
        ],
        "test": (
            "pytest -v --cov=scrapy --cov-report=xml --cov-report= --durations=10"
            " docs scrapy tests --doctest-modules"
        ),
    },
    "scrapy_5528_to_5405": {
        "base_os": "python:3.9-slim",
        "apt": f"{_APT_FULL}",
        "pip": [
            "if [ -f tests/requirements.txt ]; then"
            " pip install -e . -r tests/requirements.txt;"
            " else pip install -e .; fi",
            # Probed: unpinned pulls Twisted 26.4.0 -> 57 collection errors, and a
            # modern service_identity imports cryptography.hazmat.asn1, absent from
            # the cryptography 3.4.8 pinned here. Twisted 22.1.0 +
            # service_identity 23.1.0 collect 2809 tests with none.
            "pip install pytest pyOpenSSL==21.0.0 cryptography==3.4.8"
            " 'Twisted==22.1.0' 'service_identity==23.1.0'",
        ],
        "test": "pytest -v tests",
    },
    "scrapy_6911_to_6748": {
        "base_os": "python:3.9-slim",
        "apt": f"{_APT_LIBS}",
        "pip": [
            # pytest-twisted omitted deliberately: scrapy at this era registers its
            # own --reactor pytest option, and loading the plugin too raises
            # "argparse.ArgumentError: argument --reactor: conflicting option
            # string" before any test runs. Without it 3134 tests collect cleanly.
            "pip install attrs coverage>=7.4.0 pexpect>=4.8.0 pyftpdlib>=2.0.1 pygments"
            " pytest pytest-cov>=4.0.0 pytest-xdist sybil>=1.3.0 testfixtures",
            "pip install -e .",
        ],
        "test": f"{_COV} docs scrapy tests --doctest-modules",
    },
    "scrapy_5260_to_5260": {
        "base_os": "python:3.11-slim",
        "apt": f"{_APT_LIBS}",
        "pip": [
            "pip install attrs coverage httpx pexpect>=4.8.0 pyftpdlib>=1.5.8 pygments"
            " pytest pytest-cov pytest-xdist sybil>=1.3.0 testfixtures pytest-twisted",
            "pip install -e .",
        ],
        "test": f"{_COV} scrapy tests --doctest-modules",
    },
    "scrapy_6240_to_6240": {
        "base_os": "python:3.11-slim",
        "apt": f"{_APT_LIBS}",
        "pip": [
            "if [ -f tests/requirements.txt ]; then pip install -r tests/requirements.txt; fi",
            "pip install -e .",
            # Probed: unpinned pulls Twisted 26.4.0 -> 3 collection errors.
            # 23.10.0 collects 3116 tests with none.
            "pip install 'Twisted==23.10.0'",
            # defusedxml: this era's fix patch adds `import defusedxml.xmlrpc` to
            # scrapy/http/request/rpc.py, a dependency the base commit does not
            # declare. Without it the fix stage cannot even import scrapy -
            # conftest dies with ModuleNotFoundError and the stage reports zero
            # tests, so the record can never be classified.
            "pip install defusedxml",
        ],
        "test": "pytest -v --no-header -rA --tb=no -p no:cacheprovider",
    },
    "scrapy_7395_to_6912": {
        "base_os": "python:3.11-slim",
        "apt": f"{_APT_LIBS}",
        "pip": [
            "pip install attrs coverage>=7.10.6 httpx pexpect>=4.8.0 pyftpdlib>=2.0.1"
            " pygments pytest>=8.4.1 pytest-cov>=7.0.0 pytest-xdist sybil>=1.3.0"
            " testfixtures pytest-twisted>=1.14.3",
            "pip install -e .",
        ],
        "test": f"{_COV} scrapy tests --doctest-modules",
    },
}


# Tests that wedge the pytest session rather than merely failing. scrapy's
# CrawlerProcess/engine tests start a Twisted reactor and real sockets; with no
# usable network the test itself times out (pytest-timeout fires) but the reactor
# is left polling, so the session stalls forever - both pr-3667 and pr-3873 froze
# at exactly 18% at 0% CPU and only produced reports because the container was
# killed, leaving a fix stage covering ~385 of ~2000 tests. A truncated stage
# cannot be compared against a full one, so these node ids are deselected for the
# affected eras. The deselect applies to run, test and fix stages alike, so the
# three stages stay directly comparable.
_ERA_DESELECT = {
    "scrapy_3722_to_3377": [
        "tests/test_engine_stop_download_headers.py::EngineTest",
        "tests/test_engine_stop_download_output.py::EngineTest",
        "tests/test_engine.py::EngineTest",
    ],
    "scrapy_4036_to_3824": [
        "tests/test_engine.py::StopDownloadEngineTest",
        "tests/test_engine.py::EngineTest",
    ],
}


def _era(pr_number: int) -> str:
    era = _ERA_OF_PR.get(pr_number)
    if era is None:
        raise ValueError(f"PR #{pr_number} not mapped to any scrapy era")
    return era


def _test_cmd(era: str) -> str:
    """Era test command, plus --continue-on-collection-errors.

    scrapy's suite has modules that import optional or version-sensitive third
    party code (tests/ftpserver.py needs a pyOpenSSL newer than some eras pin).
    Without the flag a single un-importable module aborts the whole session
    ("Interrupted: N errors during collection"), the stage reports zero tests,
    and every stage then looks identical - which reads as a genuine "no
    transition" result when it is really one bad import.
    """
    # --timeout bounds any single blocked test (see the pytest-timeout note in
    # _prepare_sh). The default signal method fails just that test and lets the
    # session continue, so one network-bound test cannot stall the stage.
    deselect = "".join(f" --deselect {n}" for n in _ERA_DESELECT.get(era, []))
    return (
        f"{_ERA_SPEC[era]['test']} --continue-on-collection-errors"
        f" --timeout=120{deselect}"
    )


# ---- shared shell helpers ------------------------------------------------- #

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


def _prepare_sh(repo: str, base_sha: str, pip_steps: list[str]) -> str:
    steps = "\n".join(pip_steps)
    return f"""#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

{steps}

# pytest-timeout is mandatory for every era: scrapy's CrawlerProcessSubprocess
# tests spawn subprocesses that do DNS resolution and network I/O. With no usable
# network they do not fail, they block forever - a stage stalls at ~8% and never
# returns, so the whole run hangs instead of finishing.
pip install --no-cache-dir pytest-timeout || true
"""


# A patch that fails to apply must abort the stage loudly. Swallowing the error
# leaves the stage identical to the previous one, which reads as a genuine
# "no transition" result when it is really a tooling failure.
# --exclude '*.bin': the dataset's patches were produced without `git diff
# --binary`, so binary files appear as contentless "Binary files ... differ"
# stubs that git apply rejects ("cannot apply binary patch ... without full
# index line") and the whole patch then fails. pr-3873 carries three such
# .bin fixtures; skipping them lets the rest of the patch apply.
_APPLY_TEST = """git apply --whitespace=nowarn --exclude '*.bin' /home/test.patch || \\
    { echo "PATCH_APPLY_FAILED: test.patch"; exit 1; }"""

_APPLY_FIX = """git apply --whitespace=nowarn --exclude '*.bin' /home/fix.patch || \\
    { echo "PATCH_APPLY_FAILED: fix.patch"; exit 1; }"""


# Era 3722_to_3377 only: the base conftest needs pytest-twisted to supply
# --reactor, but the patched conftest registers that option itself and argparse
# then aborts on the duplicate. Drop the plugin after patching so each stage gets
# the environment its own code expects.
_UNINSTALL_PYTEST_TWISTED = (
    "pip uninstall -y -q pytest-twisted >/dev/null 2>&1 || true"
)

# Self-healing: some records' patched conftest reads --reactor via getoption but
# rely on pytest-twisted to register it (e.g. pr-6769), while their era-mates
# self-register it and must NOT have the plugin (pr-6911). Probe after patching:
# if pytest aborts with "no option named '--reactor'", install pytest-twisted.
# This adapts per record without disturbing the ones that already work.
# NB: capture to a variable first. Piping `pytest ... | grep` directly into an
# `if` breaks under `set -eo pipefail` - pytest exits non-zero (it aborts in
# pytest_configure), and pipefail propagates that as the pipeline status, so the
# `if` sees false and the install is skipped even when grep matched.
_ENSURE_REACTOR_OPT = (
    "_rp=$(pytest --collect-only -q -p no:cacheprovider tests 2>&1 || true); "
    "if printf '%s' \"$_rp\" | grep -qE \"no option named '--reactor'|"
    "unrecognized arguments: --reactor\"; then "
    "pip install -q pytest-twisted >/dev/null 2>&1 || true; fi"
)


def _needs_plugin_drop(era: str) -> bool:
    return era == "scrapy_3722_to_3377"


def _needs_reactor_probe(era: str) -> bool:
    # pr-6769 in this era needs pytest-twisted at the patched stages; its
    # era-mate pr-6911 does not, so the probe installs it only when required.
    return era in ("scrapy_6911_to_6748", "scrapy_6240_to_6240")


def _run_sh(repo: str, test_cmd: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
{test_cmd}
"""


def _test_run_sh(repo: str, test_cmd: str, drop_plugin: bool = False,
                 ensure_reactor: bool = False) -> str:
    drop = f"{_UNINSTALL_PYTEST_TWISTED}\n" if drop_plugin else ""
    ens = f"{_ENSURE_REACTOR_OPT}\n" if ensure_reactor else ""
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
{_APPLY_TEST}
{drop}{ens}{test_cmd}
"""


def _fix_run_sh(repo: str, test_cmd: str, drop_plugin: bool = False,
                ensure_reactor: bool = False) -> str:
    drop = f"{_UNINSTALL_PYTEST_TWISTED}\n" if drop_plugin else ""
    ens = f"{_ENSURE_REACTOR_OPT}\n" if ensure_reactor else ""
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
{_APPLY_TEST}
{_APPLY_FIX}
{drop}{ens}{test_cmd}
"""


# --------------------------------------------------------------------------- #
#  Shared per-era base image (reference format)
#
#  The leading `# syntax` directive opts out of DockerfileEnhancer, which would
#  otherwise rewrite this file and inject proxy/CA scaffolding. Hardening is
#  therefore written by hand: light here, strict in the PR layer.
# --------------------------------------------------------------------------- #
class ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config, era: str):
        self._pr = pr
        self._config = config
        self._era = era

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    @property
    def era(self) -> str:
        return self._era

    def image_prefix(self) -> str:
        return "mswebench"

    def dependency(self) -> Union[str, "Image"]:
        return _ERA_SPEC[self.era]["base_os"]

    def image_tag(self) -> str:
        return f"base-{self.era}"

    def workdir(self) -> str:
        return f"base-{self.era}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        spec = _ERA_SPEC[self.era]
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

{self.global_env}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates {spec['apt']} \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


# --------------------------------------------------------------------------- #
#  Per-PR image: FROM the era base, check out base.sha, install, then harden.
# --------------------------------------------------------------------------- #
class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config
        self._era = _era(pr.number)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def image_prefix(self) -> str:
        return "mswebench"

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self.config, self._era)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        spec = _ERA_SPEC[self._era]
        test_cmd = _test_cmd(self._era)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                _prepare_sh(self.pr.repo, self.pr.base.sha, spec["pip"]),
            ),
            File(".", "run.sh", _run_sh(self.pr.repo, test_cmd)),
            File(
                ".",
                "test-run.sh",
                _test_run_sh(self.pr.repo, test_cmd, _needs_plugin_drop(self._era),
                             _needs_reactor_probe(self._era)),
            ),
            File(
                ".",
                "fix-run.sh",
                _fix_run_sh(self.pr.repo, test_cmd, _needs_plugin_drop(self._era),
                            _needs_reactor_probe(self._era)),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Canonical hardening from image.py, pinned to this PR's literal base.sha
        # (the PR image has an Image-typed dependency, so the enhancer returns raw).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


# --------------------------------------------------------------------------- #
#  Instance - one registration covering all 21 records
# --------------------------------------------------------------------------- #
@Instance.register("scrapy", "scrapy")
class Scrapy(Instance):
    """Evaluation instance for scrapy/scrapy.

    Routes here when number_interval and tag are empty; the bundle
    number_interval keys registered below route the delivery form. The era is
    chosen from the lead PR number via _ERA_OF_PR.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        """Parse pytest output.

        The node id is anchored to `<path>.py::<name>` rather than matched as
        "anything before a status word": scrapy's suite prints progress, warning
        and summary lines that a loose pattern turns into bogus test names.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log_clean = re.sub(r"\x1b\[[0-9;]*m", "", log)

        pattern = re.compile(
            r"(\S+\.py::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR)\b"
            r"|^(PASSED|FAILED|SKIPPED|ERROR)\s+(\S+\.py::\S+)"
        )

        for line in log_clean.splitlines():
            m = pattern.search(line.strip())
            if not m:
                continue
            if m.group(1):
                name, status = m.group(1), m.group(2)
            else:
                status, name = m.group(3), m.group(4)

            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Data-derived from Dataset/scrapy__scrapy_lht_final.jsonl. The JSONL carries no
# number_interval, so records route by "scrapy/scrapy"; these keys are what the
# delivery form routes on. Regenerate when bundles change.
_BUNDLE_NIS_Scrapy = [
    "2215-2345-2457-2458-2460-2464-2466-2469-2470-2483-2485-2496-2497-2503-2507-2509-2512-2519-2525-2528-2531-2533-2534-2536-2538-2542-2544",  # lead pr-2215
    "2510-2551-2558",  # lead pr-2510
    "2639-2640",  # lead pr-2639
    "2655-2671-2755-2762-2763-2764-2767-2769-2777-2781-2789-2791-2793-2812-2816-2826-2828-2837-2847-2848-2849-2852-2854-2857-2864-2865-2866-2867-2869-2876-2884-2894-2895-2909-2910-2915-2921-2922-2923-2924-2929-2935-2942-2947-2952-2957-2958-2963-2964-2976-2978-2982-2983-2989-2991-3011-3013-3020-3030-3038-3045-3048-3049-3050-3053",  # lead pr-2655
    "2918-4753-5150-5406-5458-5489-5540-5677-5682-5699-5712-5714-5715-5719-5720-5721-5722-5724-5727-5730-5731-5732-5734-5736-5738-5744-5754-5756-5758-5760-5761-5764-5767-5768-5776-5777-5780-5781-5782-5783-5786-5790-5795-5798-5799-5800-5806-5807-5814",  # lead pr-2918
    "3426-4151-6151-6526-6547-6560-6565-6567-6568-6575-6576-6577-6579-6581-6582-6584-6586-6587-6595-6599-6601-6602-6605-6606-6607-6608-6609-6610-6613-6614-6618-6621-6622-6623-6624-6626-6628-6631-6633-6634-6635-6646-6647-6648-6650-6651-6653-6655-6656-6657-6664-6671-6678-6680-6684-6688-6693-6694-6695-6696-6697-6699-6700-6701-6702-6703-6704-6709-6710-6711-6712-6713-6714-6716-6719-6720-6721-6722-6723-6724-6725-6729-6732-6734-6735-6738-6740-6741-6743-6757-6764-6766-6770-6771-6772-6773-6775-6776-6780",  # lead pr-3426
    "3550-3596",  # lead pr-3550
    "3667-4694-4736-4759-4769-4799-4814-4850-4878-4895-4897-4898-4899-4900-4901-4902-4909-4911-4912-4924-4935-4936-4940-4942-4950-4956-4965-4973-4974-4982-4986-4987-5002-5005-5006-5008-5014-5016-5022-5027-5028-5036-5052-5053-5057-5062-5063-5065-5066-5072-5073-5076",  # lead pr-3667
    "3873-4090-4165-4243-4298-4310-4324-4414-4512-4564-4623-4632-4646-4663-4686-4688-4691-4701-4703-4705-4707-4714-4718-4721-4722-4723-4724-4727-4735-4738-4742-4743-4745-4746-4747-4752-4755-4756-4761-4764-4765-4768-4772-4775-4776-4778-4782-4800-4801-4804-4808-4809-4816-4817-4818-4820-4822-4823-4825-4831-4835-4836-4839",  # lead pr-3873
    "4436-4437",  # lead pr-4436
    "4716-5146-5705-5833-5846-5847-5925-5927-5929-5931-5937-5939-5948-5949-5950-5951-5952-5953-5958-5960-5963-5965-5971-5977-5979-5980-5984-5986-5993-5996-5998-5999-6000-6001",  # lead pr-4716
    "4803-4859-4869-4872-4874-4876-4884",  # lead pr-4803
    "5118-5926-6002-6003-6005-6007-6009-6010-6013-6014-6016-6021-6034-6036-6038-6040-6045-6046-6048-6050",  # lead pr-5118
    "5260-6952-6966-6993-7007-7036-7182-7199-7208-7210-7222-7223-7232-7233-7234-7238-7239-7241-7245-7248-7250-7254-7255-7256-7257-7259-7263-7274-7276-7277-7279-7283-7300-7329-7331-7349-7351-7353-7355-7361-7363-7366-7367-7368-7370-7373-7374-7375-7376-7379-7380-7381-7384-7385-7386-7387-7388-7391-7394-7395-7402-7405-7406-7408-7410",  # lead pr-5260
    "5328-5581-5801-5802-5805-5808-5816-5820-5821-5823-5824-5826-5827-5832-5839-5849-5851-5858-5876-5877-5879-5880-5881-5883-5885-5889-5890-5891-5892-5895-5896-5898-5901-5902-5904-5908-5909-5915-5917-5918-5919",  # lead pr-5328
    "5436-5440-5445-5448-5459-5471-5482-5503-5528-5535-5536",  # lead pr-5436
    "5589-5599-5605-5626-5681-5688-5689-5691-5692-5694-5695-5696-5697-5698-5701",  # lead pr-5589
    "6240-6358-6359",  # lead pr-6240
    "6769-6793-6795-6796-6801-6802-6803-6804-6815-6817-6821-6822-6824-6826-6827-6831-6832-6833-6835-6836-6839-6842-6845-6846-6849-6852-6855-6858-6863-6867-6873-6874-6875-6882-6883-6884-6885-6888-6889-6892-6893-6897-6900-6901-6903-6905-6907-6910-6918-6919-6920-6921-6922-6923-6926-6928-6930-6933-6937-6938-6940-6941-6942-6945-6947-6949-6957-6960-6968-6969-6970-6974-6975-6977-6979-6980-6984-6986-6994-6997-6999-7005-7006-7008-7011-7012-7013-7033-7034-7035-7037-7039-7043-7045-7046-7047-7050-7058-7059-7069-7070-7073-7076-7079-7094-7095-7109-7116-7117-7118-7121-7126-7127-7134-7137-7142-7145-7146-7151-7159-7160-7161-7164-7172-7173-7176-7177-7178-7179-7198-7202",  # lead pr-6769
    "6911-6934",  # lead pr-6911
    "7212-7213-7215-7217",  # lead pr-7212
]

for _ni in _BUNDLE_NIS_Scrapy:
    Instance.register("scrapy", _ni)(Scrapy)
