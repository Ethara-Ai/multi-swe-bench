import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "mlflow_27949_to_6358"
_PYTHON_IMAGE = "python:3.11-bookworm"
_NODE_PRIMARY = "node:24.22.1-bookworm"
_NODE_LEGACY = "node:22.22.0-bookworm"
_YARN = "1.22.22"
_UV = "0.13.6"
_VENV = "/opt/mlflow-venv"
_REPO_DIR = "/home/mlflow"
_JS_DIR = "mlflow/server/js"
_TS_DIR = "libs/typescript"

_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_PY_TEST_FILE = re.compile(r"^tests/(?:[^/]+/)*test_[^/]+\.py$")
_JS_TEST_FILE = re.compile(
    rf"^{_JS_DIR}/(?:[^/]+/)*[^/]+\.test\.(?:js|jsx|ts|tsx)$"
)
_TS_TEST_FILE = re.compile(
    rf"^{_TS_DIR}/(?:[^/]+/)*[^/]+\.test\.(?:js|jsx|ts|tsx)$"
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
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export NODE_OPTIONS=--max-old-space-size=8192
export MLFLOW_TRACKING_URI=sqlite:////tmp/mlflow-test.db
"""

_NODE_PATH = """#!/bin/bash
PKG="$1"
MAJOR="$(sed -n 's/.*"node"[[:space:]]*:[[:space:]]*"[^0-9]*\\([0-9][0-9]*\\).*/\\1/p' "$PKG" 2>/dev/null | head -1)"
if [ -n "$MAJOR" ] && [ -d "/opt/node$MAJOR/bin" ]; then
  echo "/opt/node$MAJOR/bin"
else
  echo "/opt/node-default/bin"
fi
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


_HELPERS = (
    "cat > /home/node-path.sh <<'__NODEPATH_EOF__'\n"
    + _NODE_PATH
    + "__NODEPATH_EOF__\n"
    +     "cat > /home/jest-name-reporter.cjs <<'__JESTREP_EOF__'\n"
    + _JEST_REPORTER
    + "__JESTREP_EOF__\n"
)


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
        f[len(prefix):] for f in _patch_files(pr.test_patch) if _JS_TEST_FILE.match(f)
    ]


def _ts_tests(pr: PullRequest) -> list[str]:
    prefix = _TS_DIR + "/"
    return [
        f[len(prefix):] for f in _patch_files(pr.test_patch) if _TS_TEST_FILE.match(f)
    ]


def _py_block(files: list[str]) -> str:
    return f"""PY_TARGETS=""
for f in {" ".join(files)}; do
    if [ -f "$f" ]; then
        PY_TARGETS="$PY_TARGETS $f"
    fi
done
if [ -n "$PY_TARGETS" ]; then
    python -m pytest -p no:cacheprovider --color=no -rA --continue-on-collection-errors $PY_TARGETS || STATUS=$?
fi
"""


def _js_block(files: list[str]) -> str:
    return f"""JS_TARGETS=""
for f in {" ".join(files)}; do
    if [ -f "{_JS_DIR}/$f" ]; then
        JS_TARGETS="$JS_TARGETS $f"
    fi
done
if [ -n "$JS_TARGETS" ]; then
    cd {_REPO_DIR}/{_JS_DIR}
    export PATH="$(bash /home/node-path.sh package.json):$PATH"
    yarn test --ci --maxWorkers=2 --reporters=default --reporters=/home/jest-name-reporter.cjs --runTestsByPath $JS_TARGETS || STATUS=$?
    cd {_REPO_DIR}
fi
"""


def _ts_block(files: list[str]) -> str:
    return f"""TS_TARGETS=""
for f in {" ".join(files)}; do
    if [ -f "{_TS_DIR}/$f" ]; then
        TS_TARGETS="$TS_TARGETS $f"
    fi
done
for f in $TS_TARGETS; do
    PKG_DIR="{_REPO_DIR}/{_TS_DIR}/$(dirname "$f")"
    while [ "$PKG_DIR" != "{_REPO_DIR}" ] && [ ! -f "$PKG_DIR/package.json" ]; do
        PKG_DIR="$(dirname "$PKG_DIR")"
    done
    if [ ! -f "$PKG_DIR/package.json" ]; then
        continue
    fi
    cd "$PKG_DIR"
    export PATH="$(bash /home/node-path.sh {_REPO_DIR}/{_TS_DIR}/package.json):$PATH"
    REL="{_REPO_DIR}/{_TS_DIR}/$f"
    npx jest --ci --runInBand --reporters=default --reporters=/home/jest-name-reporter.cjs --runTestsByPath "$REL" || STATUS=$?
    cd {_REPO_DIR}
done
"""


def _run_tests(pr: PullRequest) -> str:
    body = "STATUS=0\n"
    py = _py_tests(pr)
    js = _js_tests(pr)
    ts = _ts_tests(pr)
    if py:
        body += _py_block(py)
    if js:
        body += _js_block(js)
    if ts:
        body += _ts_block(ts)
    return body + "exit $STATUS\n"


def _install(pr: PullRequest) -> str:
    steps = []
    gates = []
    if _py_tests(pr):
        steps.append(
            f"""UV_PIN="$(sed -n '/^\\[tool\\.uv\\]/,/^\\[/s/^required-version[[:space:]]*=[[:space:]]*"=*\\([0-9][0-9.]*\\)".*/\\1/p' pyproject.toml | head -1)"
if [ -n "$UV_PIN" ]; then
    python -m pip install "uv==$UV_PIN"
fi
EXCLUDE_NEWER="$(git log -1 --format=%cI HEAD)"
if [ -f uv.lock ]; then
    uv sync --frozen --extra gateway --extra genai --extra mcp || uv sync --frozen
else
    uv venv {_VENV} --python /usr/local/bin/python3
    uv pip install --python {_VENV}/bin/python --exclude-newer "$EXCLUDE_NEWER" -e .
fi
uv pip install --python {_VENV}/bin/python --exclude-newer "$EXCLUDE_NEWER" -r requirements/test-requirements.txt
"""
        )
        gates.append(
            f"""{_VENV}/bin/python -c "import mlflow, pytest; print('DEPS_OK python', mlflow.__version__, pytest.__version__)"
"""
        )
    if _js_tests(pr):
        steps.append(
            f"""cd {_REPO_DIR}/{_JS_DIR}
export PATH="$(bash /home/node-path.sh package.json):$PATH"
yarn install --no-immutable
cd {_REPO_DIR}
"""
        )
        gates.append(
            f"""cd {_REPO_DIR}/{_JS_DIR}
export PATH="$(bash /home/node-path.sh package.json):$PATH"
node -e "require.resolve('jest/package.json'); require.resolve('@craco/craco/package.json'); console.log('DEPS_OK js', process.version)"
cd {_REPO_DIR}
"""
        )
    if _ts_tests(pr):
        steps.append(
            f"""cd {_REPO_DIR}/{_TS_DIR}
export PATH="$(bash /home/node-path.sh package.json):$PATH"
npm ci --ignore-scripts || npm install --ignore-scripts
cd {_REPO_DIR}
"""
        )
        gates.append(
            f"""cd {_REPO_DIR}/{_TS_DIR}
export PATH="$(bash /home/node-path.sh package.json):$PATH"
node -e "require.resolve('jest/package.json'); console.log('DEPS_OK typescript', process.version)"
cd {_REPO_DIR}
"""
        )
    return "\n".join(steps) + "\n" + "".join(gates)


class Mlflow27949To6358ImageBase(Image):
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
    CI=true \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    NODE_OPTIONS=--max-old-space-size=8192

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl build-essential pkg-config make patch \\
        libpq-dev default-libmysqlclient-dev unixodbc-dev sqlite3 \\
    && rm -rf /var/lib/apt/lists/*

COPY --from={_NODE_PRIMARY} /usr/local /opt/node24
COPY --from={_NODE_LEGACY} /usr/local /opt/node22

RUN ln -sfn /opt/node24 /opt/node-default && \\
    /opt/node24/bin/node /opt/node24/bin/npm install -g --force --prefix /opt/node24 "yarn@{_YARN}" && \\
    /opt/node22/bin/node /opt/node22/bin/npm install -g --force --prefix /opt/node22 "yarn@{_YARN}" && \\
    /opt/node24/bin/node --version && /opt/node22/bin/node --version && \\
    /opt/node24/bin/node /opt/node24/bin/yarn --version && /opt/node22/bin/node /opt/node22/bin/yarn --version

RUN python -m pip install --no-cache-dir "uv=={_UV}" && uv --version

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Mlflow27949To6358ImageDefault(Image):
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
        return Mlflow27949To6358ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        run_tests = _run_tests(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

cd {repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
git submodule update --init --recursive
bash /home/check_git_changes.sh

{run_env}
{helpers}
{install}""".format(
                    repo_dir=_REPO_DIR,
                    base_sha=self.pr.base.sha,
                    run_env=_RUN_ENV,
                    helpers=_HELPERS,
                    install=_install(self.pr),
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
{run_tests}""".format(repo_dir=_REPO_DIR, run_env=_RUN_ENV, run_tests=run_tests),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
bash /home/run_tests.sh
""".format(repo_dir=_REPO_DIR, run_env=_RUN_ENV),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
git apply --whitespace=nowarn /home/test.patch

bash /home/run_tests.sh
""".format(repo_dir=_REPO_DIR, run_env=_RUN_ENV),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

bash /home/run_tests.sh
""".format(repo_dir=_REPO_DIR, run_env=_RUN_ENV),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

WORKDIR {_REPO_DIR}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

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
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("mlflow", _INTERVAL)
class MLFLOW_27949_TO_6358(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Mlflow27949To6358ImageDefault(self.pr, self._config)

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
