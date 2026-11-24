import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "superset_46352_to_26404"
_PYTHON_IMAGE = "python:3.11-bookworm"
_NODE_VERSIONS = ("16.20.2", "18.19.1", "20.18.1", "22.22.0", "24.16.0")
_NODE_DEFAULT = "24"
_UV = "0.13.6"
_VENV = "/opt/superset-venv"
_REPO_DIR = "/home/superset"
_FE_DIR = "superset-frontend"
_TEST_CONFIG = "tests.integration_tests.superset_test_config"

_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_PY_TEST_FILE = re.compile(
    r"^tests/(?:[^/]+/)*(?:test_[^/]+|[^/]+_test|[^/]+_tests)\.py$"
)
_JS_TEST_FILE = re.compile(
    rf"^{_FE_DIR}/(?:spec|src|plugins|packages|tools|playwright)/(?:[^/]+/)*"
    r"[^/]+(?:_spec|\.test)\.(?:js|jsx|ts|tsx)$"
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
export PYTHONUNBUFFERED=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export UV_PYTHON=/usr/local/bin/python3
export UV_PYTHON_DOWNLOADS=never
export UV_LINK_MODE=copy
export VIRTUAL_ENV={_VENV}
export PATH={_VENV}/bin:$PATH
export SUPERSET_HOME=/home/superset-home
export SUPERSET_TESTENV=true
export SUPERSET_SECRET_KEY=insecure-test-secret-key-for-multi-swe-bench
export SUPERSET__SQLALCHEMY_DATABASE_URI=postgresql+psycopg2://superset:superset@127.0.0.1:5432/superset
export SUPERSET__SQLALCHEMY_EXAMPLES_URI=postgresql+psycopg2://superset:superset@127.0.0.1:5432/superset
export REDIS_HOST=127.0.0.1
export REDIS_PORT=6379
export REDIS_CELERY_DB=2
export REDIS_RESULTS_DB=3
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=8192
export npm_config_fund=false
export npm_config_audit=false
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

_START_SERVICES = """#!/bin/bash
set -e

service postgresql start
redis-server --daemonize yes --save "" --appendonly no

for i in $(seq 1 90); do
    pg_isready -h 127.0.0.1 -p 5432 > /dev/null 2>&1 && break
    sleep 1
done
for i in $(seq 1 60); do
    redis-cli -h 127.0.0.1 ping > /dev/null 2>&1 && break
    sleep 1
done

pg_isready -h 127.0.0.1 -p 5432
redis-cli -h 127.0.0.1 ping
echo "SERVICES_OK"
"""

_NODE_PATH = """#!/bin/bash
SRC="$1"
MAJOR="$(sed -n 's/^[^0-9]*\\([0-9][0-9]*\\).*/\\1/p' "$SRC" 2>/dev/null | head -1)"
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


_NPM_DROP = """const fs = require("fs");
const path = require("path");

const missing = process.argv[2];
const sections = ["dependencies", "devDependencies", "optionalDependencies"];
const packages = JSON.parse(fs.readFileSync("package-lock.json", "utf8")).packages || {};
const dependents = new Map();
for (const [key, info] of Object.entries(packages)) {
  for (const section of sections) {
    for (const name of Object.keys(info[section] || {})) {
      if (!dependents.has(name)) dependents.set(name, new Set());
      dependents.get(name).add(key);
    }
  }
}
const seen = new Set([missing]);
const queue = [missing];
const drops = [];
while (queue.length) {
  const name = queue.shift();
  for (const key of dependents.get(name) || []) {
    const idx = key.lastIndexOf("node_modules/");
    if (idx === -1) {
      drops.push([key, name]);
      continue;
    }
    const parent = key.slice(idx + "node_modules/".length);
    if (!seen.has(parent)) {
      seen.add(parent);
      queue.push(parent);
    }
  }
}
if (!drops.length) {
  console.error(`no root or workspace dependency leads to ${missing}`);
  process.exit(1);
}
for (const [key, name] of drops) {
  const file = path.join(key, "package.json");
  const json = JSON.parse(fs.readFileSync(file, "utf8"));
  for (const section of sections) {
    if (json[section]) delete json[section][name];
  }
  fs.writeFileSync(file, JSON.stringify(json, null, 2) + "\\n");
  console.log(`${key || "."}:${name}`);
}
"""

_HELPERS = (
    "cat > /home/start_services.sh <<'__SERVICES_EOF__'\n"
    + _START_SERVICES
    + "__SERVICES_EOF__\n"
    +     "cat > /home/node-path.sh <<'__NODEPATH_EOF__'\n"
    + _NODE_PATH
    + "__NODEPATH_EOF__\n"
    +     "cat > /home/jest-name-reporter.cjs <<'__JESTREP_EOF__'\n"
    + _JEST_REPORTER
    + "__JESTREP_EOF__\n"
    +     "cat > /home/npm-drop-missing.cjs <<'__NPMDROP_EOF__'\n"
    + _NPM_DROP
    + "__NPMDROP_EOF__\n"
)


def _patch_files(patch: str) -> list[str]:
    files = []
    for path in _DIFF_FILE.findall(patch or ""):
        if path not in files:
            files.append(path)
    return files


def _py_tests(pr: PullRequest) -> list[str]:
    return [f for f in _patch_files(pr.test_patch) if _PY_TEST_FILE.match(f)]


def _integration_tests(pr: PullRequest) -> list[str]:
    return [f for f in _py_tests(pr) if f.startswith("tests/integration_tests/")]


def _plain_tests(pr: PullRequest) -> list[str]:
    return [f for f in _py_tests(pr) if not f.startswith("tests/integration_tests/")]


def _js_tests(pr: PullRequest) -> list[str]:
    prefix = _FE_DIR + "/"
    return [
        f[len(prefix):] for f in _patch_files(pr.test_patch) if _JS_TEST_FILE.match(f)
    ]


def _pytest_block(files: list[str], label: str, prefix: str) -> str:
    return f"""{label}=""
for f in {" ".join(files)}; do
    if [ -f "$f" ]; then
        {label}="${label} $f"
    fi
done
if [ -n "${label}" ]; then
    {prefix}python -m pytest -p no:cacheprovider --color=no -rA --continue-on-collection-errors ${label} || STATUS=$?
fi
"""


def _js_block(files: list[str]) -> str:
    return f"""JS_TARGETS=""
for f in {" ".join(files)}; do
    if [ -f "{_FE_DIR}/$f" ]; then
        JS_TARGETS="$JS_TARGETS {_REPO_DIR}/{_FE_DIR}/$f"
    fi
done
if [ -n "$JS_TARGETS" ]; then
    cd {_REPO_DIR}/{_FE_DIR}
    export PATH="$(bash /home/node-path.sh {_REPO_DIR}/{_FE_DIR}/.nvmrc):$PATH"
    npx jest --ci --maxWorkers=2 --reporters=default --reporters=/home/jest-name-reporter.cjs --runTestsByPath $JS_TARGETS || STATUS=$?
    cd {_REPO_DIR}
fi
"""


def _run_tests(pr: PullRequest) -> str:
    body = "bash /home/start_services.sh\n\nSTATUS=0\n"
    plain = _plain_tests(pr)
    integration = _integration_tests(pr)
    js = _js_tests(pr)
    if plain:
        body += _pytest_block(plain, "PY_TARGETS", "")
    if integration:
        body += _pytest_block(
            integration, "IT_TARGETS", f"SUPERSET_CONFIG={_TEST_CONFIG} "
        )
    if js:
        body += _js_block(js)
    return body + "exit $STATUS\n"


def _install(pr: PullRequest) -> str:
    steps = ["bash /home/start_services.sh\n"]
    gates = []
    if _py_tests(pr):
        steps.append(
            f"""REQ=requirements/development.txt
if [ -f requirements/testing.txt ]; then
    REQ=requirements/testing.txt
fi
SPEC="$(awk 'tolower($0) ~ /^setuptools[^a-z0-9._-]/ {{gsub(/[[:space:]]/, ""); sub(/[;#].*/, ""); print; exit}}' requirements/*.txt)"
case "$SPEC" in
    *==*|*'<'*) printf '%s\n' "$SPEC" > /home/build-constraints.txt ;;
    *) printf 'setuptools<81\n' > /home/build-constraints.txt ;;
esac
cp /home/build-constraints.txt /home/build-only-constraints.txt
NEED="$( {{ sed -n '/^\\[build-system\\]/,/^\\[/p' pyproject.toml 2>/dev/null || true; }} | {{ grep -o 'setuptools>=[0-9][0-9.]*' || true; }} | head -1 | cut -d'=' -f2)"
PIN="${{SPEC#*==}}"
if [ -n "$NEED" ] && [ "$PIN" != "$SPEC" ] && [ "$(printf '%s\\n%s\\n' "$PIN" "$NEED" | sort -V | tail -1)" != "$PIN" ]; then
    printf 'setuptools>=%s\\n' "$NEED" > /home/build-only-constraints.txt
fi
export PIP_CONSTRAINT=/home/build-constraints.txt
export PIP_BUILD_CONSTRAINT=/home/build-only-constraints.txt
python3 -m venv {_VENV}
{_VENV}/bin/python -m pip install --upgrade pip setuptools wheel
cp "$REQ" requirements/.msb-req.txt
for attempt in $(seq 1 10); do
    if {_VENV}/bin/python -m pip install -r requirements/.msb-req.txt > /tmp/pip-req.log 2>&1; then
        echo "prepare.sh: $REQ installed on attempt $attempt"
        break
    fi
    BAD="$(sed -nE -e "s/.*Failed building wheel for ([A-Za-z0-9._-]+).*/\\1/p" -e "s/.*Failed to build '?([A-Za-z0-9._-]+)'?( when .*)?$/\\1/p" /tmp/pip-req.log | tail -1)"
    if [ -z "$BAD" ]; then
        tail -40 /tmp/pip-req.log
        echo "prepare.sh: $REQ failed for a reason other than an unbuildable package"
        exit 1
    fi
    echo "prepare.sh: dropping unbuildable package $BAD from $REQ"
    sed -E "/^[[:space:]]*${{BAD}}([^A-Za-z0-9._-].*)?$/Id" requirements/.msb-req.txt > /tmp/req.next
    mv /tmp/req.next requirements/.msb-req.txt
done
{_VENV}/bin/python -m pip install -r requirements/.msb-req.txt
rm -f requirements/.msb-req.txt
mkdir -p /home/superset-home
SUPERSET_CONFIG={_TEST_CONFIG} {_VENV}/bin/superset db upgrade || echo "superset db upgrade will be retried by the test fixtures"
SUPERSET_CONFIG={_TEST_CONFIG} {_VENV}/bin/superset init || echo "superset init will be retried by the test fixtures"
"""
        )
        gates.append(
            f"""{_VENV}/bin/python -c "import superset, pytest, flask; print('DEPS_OK python', superset.__version__ if hasattr(superset, '__version__') else 'superset', pytest.__version__, flask.__version__)"
"""
        )
    if _js_tests(pr):
        steps.append(
            f"""cd {_REPO_DIR}/{_FE_DIR}
export PATH="$(bash /home/node-path.sh {_REPO_DIR}/{_FE_DIR}/.nvmrc):$PATH"
node --version
for attempt in $(seq 1 5); do
    if npm ci --no-audit --no-fund > /tmp/npm-ci.log 2>&1 || npm ci --no-audit --no-fund --legacy-peer-deps > /tmp/npm-ci.log 2>&1; then
        echo "prepare.sh: npm ci succeeded on attempt $attempt"
        break
    fi
    MISSING="$(sed -nE 's#.*404 Not Found - GET https://registry[.]npmjs[.]org/(@[^/ ]+(/|%2[fF])[^/ ]+|[^@/ ][^/ ]*)(/-/[^ ]*)? .*#\\1#p' /tmp/npm-ci.log | sed 's#%2[fF]#/#' | tail -1)"
    if [ -z "$MISSING" ]; then
        npm install --no-audit --no-fund --legacy-peer-deps
        break
    fi
    echo "prepare.sh: $MISSING is gone from the npm registry; dropping $(node /home/npm-drop-missing.cjs "$MISSING" | tr '\\n' ' ')"
    npm install --package-lock-only --no-audit --no-fund --legacy-peer-deps
done
cd {_REPO_DIR}
git diff --name-only -- {_FE_DIR} | xargs -r git checkout --
"""
        )
        gates.append(
            f"""cd {_REPO_DIR}/{_FE_DIR}
export PATH="$(bash /home/node-path.sh {_REPO_DIR}/{_FE_DIR}/.nvmrc):$PATH"
node -e "require.resolve('jest/package.json'); console.log('DEPS_OK js', process.version)"
cd {_REPO_DIR}
"""
        )
    return "\n".join(steps) + "\n" + "".join(gates)


class Superset46352To26404ImageBase(Image):
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
    SUPERSET_HOME=/home/superset-home \\
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
        git ca-certificates curl gnupg xz-utils build-essential pkg-config make patch \\
        postgresql postgresql-contrib postgresql-client redis-server \\
        libpq-dev libsasl2-dev libldap2-dev libssl-dev libffi-dev \\
        libxml2-dev libxmlsec1-dev libxmlsec1-openssl \\
        default-libmysqlclient-dev unixodbc-dev freetds-dev libecpg-dev \\
        libjpeg-dev zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/* \\
    && NODE_ARCH="$(dpkg --print-architecture | sed 's/amd64/x64/')" \\
    && for v in {" ".join(_NODE_VERSIONS)}; do \\
        curl -fsSLO "https://nodejs.org/dist/v$v/node-v$v-linux-$NODE_ARCH.tar.xz" && \\
        curl -fsSL "https://nodejs.org/dist/v$v/SHASUMS256.txt" | grep " node-v$v-linux-$NODE_ARCH.tar.xz$" | sha256sum -c - && \\
        mkdir -p "/opt/node${{v%%.*}}" && \\
        tar -xJf "node-v$v-linux-$NODE_ARCH.tar.xz" -C "/opt/node${{v%%.*}}" --strip-components=1 && \\
        rm -f "node-v$v-linux-$NODE_ARCH.tar.xz" && \\
        "/opt/node${{v%%.*}}/bin/node" --version || exit 1; \\
    done \\
    && ln -sfn /opt/node{_NODE_DEFAULT} /opt/node-default \\
    && python -m pip install --no-cache-dir "uv=={_UV}" && uv --version \\
    && echo "local all all trust" > /etc/postgresql/15/main/pg_hba.conf \\
    && echo "host all all 127.0.0.1/32 trust" >> /etc/postgresql/15/main/pg_hba.conf \\
    && echo "host all all ::1/128 trust" >> /etc/postgresql/15/main/pg_hba.conf \\
    && service postgresql start \\
    && su - postgres -c "psql -c \\"CREATE USER superset WITH PASSWORD 'superset' CREATEDB SUPERUSER;\\"" \\
    && su - postgres -c "createdb -O superset superset" \\
    && su - postgres -c "psql -d superset -c 'CREATE SCHEMA IF NOT EXISTS sqllab_test_db AUTHORIZATION superset;'" \\
    && su - postgres -c "psql -d superset -c 'CREATE SCHEMA IF NOT EXISTS admin_database AUTHORIZATION superset;'" \\
    && service postgresql stop \\
    && mkdir -p /home/superset-home

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class Superset46352To26404ImageDefault(Image):
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
        return Superset46352To26404ImageBase(self.pr, self._config)

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


@Instance.register("apache", _INTERVAL)
class SUPERSET_46352_TO_26404(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Superset46352To26404ImageDefault(self.pr, self._config)

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
