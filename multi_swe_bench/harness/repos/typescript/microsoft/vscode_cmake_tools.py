from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "microsoft"
_REPO = "vscode-cmake-tools"
_REPO_DIR = f"/home/{_REPO}"

_ERA_B_MIN_PR = 4055

_ERA_A_NODE_IMAGE = "node:16.20.2-bookworm"
_ERA_A_VSCODE_VERSION = "1.63.2"
_ERA_A_BASE_TAG = "base-node16"

_ERA_B_NODE_IMAGE = "node:20.19.5-bookworm"
_ERA_B_VSCODE_VERSION = "1.96.4"
_ERA_B_BASE_TAG = "base-node20"

_VSCODE_DIR = "/opt/vscode"
_VSCODE_EXECUTABLE = f"{_VSCODE_DIR}/code"
_FAKEBIN_BUILD_DIR = "/tmp/cmt-fakebin-build"
_GATE_LOG = "/tmp/cmt-prepare-gate.log"

_ADDED_TEST_RE = re.compile(r"^\+\s*(?:test|suite)\s*\(")
_SUITE_DIRS = ("extension-tests", "end-to-end-tests", "smoke")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"###CMT_TEST###\s+(PASS|FAIL|SKIP)\s+::\s+(\S.*?)\s*$")


def _is_era_b(pr: PullRequest) -> bool:
    return int(pr.number) >= _ERA_B_MIN_PR


def _node_image(pr: PullRequest) -> str:
    return _ERA_B_NODE_IMAGE if _is_era_b(pr) else _ERA_A_NODE_IMAGE


def _vscode_version(pr: PullRequest) -> str:
    return _ERA_B_VSCODE_VERSION if _is_era_b(pr) else _ERA_A_VSCODE_VERSION


def _base_tag(pr: PullRequest) -> str:
    return _ERA_B_BASE_TAG if _is_era_b(pr) else _ERA_A_BASE_TAG


def _touched_test_files(pr: PullRequest) -> list[str]:
    touched: list[str] = []
    with_new_cases: list[str] = []
    current = ""
    for line in (pr.test_patch or "").split("\n"):
        if line.startswith("diff --git a/"):
            current = line.split(" a/", 1)[1].split(" b/", 1)[0]
            if current.startswith("test/") and current.endswith(".test.ts"):
                if current not in touched:
                    touched.append(current)
            else:
                current = ""
            continue
        if current and _ADDED_TEST_RE.match(line) and current not in with_new_cases:
            with_new_cases.append(current)
    return with_new_cases or touched


def _suite_targets(pr: PullRequest) -> list[tuple[str, str, str, str]]:
    targets: list[tuple[str, str, str, str]] = []
    for path in _touched_test_files(pr):
        parts = path.split("/")
        if len(parts) > 3 and parts[1] in _SUITE_DIRS:
            tests_root = "/".join(parts[:3])
            workspace = f"{tests_root}/project-folder"
        else:
            tests_root = "/".join(parts[:2])
            workspace = f"{tests_root}/test-project-without-cmakelists"
        relative = "/".join(parts[len(tests_root.split("/")):])
        pattern = "^" + re.escape(relative[: -len(".ts")] + ".js") + "$"
        target = (tests_root, workspace, pattern, tests_root)
        if target not in targets:
            targets.append(target)
    return targets


def _run_suite_lines(pr: PullRequest) -> str:
    lines = []
    for tests_root, workspace, pattern, label in _suite_targets(pr):
        lines.append(
            f'run_suite "{tests_root}" "{workspace}" "{pattern}" "{label}" || status=1'
        )
    return "\n".join(lines)


def _compiled_test_files(pr: PullRequest) -> list[str]:
    files = []
    for path in _touched_test_files(pr):
        compiled = f"out/{path[: -len('.ts')]}.js"
        if compiled not in files:
            files.append(compiled)
    return files


def _workspace_reset_lines(pr: PullRequest) -> str:
    paths = []
    for _, workspace, _, _ in _suite_targets(pr):
        paths.append(f"{workspace}/build")
        paths.append(f"{workspace}/.vscode/CMakeTools")
    return "rm -rf " + " ".join(f'"{path}"' for path in paths) + " out"


def _gate_target(pr: PullRequest) -> tuple[str, str, str, str]:
    targets = _suite_targets(pr)
    return targets[0]


_CMT_TEST_INDEX_JS = r"""const path = require('path');

const repoDir = process.env.CMT_REPO_DIR;
const testsRoot = process.env.CMT_TESTS_ROOT;
const suiteLabel = process.env.CMT_SUITE_LABEL || '';
const marker = '###CMT_TEST###';

const moduleAlias = require(path.join(repoDir, 'node_modules', 'module-alias'));
moduleAlias(repoDir);

const Mocha = require(path.join(repoDir, 'node_modules', 'mocha'));
const glob = require(path.join(repoDir, 'node_modules', 'glob'));

function fullName(test) {
    const parts = typeof test.titlePath === 'function' ? test.titlePath() : [test.title];
    const joined = parts.filter(Boolean).join(' > ');
    return suiteLabel ? `${suiteLabel} > ${joined}` : joined;
}

function cmtReporter(runner) {
    runner.on('pass', test => console.log(`${marker} PASS :: ${fullName(test)}`));
    runner.on('pending', test => console.log(`${marker} SKIP :: ${fullName(test)}`));
    runner.on('fail', (test, err) => {
        console.log(`${marker} FAIL :: ${fullName(test)}`);
        const detail = (err && (err.message || err.stack)) || String(err);
        console.log(`${marker} ERROR :: ${String(detail).split('\n')[0]}`);
    });
    runner.on('end', () => console.log(`${marker} DONE`));
}

exports.run = function run() {
    const mocha = new Mocha({ ui: 'tdd', color: false, reporter: cmtReporter });
    mocha.timeout(100000);

    return new Promise((resolve, reject) => {
        glob('**/*.test.js', { cwd: testsRoot }, (globError, files) => {
            if (globError) {
                return reject(globError);
            }
            const regex = process.env.TEST_FILTER ? new RegExp(process.env.TEST_FILTER) : /.*/;
            files.forEach(file => {
                if (regex.test(file)) {
                    mocha.addFile(path.resolve(testsRoot, file));
                }
            });
            try {
                mocha.run(failures => {
                    if (failures > 0) {
                        reject(new Error(`${failures} tests failed.`));
                    } else {
                        resolve();
                    }
                });
            } catch (runError) {
                reject(runError);
            }
        });
    });
};
"""


_CMT_VSCODE_RUNNER_JS = r"""const cp = require('child_process');
const fs = require('fs');
const path = require('path');

const marker = '###CMT_TEST###';
const repoDir = process.env.CMT_REPO_DIR;
const workspace = process.env.CMT_WORKSPACE;
const vscodeExecutable = process.env.CMT_VSCODE_EXECUTABLE || '/opt/vscode/code';
const testIndex = process.env.CMT_TEST_INDEX || '/home/cmt_test_index.js';
const maxAttempts = Math.max(1, Number(process.env.CMT_MAX_ATTEMPTS || '2'));
const attemptTimeoutMs = Math.max(60000, Number(process.env.CMT_ATTEMPT_TIMEOUT_MS || '1500000'));

function runAttempt(index) {
    return new Promise(resolve => {
        const sandbox = `/tmp/cmt-vscode-sandbox-${index}`;
        fs.rmSync(sandbox, { recursive: true, force: true });
        fs.mkdirSync(path.join(sandbox, 'user-data'), { recursive: true });
        fs.mkdirSync(path.join(sandbox, 'extensions'), { recursive: true });

        const args = [
            '--no-sandbox',
            '--disable-gpu',
            '--disable-dev-shm-usage',
            '--disable-extensions',
            '--disable-workspace-trust',
            '--disable-updates',
            '--skip-welcome',
            '--skip-release-notes',
            `--user-data-dir=${path.join(sandbox, 'user-data')}`,
            `--extensions-dir=${path.join(sandbox, 'extensions')}`,
            workspace,
            `--extensionDevelopmentPath=${repoDir}`,
            `--extensionTestsPath=${testIndex}`
        ];

        const env = Object.assign({}, process.env, {
            CMT_TESTING: '1',
            CMT_QUIET_CONSOLE: '1',
            TEST_FILTER: process.env.TEST_FILTER || '.*'
        });

        const child = cp.spawn(vscodeExecutable, args, { env });
        let output = '';
        let settled = false;

        const timer = setTimeout(() => {
            output += `\n${marker} TIMEOUT :: attempt ${index} exceeded ${attemptTimeoutMs} ms\n`;
            child.kill('SIGKILL');
        }, attemptTimeoutMs);

        const finish = code => {
            if (settled) {
                return;
            }
            settled = true;
            clearTimeout(timer);
            resolve({ output, code: typeof code === 'number' ? code : 1 });
        };

        child.stdout.on('data', chunk => {
            output += chunk.toString();
        });
        child.stderr.on('data', chunk => {
            output += chunk.toString();
        });
        child.on('error', error => {
            output += `\n${marker} SPAWN_ERROR :: ${error.message}\n`;
            finish(1);
        });
        child.on('close', code => finish(code));
    });
}

async function main() {
    let result = null;
    for (let index = 1; index <= maxAttempts; index += 1) {
        result = await runAttempt(index);
        if (result.output.includes(`${marker} DONE`)) {
            break;
        }
        if (index < maxAttempts) {
            console.log(`${marker} RETRY :: attempt ${index} produced no test results`);
        }
    }
    process.stdout.write(result.output);
    console.log(`${marker} EXIT ${result.code}`);
    process.exit(result.code);
}

void main();
"""


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export DEBIAN_FRONTEND=noninteractive
export ELECTRON_NO_ATTACH_CONSOLE=1

git config --global --add safe.directory '*'

cd __REPO_DIR__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
echo "=== HEAD pinned at $(git rev-parse HEAD) ==="

arch="$(dpkg --print-architecture)"
case "${arch}" in
    amd64) vscode_arch="linux-x64" ;;
    arm64) vscode_arch="linux-arm64" ;;
    *) echo "unsupported architecture: ${arch}" >&2; exit 1 ;;
esac

mkdir -p __VSCODE_DIR__
vscode_url="https://update.code.visualstudio.com/__VSCODE_VERSION__/${vscode_arch}/stable"
downloaded=0
for attempt in 1 2 3 4 5; do
    rm -f /tmp/vscode.tar.gz
    if curl -fL --retry 5 --retry-delay 5 --retry-all-errors \
            --connect-timeout 30 --speed-limit 4096 --speed-time 60 \
            -o /tmp/vscode.tar.gz "${vscode_url}" \
       && tar -tzf /tmp/vscode.tar.gz > /dev/null 2>&1; then
        downloaded=1
        break
    fi
    echo "=== vscode download attempt ${attempt} failed; retrying ===" >&2
    sleep 10
done
test "${downloaded}" = "1"
tar -xzf /tmp/vscode.tar.gz -C __VSCODE_DIR__ --strip-components=1
rm -f /tmp/vscode.tar.gz

install_deps() {
    yarn install --frozen-lockfile --ignore-optional --network-timeout 600000
}

install_deps || {
    echo "=== yarn install failed; clearing cache and retrying once ==="
    yarn cache clean
    rm -rf node_modules
    install_deps
}

./node_modules/.bin/webpack --mode development

rm -rf __FAKEBIN_BUILD_DIR__
cmake -S test/fakeOutputGenerator -B __FAKEBIN_BUILD_DIR__ -DCMAKE_INSTALL_PREFIX:STRING=__REPO_DIR__/test/fakebin
cmake --build __FAKEBIN_BUILD_DIR__
cmake --install __FAKEBIN_BUILD_DIR__ --config Debug

./node_modules/.bin/tsc -p test.tsconfig.json

node --version
yarn --version
cmake --version
ninja --version
gdb --version
command -v xvfb-run
test -x __VSCODE_EXECUTABLE__
test -x test/fakebin/gdb
test -f dist/main.js
node -e "require('__REPO_DIR__/package.json'); console.log('DEPS_OK: package.json')"
node -e "require.resolve('mocha'); require.resolve('glob'); require.resolve('module-alias'); console.log('DEPS_OK: mocha + glob + module-alias resolved')"
__COMPILED_TEST_ASSERTS__

CMT_REPO_DIR=__REPO_DIR__ \
CMT_TEST_INDEX=/home/cmt_test_index.js \
CMT_VSCODE_EXECUTABLE=__VSCODE_EXECUTABLE__ \
CMT_TESTS_ROOT=__REPO_DIR__/out/__GATE_TESTS_ROOT__ \
CMT_WORKSPACE=__REPO_DIR__/__GATE_WORKSPACE__ \
CMT_SUITE_LABEL=__GATE_LABEL__ \
TEST_FILTER=__GATE_PATTERN__ \
    xvfb-run -a node /home/cmt_vscode_runner.js > __GATE_LOG__ 2>&1 || echo "=== baseline probe exited non-zero; verifying it produced results ==="

grep -q '###CMT_TEST### DONE' __GATE_LOG__
grep -qE '###CMT_TEST### (PASS|FAIL|SKIP) ::' __GATE_LOG__
echo "DEPS_OK: vscode test harness produced parseable results"
"""


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export FORCE_COLOR=0
export NO_COLOR=1
export PAGER=/bin/cat
export ELECTRON_NO_ATTACH_CONSOLE=1
export CMT_REPO_DIR=__REPO_DIR__
export CMT_TEST_INDEX=/home/cmt_test_index.js
export CMT_VSCODE_EXECUTABLE=__VSCODE_EXECUTABLE__

cd __REPO_DIR__

apply_patches() {
    if git apply --whitespace=nowarn "$@"; then
        return 0
    fi
    echo "=== plain git apply failed; retrying with --3way ===" >&2
    git reset --hard
    git clean -fd
    git apply --3way --whitespace=nowarn "$@"
}

run_suite() {
    CMT_TESTS_ROOT="__REPO_DIR__/out/$1" \
    CMT_WORKSPACE="__REPO_DIR__/$2" \
    TEST_FILTER="$3" \
    CMT_SUITE_LABEL="$4" \
        xvfb-run -a node /home/cmt_vscode_runner.js
}

git reset --hard
git clean -fd
__WORKSPACE_RESET__
"""


_APPLY_TEST_PATCH = r"""
apply_patches /home/test.patch
"""


_APPLY_BOTH_PATCHES = r"""
apply_patches /home/test.patch /home/fix.patch
"""


_COMPILE_AND_RUN = r"""
./node_modules/.bin/tsc -p test.tsconfig.json || echo "=== tsc reported type errors; continuing with the emitted JavaScript ==="
__COMPILED_TEST_ASSERTS__

status=0
__RUN_SUITE_LINES__
exit "$status"
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
    CI=true \
    NO_COLOR=1 \
    FORCE_COLOR=0 \
    ELECTRON_NO_ATTACH_CONSOLE=1 \
    ELECTRON_DISABLE_SECURITY_WARNINGS=1 \
    DONT_PROMPT_WSL_INSTALL=1

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
    ca-certificates \
    curl \
    git \
    build-essential \
    cmake \
    ninja-build \
    gdb \
    pkg-config \
    python3 \
    xvfb \
    xauth \
    dbus-x11 \
    xdg-utils \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnotify4 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libsecret-1-0 \
    libsecret-1-dev \
    libx11-xcb1 \
    libxcb-dri3-0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxkbcommon0 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${REPO_URL}" __REPO_DIR__ \
 && cd __REPO_DIR__ \
 && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

WORKDIR __REPO_DIR__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

__HARDENING__

__CLEAR_ENV__
"""


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).rstrip("\n") + "\n"


class VscodeCmakeToolsImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _node_image(self.pr)

    def image_tag(self) -> str:
        return _base_tag(self.pr)

    def workdir(self) -> str:
        return _base_tag(self.pr)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_image = self.dependency()
        if isinstance(base_image, Image):
            base_image = base_image.image_full_name()

        return _tidy(
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", base_image)
            .replace("__VSCODE_VERSION__", _vscode_version(self.pr))
            .replace("__VSCODE_EXECUTABLE__", _VSCODE_EXECUTABLE)
            .replace("__VSCODE_DIR__", _VSCODE_DIR)
            .replace("__REPO_DIR__", _REPO_DIR)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class VscodeCmakeToolsImageDefault(Image):
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
        return VscodeCmakeToolsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        gate_tests_root, gate_workspace, gate_pattern, gate_label = _gate_target(self.pr)
        compiled_asserts = "\n".join(
            f'test -f "{path}"' for path in _compiled_test_files(self.pr)
        )
        return (
            template.replace("__COMPILED_TEST_ASSERTS__", compiled_asserts)
            .replace("__RUN_SUITE_LINES__", _run_suite_lines(self.pr))
            .replace("__WORKSPACE_RESET__", _workspace_reset_lines(self.pr))
            .replace("__GATE_TESTS_ROOT__", gate_tests_root)
            .replace("__GATE_WORKSPACE__", gate_workspace)
            .replace("__GATE_PATTERN__", f"'{gate_pattern}'")
            .replace("__GATE_LABEL__", f"'{gate_label}'")
            .replace("__GATE_LOG__", _GATE_LOG)
            .replace("__FAKEBIN_BUILD_DIR__", _FAKEBIN_BUILD_DIR)
            .replace("__VSCODE_VERSION__", _vscode_version(self.pr))
            .replace("__VSCODE_EXECUTABLE__", _VSCODE_EXECUTABLE)
            .replace("__VSCODE_DIR__", _VSCODE_DIR)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__REPO_DIR__", _REPO_DIR)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "cmt_test_index.js", _CMT_TEST_INDEX_JS),
            File(".", "cmt_vscode_runner.js", _CMT_VSCODE_RUNNER_JS),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_SCRIPT_HEADER + _COMPILE_AND_RUN)),
            File(
                ".",
                "test-run.sh",
                self._render(_SCRIPT_HEADER + _APPLY_TEST_PATCH + _COMPILE_AND_RUN),
            ),
            File(
                ".",
                "fix-run.sh",
                self._render(_SCRIPT_HEADER + _APPLY_BOTH_PATCHES + _COMPILE_AND_RUN),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return _tidy(
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace(
                "__HARDENING__",
                Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha),
            )
            .replace("__CLEAR_ENV__", self.clear_env)
        )


@Instance.register(_ORG, _REPO)
class VSCODE_CMAKE_TOOLS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return VscodeCmakeToolsImageDefault(self.pr, self._config)

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
        log = _ANSI_RE.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in log.splitlines():
            match = _RESULT_RE.search(line.rstrip())
            if not match:
                continue
            status, name = match.group(1), match.group(2).strip()
            if not name:
                continue
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
