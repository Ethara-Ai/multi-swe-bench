from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_MITM_PROXY_ARGS = (
    'ARG http_proxy=""\n'
    'ARG https_proxy=""\n'
    'ARG HTTP_PROXY=""\n'
    'ARG HTTPS_PROXY=""\n'
    'ARG no_proxy="localhost,127.0.0.1,::1"\n'
    'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
    'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
)

_BASE_ENV_BLOCK = (
    "ENV DEBIAN_FRONTEND=noninteractive \\\n"
    "    LANG=C.UTF-8 \\\n"
    "    TZ=UTC \\\n"
    "    http_proxy=${http_proxy} \\\n"
    "    https_proxy=${https_proxy} \\\n"
    "    HTTP_PROXY=${HTTP_PROXY} \\\n"
    "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
    "    no_proxy=${no_proxy} \\\n"
    "    NO_PROXY=${NO_PROXY} \\\n"
    "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
    "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    CURL_CA_BUNDLE=${CA_CERT_PATH}"
)

_MITM_CERT_SYMLINKS = (
    "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
)

_APT_INSTALL = (
    "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
    "    ca-certificates \\\n"
    "    curl \\\n"
    "    build-essential \\\n"
    "    git \\\n"
    "    gnupg \\\n"
    "    make \\\n"
    "    python3 \\\n"
    "    sudo \\\n"
    "    wget \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)

_HARDENING_BLOCK = """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\""""

_SUBMODULE_SCRUB_BLOCK = """RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""


_ORG = "serverless"
_REPO = "serverless-google-cloudfunctions"
_NODE_IMAGE = "node:18-bookworm"
_NPM_BEFORE = "2021-04-03T11:41:58Z"
_JEST = "node_modules/.bin/jest --no-watchman --verbose --no-color --ci --runInBand"

_SCRIPT_ENV = (
    "export CI=true\n"
    "export NODE_ENV=test\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n"
    "export NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt\n"
    "export HUSKY_SKIP_INSTALL=1\n"
    "export SLS_TELEMETRY_DISABLED=1\n"
    "export SLS_TRACKING_DISABLED=1\n"
    "export NO_UPDATE_NOTIFIER=1\n"
    "export NPM_CONFIG_UPDATE_NOTIFIER=false\n"
    "export NPM_CONFIG_FUND=false\n"
    "export NPM_CONFIG_AUDIT=false"
)

_GATE_JS = (
    'require("./package.json"); require("googleapis/package.json"); require("sinon");'
    ' require("jest/package.json"); console.log("DEPS_OK");'
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_SUITE_HEADER_RE = re.compile(r"^(?:PASS|FAIL)\s+(\S+\.[cm]?[jt]sx?)(?:\s+\(.*\))?\s*$")
_RESULT_LINE_RE = re.compile(r"^([✓✕○✎])\s+(.*)$")
_DURATION_RE = re.compile(r"\s+\(\d+(?:\.\d+)?\s*m?s\)$")


class ServerlessGoogleCloudfunctionsImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {_NODE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{_MITM_PROXY_ARGS}

{_BASE_ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_MITM_CERT_SYMLINKS}

WORKDIR /home/

{_APT_INSTALL}

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ServerlessGoogleCloudfunctionsImageDefault(Image):
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
        return ServerlessGoogleCloudfunctionsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "ERROR: /home/{repo} is not a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is not clean:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "GIT_TREE_CLEAN"
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail
{_SCRIPT_ENV}

git config --global --add safe.directory '*'

cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

npm install --no-audit --no-fund --no-save --no-package-lock --before={_NPM_BEFORE}

node -e '{_GATE_JS}'
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_SCRIPT_ENV}

cd /home/{repo}
git reset --hard
git clean -fd

{_JEST} 2>&1
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_SCRIPT_ENV}

cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch

{_JEST} 2>&1
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_SCRIPT_ENV}

cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{_JEST} 2>&1
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"

COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/

RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{_HARDENING_BLOCK}

{_SUBMODULE_SCRUB_BLOCK}
"""


@Instance.register(_ORG, _REPO)
class ServerlessGoogleCloudfunctions(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ServerlessGoogleCloudfunctionsImageDefault(self.pr, self._config)

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
        final_status: dict[str, str] = {}
        current_file: Optional[str] = None
        in_tree = False
        groups: list[tuple[int, str]] = []

        for raw in _ANSI_RE.sub("", log).splitlines():
            line = raw.rstrip()
            header = _SUITE_HEADER_RE.match(line)
            if header:
                current_file = header.group(1)
                in_tree = True
                groups = []
                continue
            if not in_tree:
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("●"):
                in_tree = False
                continue
            indent = len(line) - len(line.lstrip(" "))
            while groups and groups[-1][0] >= indent:
                groups.pop()
            result = _RESULT_LINE_RE.match(stripped)
            if not result:
                groups.append((indent, stripped))
                continue
            symbol, title = result.groups()
            if symbol in ("○", "✎"):
                title = re.sub(r"^(?:skipped|todo)\s+", "", title)
            title = _DURATION_RE.sub("", title).strip()
            name = " > ".join([current_file] + [g for _, g in groups] + [title])
            final_status[name] = {"✓": "PASS", "✕": "FAIL"}.get(symbol, "SKIP")

        passed_tests: set[str] = {n for n, s in final_status.items() if s == "PASS"}
        failed_tests: set[str] = {n for n, s in final_status.items() if s == "FAIL"}
        skipped_tests: set[str] = {n for n, s in final_status.items() if s == "SKIP"}

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
