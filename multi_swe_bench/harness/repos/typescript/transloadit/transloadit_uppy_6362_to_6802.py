import html
import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NUMBER_INTERVAL = "transloadit_uppy_6362_to_6802"
_BASE_TAG = f"base-{_NUMBER_INTERVAL}"
_NODE_IMAGE = "node:24.22.1-bookworm"

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_ENV = """export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_NODE_LINKER=node-modules
export DO_NOT_TRACK=1
export TURBO_TELEMETRY_DISABLED=1
"""

_PREPARE = """#!/bin/bash
set -e

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __SHA__^{commit} 2>/dev/null || git fetch --no-tags origin __SHA__
git checkout --detach __SHA__
bash /home/check_git_changes.sh

__ENV__
ulimit -c 0
corepack yarn install --immutable || true
corepack yarn workspace @uppy/dashboard playwright install chromium
build_status=0
corepack yarn run build || build_status=$?
if [ "${build_status}" -ne 0 ]; then
    [ "${build_status}" -ge 128 ]
    corepack yarn run build --filter='!@uppy/svelte'
fi
node -e "const fs = require('fs'); require('./package.json'); for (const path of ['packages/@uppy/core/lib/index.js', 'node_modules/typescript/package.json', 'node_modules/turbo/package.json', 'node_modules/vitest/package.json', 'node_modules/playwright/package.json']) fs.accessSync(path); fs.accessSync(require('playwright').chromium.executablePath(), fs.constants.X_OK); console.log('DEPS_OK')"
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
"""

_REINSTALL = """
if ! git diff --quiet HEAD -- yarn.lock .yarnrc.yml package.json; then
    corepack yarn install
fi
"""

_APPLY_FLAGS = (
    "--whitespace=nowarn --exclude='*.woff2' --exclude='*.png' "
    "--exclude='*.ico' --exclude='*.jpg'"
)

_TEST = """
corepack yarn workspaces list --json
corepack yarn run build || test -f packages/@uppy/core/lib/index.js

filters=(--filter='./packages/@uppy/*' --filter='./packages/uppy' --filter='./examples/{react,vue,sveltekit}')
if [ -d private/tsconfig ]; then
    filters+=(--filter='./private/tsconfig')
fi

status=0
corepack yarn turbo run test --continue --concurrency=1 --log-order=stream "${filters[@]}" -- --browser.headless --reporter=verbose || status=$?
if compgen -G 'website/test/*.test.mjs' > /dev/null; then
    (cd website && node --test --test-reporter=junit test/*.test.mjs) || status=$?
fi
exit "${status}"
"""

_RUN_SH = _RUN_HEADER + _TEST

_TEST_RUN_SH = (
    _RUN_HEADER
    + f"git apply {_APPLY_FLAGS} /home/test.patch\n"
    + _REINSTALL
    + _TEST
)

_FIX_RUN_SH = (
    _RUN_HEADER
    + f"git apply {_APPLY_FLAGS} /home/test.patch /home/fix.patch\n"
    + _REINSTALL
    + _TEST
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_VITEST_RE = re.compile(
    r"^(?P<package>\S+?):test:\s+(?P<mark>[✓×↓□])\s+(?:\|[^|]+\|\s+|\S.*?\s{2,})?"
    r"(?P<path>\S+\.(?:test|spec)\.[cm]?[jt]sx?)\s+>\s+(?P<title>.+?)\s*$"
)
_TIMING_RE = re.compile(
    r"(?:\s+\d+(?:\.\d+)?m?s)?(?:\s+\(retry x\d+\))?(?:\s+\(repeat x\d+\))?(?:\s+\d+ MB heap used)?$"
)
_SKIP_NOTE_RE = re.compile(r"\s+\[[^\]]*\]$")
_JUNIT_CASE_RE = re.compile(
    r'^\s*<testcase name="(?P<name>[^"]*)" time="[^"]*" classname="(?P<suite>[^"]*)" '
    r'file="/home/uppy/(?P<path>[^"]*)"(?P<rest>.*)$'
)


class UppyImageBase6362To6802(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""
        return f"""# syntax=docker/dockerfile:1.6

FROM {_NODE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\
    YARN_NODE_LINKER=node-modules

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt
{global_env}
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    fonts-freefont-ttf \\
    fonts-ipafont-gothic \\
    fonts-liberation \\
    fonts-noto-color-emoji \\
    fonts-tlwg-loma-otf \\
    fonts-unifont \\
    fonts-wqy-zenhei \\
    libasound2 \\
    libatk-bridge2.0-0 \\
    libatk1.0-0 \\
    libatspi2.0-0 \\
    libcairo2 \\
    libcups2 \\
    libdbus-1-3 \\
    libdrm2 \\
    libfontconfig1 \\
    libfreetype6 \\
    libgbm1 \\
    libglib2.0-0 \\
    libnspr4 \\
    libnss3 \\
    libpango-1.0-0 \\
    libx11-6 \\
    libxcb1 \\
    libxcomposite1 \\
    libxdamage1 \\
    libxext6 \\
    libxfixes3 \\
    libxkbcommon0 \\
    libxrandr2 \\
    xfonts-scalable \\
    xvfb \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null
{clear_env}
CMD ["/bin/bash"]
"""


class UppyImageDefault6362To6802(Image):
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
        return UppyImageBase6362To6802(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, script: str) -> str:
        return (
            script.replace("__ENV__", _ENV)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._render(_PREPARE)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""
        return f"""FROM {base.image_full_name()}
{global_env}
{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
{clear_env}"""


@Instance.register("transloadit", "uppy")
@Instance.register("transloadit", _NUMBER_INTERVAL)
class UPPY_6362_TO_6802(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return UppyImageDefault6362To6802(self.pr, self._config)

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
        lines = _ANSI_RE.sub("", test_log).splitlines()

        locations: dict[str, str] = {}
        for line in lines:
            if line.startswith('{"location":'):
                workspace = json.loads(line)
                locations[workspace["name"]] = workspace["location"]

        pending = ""
        for line in lines:
            stripped = line.strip()
            if pending:
                if stripped.startswith("<failure"):
                    failed_tests.add(pending)
                    pending = ""
                elif stripped.startswith("<skipped"):
                    skipped_tests.add(pending)
                    pending = ""
                elif stripped.startswith("</testcase"):
                    passed_tests.add(pending)
                    pending = ""
                continue

            junit = _JUNIT_CASE_RE.match(line)
            if junit:
                title = html.unescape(junit.group("name"))
                suite = html.unescape(junit.group("suite"))
                prefix = junit.group("path")
                name = (
                    f"{prefix} > {title}"
                    if suite == "test"
                    else f"{prefix} > {suite} > {title}"
                )
                rest = junit.group("rest")
                if ' failure="' in rest:
                    failed_tests.add(name)
                elif rest.rstrip().endswith("/>"):
                    passed_tests.add(name)
                else:
                    pending = name
                continue

            vitest = _VITEST_RE.match(line)
            if not vitest:
                continue
            package = vitest.group("package")
            mark = vitest.group("mark")
            title = vitest.group("title")
            location = locations.get(package, package)
            path = vitest.group("path")
            prefix = f"{location}/{path}" if location != "." else path
            if mark in "↓□":
                skipped_tests.add(f"{prefix} > {_SKIP_NOTE_RE.sub('', title)}")
            elif mark == "✓":
                passed_tests.add(f"{prefix} > {_TIMING_RE.sub('', title)}")
            else:
                failed_tests.add(f"{prefix} > {_TIMING_RE.sub('', title)}")

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
