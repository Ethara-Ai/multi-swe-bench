from __future__ import annotations

import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "stablyai"
_REPO = "orca"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"orca_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like orca_<hi>_to_<lo>.py")
_BASE_TAG = f"base-{_ERA_MATCH.group(1)}_to_{_ERA_MATCH.group(2)}"
_NODE_IMAGE = "node:24.20.0-bookworm"
_MOBILE_DIR = "mobile"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_UNIT_TEST_RE = re.compile(r"\.test\.(?:ts|tsx|mts|mjs)$")
_RESULT_RE = re.compile(r"^(PASSED|FAILED|SKIPPED) (\S.*)$")


def _test_targets(pr: PullRequest) -> tuple[list[str], list[str]]:
    root: list[str] = []
    mobile: list[str] = []
    for m in _DIFF_HEADER_RE.finditer(pr.test_patch or ""):
        path = m.group(2)
        if not _UNIT_TEST_RE.search(path):
            continue
        if path.startswith(f"{_MOBILE_DIR}/"):
            rel = path[len(f"{_MOBILE_DIR}/"):]
            if rel not in mobile:
                mobile.append(rel)
        elif path not in root:
            root.append(path)
    return sorted(root), sorted(mobile)


def _parse_log(test_log: str) -> TestResult:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    clean = _ANSI_RE.sub("", test_log).replace("\r\n", "\n")
    for raw in clean.splitlines():
        m = _RESULT_RE.match(raw.rstrip())
        if not m:
            continue
        status, name = m.groups()
        if status == "PASSED":
            passed.add(name)
        elif status == "FAILED":
            failed.add(name)
        else:
            skipped.add(name)

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


_VITEST_REPORT_CJS = r"""const fs = require('fs');

const path = process.argv[2];
const ROOT = '/home/__REPO__/';

if (!path || !fs.existsSync(path)) {
    console.log('vitest-report: no results file at ' + path);
    process.exit(0);
}

let report;
try {
    report = JSON.parse(fs.readFileSync(path, 'utf8'));
} catch (e) {
    console.log('vitest-report: could not parse ' + path + ': ' + e.message);
    process.exit(0);
}

const STATUS = {passed: 'PASSED', failed: 'FAILED', pending: 'SKIPPED', skipped: 'SKIPPED', todo: 'SKIPPED', disabled: 'SKIPPED'};

for (const suite of report.testResults || []) {
    let file = String(suite.name || '').replace(/\\/g, '/');
    if (file.startsWith(ROOT)) file = file.slice(ROOT.length);

    const results = suite.assertionResults || [];
    if (results.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    const seen = new Map();
    for (const a of results) {
        let name = [file].concat(a.ancestorTitles || [], [a.title]).join(' > ');
        const n = (seen.get(name) || 0) + 1;
        seen.set(name, n);
        if (n > 1) name += ' #' + n;
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + name);
    }
}

console.log('vitest-report: suites=' + (report.numTotalTestSuites || 0) +
            ' tests=' + (report.numTotalTests || 0) +
            ' passed=' + (report.numPassedTests || 0) +
            ' failed=' + (report.numFailedTests || 0) +
            ' pending=' + (report.numPendingTests || 0));
"""


_ENV_BLOCK = r"""export CI=true
export HUSKY=0
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export ORCA_BACKGROUND_LAUNCH=1
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export NPM_CONFIG_UPDATE_NOTIFIER=false
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export DO_NOT_TRACK=1
export NODE_EXTRA_CA_CERTS="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
"""


_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

__ENV_BLOCK__
cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin "__BASE_SHA__"
git checkout --detach __BASE_SHA__
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

cat > /home/vitest-report.cjs <<'__VITEST_REPORT_EOF__'
__VITEST_REPORT__
__VITEST_REPORT_EOF__

pm_full="$(node -p "require('./package.json').packageManager||''" 2>/dev/null || true)"
[ -n "$pm_full" ] || { echo "prepare.sh: package.json has no 'packageManager' field"; exit 1; }
corepack enable
corepack prepare "$pm_full" --activate
pnpm --version

for attempt in 1 2 3; do
  pnpm install --frozen-lockfile --ignore-scripts && break || true
  [ "$attempt" -lt 3 ] || { echo "prepare.sh: pnpm install failed 3 times"; exit 1; }
  echo "prepare.sh: pnpm install attempt $attempt failed; retrying in 30s"
  sleep 30
done
pnpm run --if-present build:packages || true
node config/scripts/native-build/node-addons/ensure-native-runtime.mjs --runtime=node || true

if [ -n "__MOBILE_TARGETS__" ]; then
  for attempt in 1 2 3; do
    (cd __MOBILE_DIR__ && pnpm install --frozen-lockfile --ignore-scripts) && break || true
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: mobile pnpm install failed 3 times"; exit 1; }
    echo "prepare.sh: mobile pnpm install attempt $attempt failed; retrying in 30s"
    sleep 30
  done
fi

test -s /home/vitest-report.cjs
node -e "require.resolve('vitest/package.json'); require('node-pty')"
./node_modules/.bin/vitest --version
if [ -n "__MOBILE_TARGETS__" ]; then
  (cd __MOBILE_DIR__ && ./node_modules/.bin/vitest --version)
fi
echo "DEPS_OK"
"""


_TEST_BLOCK = r"""collect_targets() {
    local base="$1"
    shift
    local -A requested=()
    local candidate dir sibling rel found depth
    for candidate in "$@"; do
        requested["${candidate}"]=1
    done
    for candidate in "$@"; do
        if [ -e "${base}/${candidate}" ]; then
            printf '%s\n' "${candidate}"
        fi
        dir="$(dirname "${candidate}")"
        depth=0
        while [ "${dir}" != "." ] && [ "${depth}" -lt 3 ]; do
            found=0
            if [ -d "${base}/${dir}" ]; then
                for sibling in "${base}/${dir}"/*.test.ts "${base}/${dir}"/*.test.tsx "${base}/${dir}"/*.test.mts "${base}/${dir}"/*.test.mjs; do
                    [ -f "${sibling}" ] || continue
                    case "${sibling}" in
                        *.electron.test.*|*.live-shell.test.*|*.node-pty.test.*|*.win32.test.*|*.darwin.test.*) continue ;;
                    esac
                    rel="${sibling#"${base}"/}"
                    [ -z "${requested[${rel}]:-}" ] || continue
                    printf '%s\n' "${rel}"
                    found=1
                done
            fi
            [ "${found}" -eq 0 ] || break
            dir="$(dirname "${dir}")"
            depth=$((depth + 1))
        done
    done | sort -u
}

mapfile -t ROOT_TARGETS < <(collect_targets . __ROOT_TARGETS__)
mapfile -t MOBILE_TARGETS < <(collect_targets __MOBILE_DIR__ __MOBILE_TARGETS__)
echo "Root test targets: ${#ROOT_TARGETS[@]}"
printf '  %s\n' "${ROOT_TARGETS[@]}"
echo "Mobile test targets: ${#MOBILE_TARGETS[@]}"
printf '  %s\n' "${MOBILE_TARGETS[@]}"

if [ "${#ROOT_TARGETS[@]}" -eq 0 ] && [ "${#MOBILE_TARGETS[@]}" -eq 0 ]; then
    echo "No test targets present at this stage"
    exit 0
fi

status=0

if [ "${#ROOT_TARGETS[@]}" -gt 0 ]; then
    rm -f /tmp/vitest-root.json
    rc=0
    if node config/scripts/workspace-package-build/build-workspace-packages.mjs \
        && node config/scripts/native-build/node-addons/ensure-native-runtime.mjs --runtime=node; then
        ./node_modules/.bin/vitest run --config config/vitest.config.ts \
            --no-color \
            --maxWorkers=4 \
            --passWithNoTests \
            --reporter=default \
            --reporter=json --outputFile.json=/tmp/vitest-root.json \
            "${ROOT_TARGETS[@]}" || rc=$?
    else
        rc=$?
        echo "root test setup failed with code $rc" >&2
    fi
    node /home/vitest-report.cjs /tmp/vitest-root.json
    if [ ! -s /tmp/vitest-root.json ]; then
        echo "vitest exited with code $rc without writing /tmp/vitest-root.json" >&2
        status="$(( rc == 0 ? 1 : rc ))"
    fi
fi

if [ "${#MOBILE_TARGETS[@]}" -gt 0 ]; then
    rm -f /tmp/vitest-mobile.json
    mrc=0
    (cd __MOBILE_DIR__ && ./node_modules/.bin/vitest run \
        --no-color \
        --maxWorkers=2 \
        --passWithNoTests \
        --reporter=default \
        --reporter=json --outputFile.json=/tmp/vitest-mobile.json \
        "${MOBILE_TARGETS[@]}") || mrc=$?
    node /home/vitest-report.cjs /tmp/vitest-mobile.json
    if [ ! -s /tmp/vitest-mobile.json ]; then
        echo "vitest exited with code $mrc without writing /tmp/vitest-mobile.json" >&2
        if [ "$status" -eq 0 ]; then
            status="$(( mrc == 0 ? 1 : mrc ))"
        fi
    fi
fi

exit "$status"
"""


_RUN_HEADER = r"""#!/bin/bash
set -eo pipefail

__ENV_BLOCK__"""


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
        git ca-certificates curl build-essential python3 \
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


class OrcaImageBase(Image):

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


class OrcaImageDefault(Image):

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
        return OrcaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        root, mobile = _test_targets(self.pr)
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__ENV_BLOCK__", _ENV_BLOCK)
            .replace("__VITEST_REPORT__", _VITEST_REPORT_CJS.rstrip("\n"))
            .replace("__ROOT_TARGETS__", " ".join(root))
            .replace("__MOBILE_TARGETS__", " ".join(mobile))
            .replace("__MOBILE_DIR__", _MOBILE_DIR)
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
        hardening = (
            Image._HARDENING_BLOCK.replace('"${BASE_COMMIT}"', self.pr.base.sha)
            .replace("git gc --prune=now --aggressive", "git gc --prune=now")
        )
        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__HARDENING__", hardening)
        )


@Instance.register(_ORG, _ERA_FILE)
class ORCA(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return OrcaImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)


Instance.register(_ORG, _REPO)(ORCA)
