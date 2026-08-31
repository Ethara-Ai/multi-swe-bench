import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Textualize/rich — a Python library for rich text & formatting in the terminal.
#
# TWO-IMAGE, shared-base design (efficient + self-contained, MITM-enabled):
#
#   ImageBase  ->  mswebench/textualize_m_rich:base   (ONE shared image)
#       Clones the repo ONCE (full history) and installs the COMMON deps
#       (rich's runtime deps via an editable install at the default branch +
#       the pytest toolchain). No BASE_COMMIT is used and NO hardening runs
#       here — the base must keep full history so every PR can check out its
#       own commit. Built once and reused by all PRs.
#
#   ImageDefault  ->  mswebench/textualize_m_rich:pr-<N>   (per PR)
#       FROM the shared base, so the common deps are already present. Checks
#       out the PR's BASE_COMMIT (baked as an ARG default), runs install.sh to
#       pick up only the deps that differ for that commit (common ones are
#       reused from the base, not reinstalled), bakes the eval scripts +
#       patches, and THEN runs the _HARDENING_BLOCK that detaches HEAD and
#       strips every other ref/remote/commit. That scrub is what closes the
#       reward-hacking hole (git log / show / diff origin) and it lives in the
#       PR image because it can only run after the per-PR checkout.
#
# Both images are SELF-CONTAINED: each dockerfile() emits the complete Dockerfile
# starting with "# syntax=docker/dockerfile:1.6", which makes
# DockerfileEnhancer.enhance() return it unchanged (early-return on the
# directive). So the enhancer's proxy / CA-cert / MITM injection never runs for
# rich -- which is why the canonical MITM scaffolding is inlined BY HAND below
# (2026-08-19 re-add). Both dockerfiles carry image.py's _PROXY_ARGS, _ENV_BLOCK
# and _CERT_SYMLINKS constants VERBATIM: build ARGs default to empty (passthrough)
# so traffic only routes through a proxy when the build passes
# `--build-arg http_proxy=<proxy>`, and the CA bundle is symlinked to every path
# the toolchain looks for it. _MITM_MOUNT stays latent, exactly as in image.py.
#
# Single pure-Python package (rich/) with a root poetry pyproject and a root
# tests/ tree. pytest is the runner throughout -> one parse_log. System pip
# (no venv / no uv); native arm64 build keeps the git tree clean for hardening.


# git apply excludes: rich's test/fix patches carry binary blobs (SVG/PNG/etc.
# rendered-output fixtures) whose diff hunks abort an otherwise-good apply.
_EXCLUDES = (
    "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
    "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip"
)


def _target_test_files(test_patch: str) -> list[str]:
    """Test files the test_patch touches — the only ones worth running.

    Scoping to these (instead of the whole tests/ tree) is the standard SWE-bench
    F2P/P2P convention; it also sidesteps unrelated tests in recent rich versions
    that hang forever on a tty/stdin read. We read the destination ("+++ b/...")
    side so newly-added test files are captured too (get_modified_files drops
    them, since their source is /dev/null). Deleted files map to +++ /dev/null and
    are naturally excluded.
    """
    files: list[str] = []
    for dst in re.findall(r"^\+\+\+ b/(.+?)\s*$", test_patch, re.M):
        if dst.endswith(".py") and (dst.startswith("tests/") or "/test" in dst or dst.startswith("test")):
            if dst not in files:
                files.append(dst)
    return files


class ImageBase(Image):
    """Shared base image: clone once + install common deps. No checkout, no hardening."""

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
        # Single shared tag — identical for every PR, so it builds once and is
        # reused (Image equality / dedup is on image_full_name).
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_install = """#!/bin/bash
# COMMON dependency install, shared by every PR. Editable install at the default
# branch pulls rich's runtime deps (pygments, markdown-it-py, ...) plus the
# pytest toolchain. Per-PR images reuse all of this; their install.sh only adds
# whatever differs at their specific commit.
set -uo pipefail
cd /home/__REPO__
pip install --no-cache-dir -e ".[jupyter]" >/dev/null 2>&1 \\
  || pip install --no-cache-dir -e . >/dev/null 2>&1 || true
pip install --no-cache-dir pytest pytest-asyncio pytest-mock pytest-timeout >/dev/null 2>&1 || true
""".replace("__REPO__", repo)
        return [File(".", "base_install.sh", base_install)]

    def dockerfile(self) -> str:
        repo = self.pr.repo
        org = self.pr.org
        repo_url = f"https://github.com/{org}/{repo}.git"
        # Self-contained (syntax directive -> enhancer skips), so the canonical
        # MITM block is inlined here by hand. Clones FULL history (no checkout)
        # so PRs can check out any commit.
        template = """# syntax=docker/dockerfile:1.6

FROM python:3.12-bookworm

ARG TARGETARCH
ARG REPO_URL="__REPO_URL__"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ base image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/
ENV PIP_ROOT_USER_ACTION=ignore
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${REPO_URL}" /home/__REPO__

COPY base_install.sh /home/base_install.sh
RUN bash /home/base_install.sh || true

CMD ["/bin/bash"]
"""
        return (
            template.replace("__REPO_URL__", repo_url)
            .replace("__ORG__", org)
            .replace("__REPO__", repo)
        )


class ImageDefault(Image):
    """Per-PR image: FROM shared base, checkout the PR commit, install PR-specific
    deps (reusing the base's common deps), bake patches/scripts, then harden."""

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
        # Depend on the shared base Image (not a string) -> this PR image is
        # built FROM mswebench/textualize_m_rich:base, reusing its deps.
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = f"{base.image_name()}:{base.image_tag()}"
        org, repo = self.pr.org, self.pr.repo
        sha = self.pr.base.sha
        # Self-contained Dockerfile. BASE_COMMIT is baked as the ARG default
        # (build_dataset only passes it for string-dependency images, i.e. the
        # base — so the PR image must carry its own commit). The hardening block
        # uses ${BASE_COMMIT} and runs AFTER checkout, scrubbing all history.
        template = """# syntax=docker/dockerfile:1.6

FROM __BASE__

ARG TARGETARCH
ARG BASE_COMMIT=__SHA__

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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

ENV PIP_ROOT_USER_ACTION=ignore
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY install.sh /home/install.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/install.sh || true

__HARDENING__

CMD ["/bin/bash"]
"""
        return (
            template.replace("__BASE__", base_name)
            .replace("__SHA__", sha)
            .replace("__ORG__", org)
            .replace("__REPO__", repo)
            .replace("__HARDENING__", Image._HARDENING_BLOCK.rstrip("\n"))
        )

    def files(self) -> list[File]:
        repo = self.pr.repo

        install = """#!/bin/bash
# Per-PR install: editable install at THIS commit. Common deps are already in
# the shared base, so pip reuses them and only installs what differs here.
# Always (re)ensure the pytest runner + plugins.
set -uo pipefail
cd /home/__REPO__
pip install --no-cache-dir -e ".[jupyter]" >/dev/null 2>&1 \\
  || pip install --no-cache-dir -e . >/dev/null 2>&1 || true
# pytest-timeout is mandatory: some rich versions ship tests that block forever
# on a tty/stdin read or a live-display loop, which would otherwise hang the
# whole suite. A per-test timeout kills only the offending test.
pip install --no-cache-dir pytest pytest-asyncio pytest-mock pytest-timeout >/dev/null 2>&1 || true
""".replace("__REPO__", repo)

        target_tests = " ".join(_target_test_files(self.pr.test_patch))

        # Re-run install (a patch may add a dependency), select the patch's own
        # test files that actually exist on disk (the unpatched baseline run
        # won't have newly-added ones yet), then pytest only those. -v gives one
        # `path::test STATUS` line per test for parse_log; -o addopts="" discards
        # the coverage/plugin flags rich pins in pyproject. --timeout=30 (signal)
        # caps any single test that hangs (e.g. an infinite loop the PR fixes ->
        # the new test must register as a bounded FAILED, the F2P signal).
        run_pytest = """bash /home/install.sh || true
TARGET="__TARGET__"
SEL=""
for f in $TARGET; do [ -e "$f" ] && SEL="$SEL $f"; done
if [ -z "${SEL// }" ]; then
    echo ">>>>> No target test files present; nothing to run."
else
    timeout -k 30 1200 python -m pytest $SEL -v -rA --continue-on-collection-errors \\
        -o addopts="" -p no:cacheprovider --timeout=30 --timeout-method=signal || true
fi""".replace("__TARGET__", target_tests)

        run_sh = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO__
__RUN_PYTEST__
""".replace("__REPO__", repo).replace("__RUN_PYTEST__", run_pytest)

        test_run = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
__RUN_PYTEST__
""".replace("__REPO__", repo).replace("__EXCLUDES__", _EXCLUDES).replace(
            "__RUN_PYTEST__", run_pytest
        )

        fix_run = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
__RUN_PYTEST__
""".replace("__REPO__", repo).replace("__EXCLUDES__", _EXCLUDES).replace(
            "__RUN_PYTEST__", run_pytest
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", install),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


@Instance.register("Textualize", "rich")
class Rich(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        # Strip real ANSI escape sequences (note: a nodeid may contain the
        # *literal* text "\x1b" — pytest escapes control chars in param ids —
        # which this leaves intact).
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Parse ONLY pytest's `-v` progress lines, anchored on the unambiguous
        # "[ NN%]" suffix. Non-greedy nodeid keeps params containing spaces or
        # " - " whole; the `-rA` summary lines are deliberately ignored.
        line_re = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\(.*?\))?\s+\[\s*\d+%\]\s*$"
        )

        for line in clean.splitlines():
            m = line_re.match(line.rstrip())
            if not m:
                continue
            name = m.group(1).strip()
            status = m.group(2)
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # Disjoint sets: failed > skipped > passed.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- bundle routing keys (PIPELINE 11b) --------------------------------------
# The trajectory harness routes through Instance.create() -> "{org}/{number_interval}",
# and every record in Textualize__rich_usable32.jsonl carries a dash-joined bundle
# value, so each one must exist as its own registry key or create() raises
# ValueError. rich is single-era: all 32 bundles map to the one Rich class above.
# Bundle-level, NOT pr-level -> #keys == #instances. Data-derived: REGENERATE if the
# bundles change. (32 keys covering 378 PRs.)
_BUNDLE_NIS_RICH = [
    "40-41",
    "101-102-105",
    "112-115-116",
    "155-156",
    "160-161-167",
    "169-172",
    "193-198-199",
    "201-208-211-217-221",
    "246-250-251-254",
    "326-334-338-339-341-342-350-353-361-362-365-367-368-369-372-373-376-377-380-381-386-387-389-390-391",
    "397-398-402",
    "435-437-443-444-447-448-452-456",
    "837-843-853-856-857-858-860-861-862-863-865",
    "962-972-974-981-994-998-1008-1012-1013",
    "1020-1028-1031-1032",
    "1248-1252-1253-1269-1274-1275-1276",
    "1281-1282-1284-1291-1292-1293-1300",
    "1299-1310-1315",
    "1327-1330-1333-1335-1336-1344-1345-1346",
    "1490-1850-1851-1858-1878-1892-1904-1915-1916-1919-1920-1929-1941-1942-1945-1950-1952-1956-1957-1963-1986-1988-1992-1993-1995-1996-2000-2002-2004-2008-2019-2029-2031-2037-2038-2043-2044-2045",
    "1538-1540-1543-1545-1546-1547-1557-1573-1574-1579-1580-1581-1583-1584-1586-1593-1595-1596-1620-1628-1629-1631-1634-1636-1637-1643-1644-1647-1648-1649-1654-1655-1656",
    "1730-1748-1787-1795-1796-1800-1804-1805-1806-1807-1808-1811-1812-1813-1815-1816-1819",
    "2131-2160-2166-2168-2170-2177-2188-2200-2201-2209-2210-2212-2216-2217-2219-2224-2225-2226-2228",
    "2221-2254-2264-2268-2292-2294-2296-2301-2305-2322-2325-2327-2328-2330-2331-2332-2339-2341-2342-2343-2346-2349-2352-2355-2356-2357-2359-2361-2365-2366-2367-2377-2382-2385",
    "2437-2606-2613-2631-2635-2659-2786-2787-2799-2804-2805-2806-2808-2820-2828-2839-2844-2845-2850-2851-2852-2853",
    "2725-2788-2858-2864-2867-2870-2943-2984-3007-3052-3077-3141-3165-3166-3209-3220-3226-3229-3232-3255-3268-3276-3278-3296-3324-3333-3350-3351-3378-3399-3401-3402-3403-3404-3405-3414-3420-3421-3452-3454-3455-3467-3468-3469-3470-3471-3472-3473",
    "3004-3006-3019-3043-3060-3061-3063-3064-3065-3066",
    "3035-3094-3105-3113-3122-3145-3151-3162-3170-3178-3180-3181-3191-3192-3195-3202",
    "3514-3518-3519-3521",
    "3828-3879-3882-3894-3905-3906-3915-3923-3930-3934-3935-3937-3938-3939-3942",
    "3941-4075-4076-4077-4079-4080",
    "3972-4006-4007-4008",
]

for _ni in _BUNDLE_NIS_RICH:
    Instance.register("Textualize", _ni)(Rich)
