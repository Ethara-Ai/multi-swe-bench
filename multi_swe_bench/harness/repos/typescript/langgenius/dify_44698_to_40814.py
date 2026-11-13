from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22-bookworm"
_BASE_TAG = "base-44698_to_40814"
_WEB_DIR = "web"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_NEW_FILE_RE = re.compile(r"^new file mode ", re.M)

_VITEST_CASE_RE = re.compile(
    r"^\s*([✓✔×✗↓·])\s+(?:\|([^|]+)\|\s+)?(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\s*>\s*(.+?)"
    r"\s*(?:\[(?:skipped|todo)\])?\s*(?:\(\s*\d+(?:\.\d+)?\s*m?s\s*\))?\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$"
)
_VITEST_SUITE_FAIL_RE = re.compile(
    r"^\s*FAIL\s+(?:\|([^|]+)\|\s+)?(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\s*(?:\[.*\])?\s*$"
)


def _diff_sections(patch: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    matches = list(_DIFF_HEADER_RE.finditer(patch or ""))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(patch)
        sections.append((m.group(2), patch[start:end]))
    return sections


def _test_targets(pr: PullRequest) -> list[str]:
    targets: list[str] = []
    for path, _body in _diff_sections(pr.test_patch or ""):
        if not path.startswith(f"{_WEB_DIR}/"):
            continue
        if not re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", path):
            continue
        rel = path[len(f"{_WEB_DIR}/"):]
        if rel not in targets:
            targets.append(rel)
    return sorted(targets)


def _parse_log(test_log: str) -> TestResult:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    clean = _ANSI_RE.sub("", test_log).replace("\r\n", "\n")
    for raw in clean.splitlines():
        line = raw.rstrip()
        m = _VITEST_CASE_RE.match(line)
        if m:
            mark, project, path, title = m.groups()
            title = title.strip()
            key = f"[{project.strip()}] " if project else ""
            name = f"{path} > {key}{title}"
            if mark in "✓✔":
                passed.add(name)
            elif mark in "×✗":
                failed.add(name)
            else:
                skipped.add(name)
            continue
        m = _VITEST_SUITE_FAIL_RE.match(line)
        if m:
            project, path = m.groups()
            key = f"[{project.strip()}] " if project else ""
            failed.add(f"{path} > {key}<suite>")

    passed -= failed
    passed -= skipped
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export NPM_CONFIG_UPDATE_NOTIFIER=false
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export DO_NOT_TRACK=1
export TURBO_TELEMETRY_DISABLED=1
export NODE_EXTRA_CA_CERTS="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"

cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin "__BASE_SHA__"
git checkout --detach __BASE_SHA__
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

# pnpm via corepack from the repo's `packageManager` field — do not shadow with `npm install -g` (it fights the shim).
# `corepack enable` creates the pnpm shim; `prepare --activate` pins the exact version from packageManager.
pm_full="$(node -p "require('./package.json').packageManager||''" 2>/dev/null || true)"
[ -n "$pm_full" ] || { echo "prepare.sh: package.json has no 'packageManager' field"; exit 1; }
corepack enable
corepack prepare "$pm_full" --activate
pnpm --version

[ -d __WEB_DIR__ ] || { echo "prepare.sh: __WEB_DIR__/ not found at this commit"; exit 1; }

for attempt in 1 2 3; do
  pnpm install --frozen-lockfile && break
  [ "$attempt" -lt 3 ] || { echo "prepare.sh: pnpm install failed 3 times"; exit 1; }
  sleep 30
done

cd /home/__REPO__/__WEB_DIR__
node -e "require.resolve('vitest/package.json')" >/dev/null 2>&1 || {
  echo "prepare.sh: vitest not resolvable in __WEB_DIR__"
  exit 1
}
./node_modules/.bin/vitest --version
echo "DEPS_OK"
"""


_TEST_BLOCK = r"""cd /home/__REPO__/__WEB_DIR__

TEST_TARGETS=""
for candidate in __TEST_TARGETS__; do
    if [ -e "${candidate}" ]; then
        TEST_TARGETS="${TEST_TARGETS} ${candidate}"
    fi
done

if [ -z "${TEST_TARGETS}" ]; then
    echo "No test targets present at this stage"
    exit 0
fi

./node_modules/.bin/vitest run \
    --no-color \
    --reporter=verbose \
    --no-file-parallelism \
    --passWithNoTests \
    ${TEST_TARGETS}
"""


_RUN_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export NPM_CONFIG_UPDATE_NOTIFIER=false
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export DO_NOT_TRACK=1
export NODE_EXTRA_CA_CERTS="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
"""


_RUN_SH = _RUN_HEADER + r"""
cd /home/__REPO__

__TEST_BLOCK__
"""


_TEST_RUN_SH = _RUN_HEADER + r"""
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch

__TEST_BLOCK__
"""


_FIX_RUN_SH = _RUN_HEADER + r"""
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_BLOCK__
"""


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    DO_NOT_TRACK=1

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

__GLOBAL_ENV__

RUN node --version && npm --version && git --version

WORKDIR /home/

__CODE__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


_CLONE_CODE = r"""RUN git clone "${REPO_URL}" /home/__REPO__ && \
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null"""


_COPY_CODE = r"""COPY __REPO__ /home/__REPO__"""


_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

WORKDIR /home/__REPO__

__HARDENING__
__CLEAR_ENV__
"""


class DifyImageBase_44698_TO_40814(Image):

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
        code = _CLONE_CODE if self.config.need_clone else _COPY_CODE
        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class DifyImageDefault_44698_TO_40814(Image):

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
        return DifyImageBase_44698_TO_40814(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        targets = _test_targets(self.pr)
        pattern = "|".join(re.escape(t) for t in targets) if targets else "__NONE__"
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__TEST_TARGETS__", " ".join(targets) if targets else "")
            .replace("__TEST_PATTERN__", pattern)
            .replace("__WEB_DIR__", _WEB_DIR)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(
            f"COPY {f.name} /home/\n" for f in self.files()
        )
        hardening = Image._HARDENING_BLOCK.replace(
            '"${BASE_COMMIT}"', self.pr.base.sha
        )
        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__HARDENING__", hardening)
        )


@Instance.register("langgenius", "dify_44698_to_40814")
class DIFY_44698_TO_40814(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return DifyImageDefault_44698_TO_40814(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)


Instance.register("langgenius", "dify")(DIFY_44698_TO_40814)
