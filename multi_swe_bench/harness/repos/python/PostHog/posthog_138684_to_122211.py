import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "posthog_138684_to_122211"
_PYTHON_IMAGE = "python:3.14.7-bookworm"
_NODE_VERSION = "26.13.1"
_PNPM = "11.28.5"
_UV = "0.13.6"
_SQLX = "0.8.3"
_SQLX_SHA256 = {
    "x86_64": "6a9317f3269b723512fff7b1e5211114246519ee533f34de95c2942ec5beeb4e",
    "aarch64": "a19f337703eacb814a0ec5bf8e755471df81867cc60fae1c0964e8ffa707e759",
}
_VENV = "/opt/posthog-venv"
_REPO_DIR = "/home/posthog"

_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_PY_TEST_FILE = re.compile(r"(?:^|/)test_[^/]+\.py$")
_JEST_TEST_FILE = re.compile(
    r"^(?:frontend|products|common|packages)/(?:[^/]+/)*[^/]+\.test\.(?:js|jsx|ts|tsx)$"
)
_WEB_TEST_FILE = re.compile(r"^(?:[^/]+/)*[^/]+\.test\.(?:js|jsx|ts|tsx)$")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_NODE = r"\S+\.py::[^\s\[]+(?:\[[^\]]*\])?"
_VERBOSE = re.compile(
    rf"^(?P<name>{_NODE})\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_SUMMARY = re.compile(
    rf"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<name>{_NODE})(?:\s+-\s.*)?$"
)
_JS = re.compile(
    r"^(?:JEST|VITEST)_RESULT (?P<status>PASSED|FAILED|SKIPPED) (?P<name>\S+::.+)$"
)
_PASS = {"PASSED", "XPASS"}
_FAIL = {"FAILED", "ERROR"}
_SKIP = {"SKIPPED", "XFAIL"}

_RUN_ENV = f"""export CI=true
export TZ=UTC
export PYTHONUNBUFFERED=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export UV_PROJECT_ENVIRONMENT={_VENV}
export VIRTUAL_ENV={_VENV}
export UV_PYTHON=/usr/local/bin/python3
export UV_PYTHON_DOWNLOADS=never
export UV_LINK_MODE=copy
export PATH={_VENV}/bin:/opt/pnpm:/opt/node/bin:$PATH
export DJANGO_SETTINGS_MODULE=posthog.settings
export DEBUG=1
export TEST=1
export SECRET_KEY=insecure-test-secret-key-for-multi-swe-bench
export SKIP_SERVICE_VERSION_REQUIREMENTS=1
export DATABASE_URL=postgres://posthog:posthog@localhost:5432/posthog
export REDIS_URL=redis://localhost:6379/
export CDP_REDIS_HOST=localhost
export SESSION_RECORDING_API_REDIS_HOST=localhost
export COOKIELESS_REDIS_HOST=localhost
export CLICKHOUSE_HOST=localhost
export CLICKHOUSE_LOGS_HOST=localhost
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=
export CLICKHOUSE_SECURE=False
export CLICKHOUSE_VERIFY=False
export CLICKHOUSE_CLUSTER=posthog
export KAFKA_HOSTS=localhost:9092
export OBJECT_STORAGE_ENABLED=False
export PNPM_HOME=/opt/pnpm
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export NODE_OPTIONS=--max-old-space-size=8192
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

if [ -f /home/posthog/docker/clickhouse/config.d/default.xml ]; then
python3 - <<'__CHCONF_EOF__'
import re
src = open("/home/posthog/docker/clickhouse/config.d/default.xml").read()
parts = []
m = re.search(r"<remote_servers>.*?</remote_servers>", src, re.S)
if m:
    parts.append(m.group(0).replace("<remote_servers>", '<remote_servers replace="replace">', 1).replace("<host>clickhouse</host>", "<host>127.0.0.1</host>"))
m = re.search(r"<named_collections>.*?</named_collections>", src, re.S)
if m:
    parts.append(m.group(0))
m = re.search(r"<macros>(.*?)</macros>", src, re.S)
if m:
    parts.append("<macros>" + re.sub(r"\\s*<(shard|replica)>[^<]*</\\1>", "", m.group(1)) + "</macros>")
parts.append("<merge_tree><allow_dimensions_outside_sorting_key>1</allow_dimensions_outside_sorting_key></merge_tree>")
open("/etc/clickhouse-server/config.d/zz-posthog.xml", "w").write("<clickhouse>" + "".join(parts) + "</clickhouse>")
__CHCONF_EOF__
fi

{ grep -q ' db$' /etc/hosts || echo "127.0.0.1 db" >> /etc/hosts; } 2>/dev/null || true

if [ -f /home/posthog/docker/clickhouse/users-dev.xml ]; then
    cp /home/posthog/docker/clickhouse/users-dev.xml /etc/clickhouse-server/users.xml
fi

printf '%s' '<clickhouse><seccomp>disabled</seccomp></clickhouse>' > /etc/clickhouse-server/config.d/zz-seccomp.xml

service postgresql start
redis-server --daemonize yes --save "" --appendonly no

mkdir -p /var/lib/clickhouse /var/log/clickhouse-server /run/clickhouse-server
chown -R clickhouse:clickhouse /var/lib/clickhouse /var/log/clickhouse-server /run/clickhouse-server
runuser -u clickhouse -- clickhouse-server --daemon --config-file=/etc/clickhouse-server/config.xml \\
    --pid-file=/run/clickhouse-server/clickhouse-server.pid

for i in $(seq 1 90); do
    pg_isready -h 127.0.0.1 -p 5432 > /dev/null 2>&1 && break
    sleep 1
done
for i in $(seq 1 60); do
    redis-cli -h 127.0.0.1 ping > /dev/null 2>&1 && break
    sleep 1
done
CH_PID=/run/clickhouse-server/clickhouse-server.pid
for i in $(seq 1 600); do
    clickhouse-client --host 127.0.0.1 --query "SELECT 1" > /dev/null 2>&1 && break
    if [ $((i % 30)) -eq 0 ] && ! { [ -f "$CH_PID" ] && kill -0 "$(cat "$CH_PID")" 2>/dev/null; }; then
        tail -n 20 /var/log/clickhouse-server/clickhouse-server.err.log 2>/dev/null || true
        runuser -u clickhouse -- clickhouse-server --daemon --config-file=/etc/clickhouse-server/config.xml \\
            --pid-file="$CH_PID" || true
    fi
    sleep 1
done

pg_isready -h 127.0.0.1 -p 5432
redis-cli -h 127.0.0.1 ping
clickhouse-client --host 127.0.0.1 --query "SELECT 1"
echo "SERVICES_OK"
"""

_CLICKHOUSE_CONFIG = """<clickhouse>
    <listen_host>127.0.0.1</listen_host>
    <logger>
        <level>warning</level>
        <console>0</console>
    </logger>
    <max_server_memory_usage_to_ram_ratio>0.6</max_server_memory_usage_to_ram_ratio>
    <mark_cache_size>268435456</mark_cache_size>
    <macros>
        <shard>1</shard>
        <replica>replica-1</replica>
        <cluster>posthog</cluster>
    </macros>
    <keeper_server>
        <tcp_port>9181</tcp_port>
        <server_id>1</server_id>
        <log_storage_path>/var/lib/clickhouse/coordination/log</log_storage_path>
        <snapshot_storage_path>/var/lib/clickhouse/coordination/snapshots</snapshot_storage_path>
        <coordination_settings>
            <operation_timeout_ms>10000</operation_timeout_ms>
            <session_timeout_ms>30000</session_timeout_ms>
            <raft_logs_level>warning</raft_logs_level>
        </coordination_settings>
        <raft_configuration>
            <server>
                <id>1</id>
                <hostname>127.0.0.1</hostname>
                <port>9234</port>
            </server>
        </raft_configuration>
    </keeper_server>
    <zookeeper>
        <node>
            <host>127.0.0.1</host>
            <port>9181</port>
        </node>
    </zookeeper>
    <remote_servers>
        <posthog>
            <shard>
                <internal_replication>true</internal_replication>
                <replica><host>127.0.0.1</host><port>9000</port></replica>
            </shard>
        </posthog>
        <posthog_migrations>
            <shard>
                <internal_replication>true</internal_replication>
                <replica><host>127.0.0.1</host><port>9000</port></replica>
            </shard>
        </posthog_migrations>
        <posthog_single_shard>
            <shard>
                <internal_replication>true</internal_replication>
                <replica><host>127.0.0.1</host><port>9000</port></replica>
            </shard>
        </posthog_single_shard>
        <ai_events>
            <shard>
                <internal_replication>true</internal_replication>
                <replica><host>127.0.0.1</host><port>9000</port></replica>
            </shard>
        </ai_events>
        <aux>
            <shard>
                <internal_replication>true</internal_replication>
                <replica><host>127.0.0.1</host><port>9000</port></replica>
            </shard>
        </aux>
        <ops>
            <shard>
                <internal_replication>true</internal_replication>
                <replica><host>127.0.0.1</host><port>9000</port></replica>
            </shard>
        </ops>
        <sessions>
            <shard>
                <internal_replication>true</internal_replication>
                <replica><host>127.0.0.1</host><port>9000</port></replica>
            </shard>
        </sessions>
    </remote_servers>
</clickhouse>
"""

_CLICKHOUSE_XML = " ".join(_CLICKHOUSE_CONFIG.split())

_JEST_REPORTER = """const fs = require("fs");
const path = require("path");

function writeOut(text) {
  const buf = Buffer.from(text);
  let off = 0;
  while (off < buf.length) {
    try {
      off += fs.writeSync(1, buf, off);
    } catch (e) {
      if (e.code !== "EAGAIN") {
        throw e;
      }
    }
  }
}

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
      writeOut(`JEST_RESULT ${status} ${file}::${name}\\n`);
    }
  }
}

module.exports = NameReporter;
"""

_VITEST_REPORTER = """import fs from "node:fs";
import path from "node:path";

function writeOut(text) {
  const buf = Buffer.from(text);
  let off = 0;
  while (off < buf.length) {
    try {
      off += fs.writeSync(1, buf, off);
    } catch (e) {
      if (e.code !== "EAGAIN") {
        throw e;
      }
    }
  }
}

function statusOf(task) {
  const state = task.result && task.result.state;
  if (state === "pass") {
    return "PASSED";
  }
  if (state === "fail") {
    return "FAILED";
  }
  return "SKIPPED";
}

function walk(task, trail, file, out) {
  if (task.type === "suite" && Array.isArray(task.tasks)) {
    for (const child of task.tasks) {
      walk(child, trail.concat([task.name]), file, out);
    }
    return;
  }
  const name = trail.concat([task.name]).filter(Boolean).join(" > ").replace(/\\s+/g, " ").trim();
  out.push(`VITEST_RESULT ${statusOf(task)} ${file}::${name}`);
}

function emit(out) {
  if (out.length > 0) {
    writeOut(out.join("\\n") + "\\n");
  }
}

export default class NameReporter {
  onTestRunEnd(testModules) {
    if (this.done) {
      return;
    }
    this.done = true;
    const out = [];
    for (const m of testModules || []) {
      const file = path.relative(process.cwd(), m.moduleId).split(path.sep).join("/");
      for (const task of (m.task && m.task.tasks) || []) {
        walk(task, [], file, out);
      }
    }
    emit(out);
  }

  onFinished(files) {
    if (this.done) {
      return;
    }
    this.done = true;
    const out = [];
    for (const f of files || []) {
      const file = path.relative(process.cwd(), f.filepath).split(path.sep).join("/");
      for (const task of f.tasks || []) {
        walk(task, [], file, out);
      }
    }
    emit(out);
  }
}
"""


_HELPERS = (
    "cat > /home/start_services.sh <<'__SERVICES_EOF__'\n"
    + _START_SERVICES
    + "__SERVICES_EOF__\n"
    +     "cat > /home/jest-name-reporter.cjs <<'__JESTREP_EOF__'\n"
    + _JEST_REPORTER
    + "__JESTREP_EOF__\n"
    +     "cat > /home/vitest-name-reporter.mjs <<'__VITESTREP_EOF__'\n"
    + _VITEST_REPORTER
    + "__VITESTREP_EOF__\n"
)


def _patch_files(patch: str) -> list[str]:
    files = []
    for path in _DIFF_FILE.findall(patch or ""):
        if path not in files:
            files.append(path)
    return files


def _py_tests(pr: PullRequest) -> list[str]:
    return [f for f in _patch_files(pr.test_patch) if _PY_TEST_FILE.search(f)]


def _jest_tests(pr: PullRequest) -> list[str]:
    return [f for f in _patch_files(pr.test_patch) if _JEST_TEST_FILE.match(f)]


def _vitest_tests(pr: PullRequest) -> list[str]:
    return [
        f
        for f in _patch_files(pr.test_patch)
        if _WEB_TEST_FILE.match(f) and not _JEST_TEST_FILE.match(f)
    ]


def _py_block(files: list[str]) -> str:
    return f"""PY_TARGETS=""
for f in {" ".join(files)}; do
    if [ -f "$f" ]; then
        PY_TARGETS="$PY_TARGETS $f"
    fi
done
if [ -n "$PY_TARGETS" ]; then
    python -m pytest -p no:cacheprovider --color=no -rA --reuse-db --continue-on-collection-errors $PY_TARGETS || STATUS=$?
fi
"""


def _jest_block(files: list[str]) -> str:
    return f"""JS_TARGETS=""
for f in {" ".join(files)}; do
    if [ -f "{_REPO_DIR}/$f" ]; then
        JS_TARGETS="$JS_TARGETS {_REPO_DIR}/$f"
    fi
done
if [ -n "$JS_TARGETS" ]; then
    pnpm --dir {_REPO_DIR}/frontend run build:products
    pnpm --dir {_REPO_DIR}/frontend exec jest --ci --forceExit --maxWorkers=2 --reporters=default --reporters=/home/jest-name-reporter.cjs --runTestsByPath $JS_TARGETS || STATUS=$?
fi
"""


def _vitest_block(files: list[str]) -> str:
    return f"""for f in {" ".join(files)}; do
    if [ ! -f "{_REPO_DIR}/$f" ]; then
        continue
    fi
    WS="{_REPO_DIR}/$(dirname "$f")"
    while [ "$WS" != "{_REPO_DIR}" ] && [ ! -f "$WS/package.json" ]; do
        WS="$(dirname "$WS")"
    done
    if [ ! -f "$WS/package.json" ]; then
        continue
    fi
    pnpm --dir "$WS" exec vitest run --reporter=default --reporter=/home/vitest-name-reporter.mjs "{_REPO_DIR}/$f" || STATUS=$?
done
"""


def _run_tests(pr: PullRequest) -> str:
    body = "bash /home/start_services.sh\n\nSTATUS=0\n"
    py = _py_tests(pr)
    jest = _jest_tests(pr)
    vitest = _vitest_tests(pr)
    if py:
        body += _py_block(py)
    if jest:
        body += _jest_block(jest)
    if vitest:
        body += _vitest_block(vitest)
    return body + "exit $STATUS\n"


def _install(pr: PullRequest) -> str:
    steps = ["bash /home/start_services.sh\n"]
    gates = []
    if _py_tests(pr):
        steps.append(
            f"""SQLX_ARCH="$(uname -m)"
case "$SQLX_ARCH" in
    x86_64) SQLX_SHA256="{_SQLX_SHA256['x86_64']}" ;;
    aarch64) SQLX_SHA256="{_SQLX_SHA256['aarch64']}" ;;
    *) echo "unsupported arch $SQLX_ARCH"; exit 1 ;;
esac
curl -fsSL -o /tmp/sqlx-cli.tar.gz "https://github.com/cargo-bins/cargo-quickinstall/releases/download/sqlx-cli-{_SQLX}/sqlx-cli-{_SQLX}-$SQLX_ARCH-unknown-linux-musl.tar.gz"
echo "$SQLX_SHA256  /tmp/sqlx-cli.tar.gz" | sha256sum -c -
tar -xzf /tmp/sqlx-cli.tar.gz -C /usr/local/bin sqlx cargo-sqlx
rm -f /tmp/sqlx-cli.tar.gz
uv sync --frozen --dev
DATABASE_URL=postgres://posthog:posthog@localhost:5432/test_posthog python manage.py migrate --noinput || echo "test database schema will be built on first run"
"""
        )
        gates.append(
            f"""{_VENV}/bin/python -c "import django, pytest, posthog; print('DEPS_OK python', django.get_version(), pytest.__version__)"
sqlx --version
"""
        )
    if _jest_tests(pr):
        steps.append(
            """pnpm install --frozen-lockfile --filter "./frontend..."
pnpm --dir /home/posthog/frontend run build:products
"""
        )
        gates.append(
            f"""pnpm --dir {_REPO_DIR}/frontend exec node -e "require.resolve('jest/package.json'); console.log('DEPS_OK jest', process.version)"
"""
        )
    if _vitest_tests(pr):
        steps.append(
            f"""VITEST_FILTERS=""
VITEST_LAST=""
for f in {" ".join(_vitest_tests(pr))}; do
    WS="{_REPO_DIR}/$(dirname "$f")"
    while [ "$WS" != "{_REPO_DIR}" ] && [ ! -f "$WS/package.json" ]; do
        WS="$(dirname "$WS")"
    done
    if [ ! -f "$WS/package.json" ]; then
        continue
    fi
    VITEST_FILTERS="$VITEST_FILTERS --filter ./$(realpath --relative-to={_REPO_DIR} "$WS")..."
    VITEST_LAST="$WS"
done
pnpm install --frozen-lockfile $VITEST_FILTERS
echo "$VITEST_LAST" > /home/vitest-workspace
"""
        )
        gates.append(
            """pnpm --dir "$(cat /home/vitest-workspace)" exec node -e "require.resolve('vitest/package.json'); console.log('DEPS_OK vitest', process.version)"
"""
        )
    return "\n".join(steps) + "\n" + "".join(gates)


class Posthog138684To122211ImageBase(Image):
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
    PNPM_HOME=/opt/pnpm \\
    NODE_OPTIONS=--max-old-space-size=8192 \\
    PATH=/opt/pnpm:/opt/node/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

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
        git ca-certificates curl gnupg xz-utils lsb-release build-essential pkg-config make patch \\
        postgresql postgresql-contrib postgresql-client redis-server \\
        libpq-dev libxml2-dev libxmlsec1-dev libxmlsec1-openssl libsasl2-dev \\
        libffi-dev libssl-dev libjpeg-dev zlib1g-dev libbrotli-dev \\
    && curl -fsSL "https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key" | gpg --dearmor > /usr/share/keyrings/clickhouse-keyring.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg arch=$(dpkg --print-architecture)] https://packages.clickhouse.com/deb stable main" > /etc/apt/sources.list.d/clickhouse.list \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends clickhouse-server clickhouse-client \\
    && rm -rf /var/lib/apt/lists/* \\
    && NODE_ARCH="$(dpkg --print-architecture | sed 's/amd64/x64/')" \\
    && curl -fsSLO "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-$NODE_ARCH.tar.xz" \\
    && curl -fsSL "https://nodejs.org/dist/v{_NODE_VERSION}/SHASUMS256.txt" | grep " node-v{_NODE_VERSION}-linux-$NODE_ARCH.tar.xz$" | sha256sum -c - \\
    && mkdir -p /opt/node \\
    && tar -xJf "node-v{_NODE_VERSION}-linux-$NODE_ARCH.tar.xz" -C /opt/node --strip-components=1 \\
    && rm -f "node-v{_NODE_VERSION}-linux-$NODE_ARCH.tar.xz" \\
    && mkdir -p /opt/pnpm \\
    && npm install -g "pnpm@{_PNPM}" \\
    && node --version && pnpm --version \\
    && python -m pip install --no-cache-dir "uv=={_UV}" && uv --version \\
    && echo "local all all trust" > /etc/postgresql/15/main/pg_hba.conf \\
    && echo "host all all 127.0.0.1/32 trust" >> /etc/postgresql/15/main/pg_hba.conf \\
    && echo "host all all ::1/128 trust" >> /etc/postgresql/15/main/pg_hba.conf \\
    && service postgresql start \\
    && su - postgres -c "psql -c \\"CREATE USER posthog WITH PASSWORD 'posthog' CREATEDB SUPERUSER;\\"" \\
    && su - postgres -c "createdb -O posthog posthog" \\
    && su - postgres -c "createdb -O posthog test_posthog" \\
    && su - postgres -c "createdb -O posthog test_dagster" \\
    && service postgresql stop \\
    && printf '%s' '{_CLICKHOUSE_XML}' > /etc/clickhouse-server/config.d/multi-swe-bench.xml \\
    && mkdir -p /var/lib/clickhouse /var/log/clickhouse-server /run/clickhouse-server \\
    && chown -R clickhouse:clickhouse /var/lib/clickhouse /var/log/clickhouse-server /run/clickhouse-server /etc/clickhouse-server/config.d/multi-swe-bench.xml

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class Posthog138684To122211ImageDefault(Image):
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
        return Posthog138684To122211ImageBase(self.pr, self._config)

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
git apply --whitespace=nowarn /home/test.patch || git apply --3way --whitespace=nowarn /home/test.patch

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
for p in /home/test.patch /home/fix.patch; do
    git apply --whitespace=nowarn "$p" || git apply --3way --whitespace=nowarn "$p"
done

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


@Instance.register("PostHog", _INTERVAL)
class POSTHOG_138684_TO_122211(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Posthog138684To122211ImageDefault(self.pr, self._config)

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
            match = _JS.match(line) or _VERBOSE.match(line) or _SUMMARY.match(line)
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
