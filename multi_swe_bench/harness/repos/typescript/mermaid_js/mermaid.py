import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# MITM proxy / cert scaffolding (PIPELINE §2a, §8) — written out in full here
# ---------------------------------------------------------------------------
# These images opt out of `DockerfileEnhancer` auto-injection: the base carries a
# `# syntax` directive (§2 opt-out gotcha) and the PR layer has an `Image`-typed
# dependency, so `enhance()` returns both Dockerfiles raw and injects nothing.
# Per §2a the MITM block is therefore added BY HAND, spelled out literally below.
#
# The text is identical to the canonical `image.py` constants (`_PROXY_ARGS`,
# `_ENV_BLOCK`, `_CERT_SYMLINKS`) — keep it that way. If `image.py` ever changes,
# these literals must be updated to match, or the §8.1 audit will flag the drift.
#
# `_MITM_MOUNT` is deliberately NOT wired in — it is latent in image.py too (§2a).
# Proxy ARGs default to empty = passthrough; pass `--build-arg http_proxy=...`
# to actually route the build through a MITM proxy.

_MITM_PROXY_ARGS = '''\
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'''

_MITM_ENV_BLOCK = '''\
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
    CURL_CA_BUNDLE=${CA_CERT_PATH}'''

_MITM_CERT_SYMLINKS = '''\
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt'''


# ---------------------------------------------------------------------------
# Shared base image — parameterized by Node version + package manager style
# ---------------------------------------------------------------------------


class MermaidVersionBase(Image):
    """Base image for mermaid-js/mermaid — clones repo (bare + PR refs), installs deps.

    Args:
        node_image: Docker image name (e.g. "node:16-bookworm", "node:22-bookworm")
        interval_name: Used for image_tag/workdir dedup (e.g. "mermaid_997_to_3408")
        pkg_manager: "yarn", "pnpm", or "pnpm_with_yarn_fallback" (installs both for transition eras)
        pnpm_version: pnpm version to install (only used when pkg_manager contains "pnpm")
    """

    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        node_image: str,
        interval_name: str,
        pkg_manager: str = "pnpm",
        pnpm_version: str = "9",
    ):
        self._pr = pr
        self._config = config
        self._node_image = node_image
        self._interval_name = interval_name
        self._pkg_manager = pkg_manager
        self._pnpm_version = pnpm_version

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return self._node_image

    def image_tag(self) -> str:
        return "base-{name}".format(name=self._interval_name)

    def workdir(self) -> str:
        return "base-{name}".format(name=self._interval_name)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Shared per-era base image — PIPELINE §3 reference format.

        `# syntax=docker/dockerfile:1.6` opts this base out of
        `DockerfileEnhancer` auto-injection (PIPELINE §2 "opt-out gotcha"), so a
        single base is built and reused by every PR of the era instead of being
        rebuilt per PR under one shared tag. Full git history is kept here; the
        strict `_HARDENING_BLOCK` runs in the PR layer (PIPELINE §4).
        """
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        global_env = self.global_env.strip()
        clear_env = self.clear_env.strip()
        global_section = f"\n{global_env}\n" if global_env else ""
        clear_section = f"\n{clear_env}\n" if clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

{_MITM_PROXY_ARGS}

{_MITM_ENV_BLOCK}

ENV LC_ALL=C.UTF-8

{_MITM_CERT_SYMLINKS}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"
{global_section}
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    jq git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/
{clear_section}
CMD ["/bin/bash"]
"""


# ---------------------------------------------------------------------------
# Shared parse_log: Jest output (Era 2)
# ---------------------------------------------------------------------------


def mermaid_jest_parse_log(test_log: str) -> TestResult:
    """Parse Jest verbose test output for mermaid (Era 2: yarn+jest).

    Handles:
    - Suite-level: PASS src/file.spec.js / FAIL src/file.spec.js
    - Individual: checkmark/cross test names
    - Summary: Test Suites: N passed, N total / Tests: N passed, N total
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    re_pass_suite = re.compile(
        r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
    )
    re_fail_suite = re.compile(
        r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
    )
    re_pass_test = re.compile(
        r"^\s*[✔✓√]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
    )
    re_fail_test = re.compile(
        r"^\s*[×✕✗✘✖]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
    )
    re_skip_test = re.compile(
        r"^\s*[○◌]\s+(?:skipped\s+)?(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
    )
    re_fail_indicator = re.compile(r"^\s*●\s+(.+?)\s+›\s+(.+)$")

    for line in test_log.splitlines():
        line = ansi_escape.sub("", line).strip()
        if not line:
            continue

        m = re_pass_suite.match(line)
        if m:
            passed_tests.add(m.group(1).strip())
            continue

        m = re_fail_suite.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_pass_test.match(line)
        if m:
            name = m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
            continue

        m = re_fail_test.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_skip_test.match(line)
        if m:
            skipped_tests.add(m.group(1).strip())
            continue

        m = re_fail_indicator.match(line)
        if m:
            name = "{suite} > {test}".format(
                suite=m.group(1).strip(), test=m.group(2).strip()
            )
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# ---------------------------------------------------------------------------
# Shared parse_log: Vitest output (Eras 3 & 4)
# ---------------------------------------------------------------------------


def mermaid_vitest_parse_log(test_log: str) -> TestResult:
    """Parse Vitest verbose test output for mermaid (Eras 3 & 4: pnpm+vitest).

    Handles:
    - Individual: checkmark/cross test names with optional timing
    - File-level FAIL lines
    - Summary: Test Files  N passed | N failed | N skipped (N)
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    re_vitest_pass = re.compile(
        r"^\s*[✓✔]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"
    )
    re_vitest_fail = re.compile(
        r"^\s*[×✗]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"
    )
    re_vitest_skip = re.compile(
        r"^\s*[-↓]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"
    )
    re_vitest_fail_file = re.compile(
        r"^\s*FAIL\s+(\S+\.(?:test|spec)(?:-d)?\.(?:ts|tsx|js|jsx|mts|mjs))"
    )
    re_vitest_summary_passed = re.compile(
        r"^\s*Test Files\s+(\d+)\s+passed"
    )
    re_vitest_summary_failed = re.compile(
        r"^\s*Test Files\s+(\d+)\s+failed"
    )

    for line in test_log.splitlines():
        line = ansi_escape.sub("", line).strip()
        if not line:
            continue

        m = re_vitest_pass.match(line)
        if m:
            name = m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
            continue

        m = re_vitest_fail.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_vitest_skip.match(line)
        if m:
            skipped_tests.add(m.group(1).strip())
            continue

        m = re_vitest_fail_file.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_vitest_summary_passed.match(line)
        if m and not passed_tests:
            passed_tests.add("__vitest_suite_passed__")
            continue

        m = re_vitest_summary_failed.match(line)
        if m and not failed_tests:
            failed_tests.add("__vitest_suite_failed__")
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# ---------------------------------------------------------------------------
# Shared shell script templates
# ---------------------------------------------------------------------------

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

# Era 2: yarn + jest
_YARN_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install yarn if not available
if ! command -v yarn &> /dev/null; then
    echo "prepare: Installing yarn@1"
    npm install -g yarn@1 2>&1 || true
fi

# Install dependencies (yarn classic)
yarn install --frozen-lockfile 2>&1 || yarn install 2>&1 || true
"""

_YARN_RUN_SH = """#!/bin/bash
set -e

cd /home/{repo}
yarn jest --verbose "src/.*" 2>&1 || true
"""

_YARN_TEST_RUN_SH = """#!/bin/bash
set -e

cd /home/{repo}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch || git apply --exclude yarn.lock --whitespace=nowarn --reject /home/test.patch || true
yarn jest --verbose "src/.*" 2>&1 || true
"""

_YARN_FIX_RUN_SH = """#!/bin/bash
set -e

cd /home/{repo}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --exclude yarn.lock --whitespace=nowarn --reject /home/test.patch || true; git apply --exclude yarn.lock --whitespace=nowarn --reject /home/fix.patch || true; }}
yarn jest --verbose "src/.*" 2>&1 || true
"""

# Eras 3 & 4: pnpm + vitest (smart detect — handles yarn→pnpm transition PR #3531)
_PNPM_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Detect package manager from the checked-out commit
if [ -f pnpm-lock.yaml ] || grep -q '"pnpm@' package.json 2>/dev/null; then
    echo "prepare: Detected pnpm project"
    # Install the exact pnpm version from packageManager field
    PNPM_VER=$(node -e "try {{ const p = require('./package.json'); const m = (p.packageManager || '').match(/pnpm@([\\d.]+)/); if(m) console.log(m[1]); }} catch(e) {{}}" 2>/dev/null)
    if [ -n "$PNPM_VER" ]; then
        echo "prepare: Installing pnpm@$PNPM_VER from packageManager field"
        npm install -g "pnpm@$PNPM_VER" 2>&1 || true
    else
        echo "prepare: No packageManager version found, installing pnpm@9"
        npm install -g pnpm@9 2>&1 || true
    fi
    pnpm install --no-frozen-lockfile 2>&1 || pnpm install 2>&1 || true
elif [ -f yarn.lock ]; then
    echo "prepare: Detected yarn project (transition commit)"
    npm install -g yarn@1 2>&1 || true
    yarn install --frozen-lockfile 2>&1 || yarn install 2>&1 || true
else
    echo "prepare: No lockfile detected, trying npm install"
    npm install 2>&1 || true
fi
"""

_PNPM_RUN_SH = """#!/bin/bash
set -e

cd /home/{repo}
npx vitest run --reporter=verbose 2>&1 || true
"""

_PNPM_TEST_RUN_SH = """#!/bin/bash
set -e

cd /home/{repo}

# Detect lockfile to choose correct --exclude for git apply
if [ -f pnpm-lock.yaml ]; then
    LOCK_EXCLUDE="pnpm-lock.yaml"
elif [ -f yarn.lock ]; then
    LOCK_EXCLUDE="yarn.lock"
else
    LOCK_EXCLUDE="package-lock.json"
fi

git apply --exclude "$LOCK_EXCLUDE" --whitespace=nowarn /home/test.patch || git apply --exclude "$LOCK_EXCLUDE" --whitespace=nowarn --reject /home/test.patch || true
npx vitest run --reporter=verbose 2>&1 || true
"""

_PNPM_FIX_RUN_SH = """#!/bin/bash
set -e

cd /home/{repo}

# Detect lockfile to choose correct --exclude for git apply
if [ -f pnpm-lock.yaml ]; then
    LOCK_EXCLUDE="pnpm-lock.yaml"
elif [ -f yarn.lock ]; then
    LOCK_EXCLUDE="yarn.lock"
else
    LOCK_EXCLUDE="package-lock.json"
fi

git apply --exclude "$LOCK_EXCLUDE" --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --exclude "$LOCK_EXCLUDE" --whitespace=nowarn --reject /home/test.patch || true; git apply --exclude "$LOCK_EXCLUDE" --whitespace=nowarn --reject /home/fix.patch || true; }}
npx vitest run --reporter=verbose 2>&1 || true
"""
