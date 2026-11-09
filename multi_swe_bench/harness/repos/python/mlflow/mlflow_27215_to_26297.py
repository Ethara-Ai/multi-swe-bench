import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "mlflow_27215_to_26297"
_PYTHON_IMAGE = "python:3.11.17-bookworm"
_NODE_VERSION = "24.22.0"
_YARN_CLASSIC = "1.22.22"
_UV = "0.13.6"
_VENV = "/opt/mlflow-venv"
_JS_DIR = "mlflow/server/js"
_PY_EXTRAS = "--extra gateway --extra mcp --extra genai"

_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_PY_TEST_FILE = re.compile(r"^tests/(?:[^/]+/)*test_[^/]+\.py$")
_JS_TEST_FILE = re.compile(
    r"^mlflow/server/js/(?:[^/]+/)*[^/]+\.test\.(?:js|jsx|ts|tsx)$"
)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_NODE = r"\S+\.py::[^\s\[]+(?:\[[^\]]*\])?"
_VERBOSE = re.compile(
    rf"^(?P<name>{_NODE})\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_SUMMARY = re.compile(
    rf"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<name>{_NODE})(?:\s+-\s.*)?$"
)
_JEST = re.compile(r"^JEST_RESULT (?P<status>PASSED|FAILED|SKIPPED) (?P<name>\S+::.+)$")
_PASS = {"PASSED", "XPASS"}
_FAIL = {"FAILED", "ERROR"}
_SKIP = {"SKIPPED", "XFAIL"}

_RUN_ENV = f"""export CI=true
export TZ=UTC
export PIP_DISABLE_PIP_VERSION_CHECK=1
export UV_PROJECT_ENVIRONMENT={_VENV}
export VIRTUAL_ENV={_VENV}
export UV_PYTHON=/usr/local/bin/python3
export UV_PYTHON_DOWNLOADS=never
export UV_LINK_MODE=copy
export PATH={_VENV}/bin:$PATH
export YARN_ENABLE_TELEMETRY=0
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export NODE_OPTIONS=--max-old-space-size=8192
"""

_JEST_REPORTER = """const path = require("path");

class NameReporter {
  onTestResult(test, testResult) {
    const file = path
      .relative(process.cwd(), testResult.testFilePath)
      .split(path.sep)
      .join("/");
    for (const r of testResult.testResults) {
      let status = null;
      if (r.status === "passed") {
        status = "PASSED";
      } else if (r.status === "failed") {
        status = "FAILED";
      } else if (["pending", "skipped", "todo", "disabled"].includes(r.status)) {
        status = "SKIPPED";
      }
      if (status === null) {
        continue;
      }
      const name = [...r.ancestorTitles, r.title].join(" > ").replace(/\\s+/g, " ").trim();
      process.stdout.write(`JEST_RESULT ${status} ${file}::${name}\\n`);
    }
  }
}

module.exports = NameReporter;
"""

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


def _patch_files(patch: str) -> list[str]:
    files = []
    for path in _DIFF_FILE.findall(patch or ""):
        if path not in files:
            files.append(path)
    return files


def _py_tests(pr: PullRequest) -> list[str]:
    return [f for f in _patch_files(pr.test_patch) if _PY_TEST_FILE.match(f)]


def _js_tests(pr: PullRequest) -> list[str]:
    prefix = _JS_DIR + "/"
    return [
        f[len(prefix) :] for f in _patch_files(pr.test_patch) if _JS_TEST_FILE.match(f)
    ]


def _py_block(files: list[str], capture: str) -> str:
    return f"""PY_TARGETS=""
for f in {" ".join(files)}; do
    if [ -f "$f" ]; then
        PY_TARGETS="$PY_TARGETS $f"
    fi
done
if [ -n "$PY_TARGETS" ]; then
    python -m pytest -p no:cacheprovider --color=no -rA $PY_TARGETS{capture}
fi
"""


def _js_block(files: list[str], capture: str) -> str:
    return f"""JS_TARGETS=""
for f in {" ".join(files)}; do
    if [ -f "{_JS_DIR}/$f" ]; then
        JS_TARGETS="$JS_TARGETS $f"
    fi
done
if [ -n "$JS_TARGETS" ]; then
    cd /home/mlflow/{_JS_DIR}
    yarn test --ci --maxWorkers=2 --reporters=default --reporters=/home/jest-name-reporter.js --runTestsByPath $JS_TARGETS{capture}
    cd /home/mlflow
fi
"""


def _run_tests(pr: PullRequest) -> str:
    py = _py_tests(pr)
    js = _js_tests(pr)
    if py and js:
        capture = " || STATUS=$?"
        return (
            "STATUS=0\n"
            + _py_block(py, capture)
            + _js_block(js, capture)
            + "exit $STATUS\n"
        )
    if py:
        return _py_block(py, "")
    if js:
        return _js_block(js, "")
    return ""


def _install(pr: PullRequest) -> str:
    steps = []
    gates = []
    if _py_tests(pr):
        steps.append(
            f"""EXCLUDE_NEWER="$(git log -1 --format=%cI HEAD)"
uv sync --frozen {_PY_EXTRAS}
uv pip install --python {_VENV}/bin/python --exclude-newer "$EXCLUDE_NEWER" -r requirements/test-requirements.txt
"""
        )
        gates.append(
            f"""{_VENV}/bin/python -c "import mlflow, pytest, tests; print('DEPS_OK python', mlflow.__version__, pytest.__version__)"
"""
        )
    if _js_tests(pr):
        steps.append(
            f"""cd /home/mlflow/{_JS_DIR}
yarn install --immutable
cd /home/mlflow
"""
        )
        gates.append(
            f"""cd /home/mlflow/{_JS_DIR}
node -e "require.resolve('jest/package.json'); require.resolve('@craco/craco/package.json'); require.resolve('react-scripts/package.json'); console.log('DEPS_OK js', process.version)"
cd /home/mlflow
"""
        )
    return "\n".join(steps) + "\n" + "".join(gates)


class Mlflow27215To26297ImageBase(Image):
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
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

FROM {_PYTHON_IMAGE}

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

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) node_arch=x64 ;; \\
        arm64) node_arch=arm64 ;; \\
        *) echo "unsupported architecture: $arch"; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-${{node_arch}}.tar.xz" \\
        | tar -xJ -C /usr/local --strip-components=1 --no-same-owner; \\
    node --version; \\
    corepack enable; \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack prepare yarn@{_YARN_CLASSIC} --activate; \\
    yarn --version; \\
    python -m pip install --no-cache-dir "uv=={_UV}"; \\
    uv --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Mlflow27215To26297ImageDefault(Image):
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
        return Mlflow27215To26297ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        run_tests = _run_tests(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "jest-name-reporter.js", _JEST_REPORTER),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
git submodule update --init --recursive
bash /home/check_git_changes.sh

{run_env}
{install}""".format(
                    repo=repo,
                    base_sha=self.pr.base.sha,
                    run_env=_RUN_ENV,
                    install=_install(self.pr),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
{run_tests}""".format(repo=repo, run_env=_RUN_ENV, run_tests=run_tests),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
git apply --whitespace=nowarn /home/test.patch

{run_tests}""".format(repo=repo, run_env=_RUN_ENV, run_tests=run_tests),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{run_tests}""".format(repo=repo, run_env=_RUN_ENV, run_tests=run_tests),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
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
    fi
"""


@Instance.register("mlflow", "mlflow")
@Instance.register("mlflow", _INTERVAL)
class MLFLOW_27215_TO_26297(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Mlflow27215To26297ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for raw in _ANSI.sub("", log).splitlines():
            line = raw.strip()
            match = _JEST.match(line) or _VERBOSE.match(line) or _SUMMARY.match(line)
            if not match:
                continue
            name = match.group("name").strip()
            status = match.group("status")
            if status in _FAIL:
                failed_tests.add(name)
            elif status in _PASS:
                passed_tests.add(name)
            elif status in _SKIP:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
