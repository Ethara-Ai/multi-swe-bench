import base64
import os
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22-bookworm"
_FALLBACK_PNPM = "9.15.5"
_BASE_TAG = "base-6452_to_71"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


def _render(template: str, **values: str) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"__{key.upper()}__", value)
    return rendered


def _serial_mode() -> str:
    return "1" if os.environ.get("MSWEB_SERIAL") == "1" else "0"


def _stage_command(repo: str, runner: str) -> str:
    script = _render(_RUN_TESTS_SH, repo=repo, serial=_serial_mode())
    encoded = base64.b64encode(script.encode()).decode()
    return (
        f"bash -c 'echo {encoded} | base64 -d > /home/run_tests.sh"
        f" && bash {runner}'"
    )


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __FROM_IMAGE__

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
    CURL_CA_BUNDLE=${CA_CERT_PATH}

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
    git ca-certificates curl gnupg build-essential python3 pkg-config libvips-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    postgresql-16 postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__ && \
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \
    cd /home/__REPO__; \
    git checkout --detach "__BASE_SHA__"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test -z "$(git reflog)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
        cd /home/__REPO__ && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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

_POSTGRES_SH = r"""#!/bin/bash
set -u

PGBIN=/usr/lib/postgresql/16/bin
PGDATA=/var/lib/postgresql/testdata
PGLOG=/var/lib/postgresql/postgres.log
ACTION="${1:-start}"

as_postgres() {
    su postgres -c "$1"
}

is_ready() {
    as_postgres "$PGBIN/pg_isready -h 127.0.0.1 -p 5432 -q" >/dev/null 2>&1
}

if [ "$ACTION" = "stop" ]; then
    as_postgres "$PGBIN/pg_ctl -D $PGDATA -m fast -w -t 60 stop" >/dev/null 2>&1 || true
    exit 0
fi

mkdir -p /var/run/postgresql "$PGDATA"
chown -R postgres:postgres /var/run/postgresql "$PGDATA"
chmod 700 "$PGDATA"
: > "$PGLOG"
chown postgres:postgres "$PGLOG"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    if ! as_postgres "$PGBIN/initdb -D $PGDATA -U postgres --auth-local=trust --auth-host=trust --encoding=UTF8 --locale=C" >/dev/null 2>&1; then
        echo "postgres: initdb failed"
        exit 1
    fi
fi

if ! is_ready; then
    rm -f "$PGDATA/postmaster.pid"
    as_postgres "$PGBIN/pg_ctl -D $PGDATA -l $PGLOG -o '-c listen_addresses=127.0.0.1 -p 5432 -c max_connections=200 -c fsync=off -c synchronous_commit=off' -w -t 120 start" >/dev/null 2>&1 || true
fi

ATTEMPT=0
while [ "$ATTEMPT" -lt 30 ]; do
    if is_ready; then
        as_postgres "$PGBIN/psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c \"ALTER USER postgres WITH PASSWORD 'postgres'\"" >/dev/null 2>&1
        if as_postgres "$PGBIN/psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector'" >/dev/null 2>&1; then
            echo "postgres: ready with pgvector"
            exit 0
        fi
        echo "postgres: pgvector extension unavailable"
        exit 1
    fi
    ATTEMPT=$((ATTEMPT + 1))
    sleep 2
done

echo "postgres: server did not become ready"
tail -n 60 "$PGLOG" 2>/dev/null || true
exit 1
"""

_EMIT_RESULTS_JS = r"""const fs = require('fs');
const path = require('path');

const repoDir = process.argv[2];
const resultsDir = process.argv[3];

function toRelative(filePath) {
    if (!filePath) return '';
    let rel = path.relative(repoDir, filePath);
    if (!rel || rel.startsWith('..')) rel = filePath;
    return rel.split(path.sep).join('/');
}

function normalise(text) {
    return String(text === undefined || text === null ? '' : text)
        .replace(/[\r\n\t]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function toStatus(raw) {
    if (raw === 'passed') return 'PASSED';
    if (raw === 'failed') return 'FAILED';
    return 'SKIPPED';
}

let reportFiles = [];
try {
    reportFiles = fs
        .readdirSync(resultsDir)
        .filter(function (name) { return name.endsWith('.json'); })
        .sort()
        .map(function (name) { return path.join(resultsDir, name); });
} catch (err) {
    reportFiles = [];
}

const emitted = [];

for (const reportFile of reportFiles) {
    if (!fs.existsSync(reportFile)) continue;
    let report;
    try {
        report = JSON.parse(fs.readFileSync(reportFile, 'utf8'));
    } catch (err) {
        continue;
    }
    const suites = Array.isArray(report.testResults) ? report.testResults : [];
    for (const suite of suites) {
        const file = toRelative(suite.name || suite.testFilePath || '');
        if (!file) continue;
        const cases = Array.isArray(suite.assertionResults) ? suite.assertionResults : [];
        if (cases.length === 0) {
            if (suite.status && suite.status !== 'passed') {
                emitted.push('TESTCASE FAILED ' + file);
            }
            continue;
        }
        for (const testCase of cases) {
            const parts = [file];
            const ancestors = Array.isArray(testCase.ancestorTitles) ? testCase.ancestorTitles : [];
            for (const ancestor of ancestors) {
                const cleaned = normalise(ancestor);
                if (cleaned) parts.push(cleaned);
            }
            const title = normalise(testCase.title || testCase.fullName || '');
            if (title) parts.push(title);
            if (parts.length < 2) continue;
            emitted.push('TESTCASE ' + toStatus(testCase.status) + ' ' + parts.join(' > '));
        }
    }
}

if (emitted.length > 0) {
    process.stdout.write(emitted.join('\n') + '\n');
}
"""

_RUN_TESTS_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/__REPO__

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=6144"
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
export PUPPETEER_SKIP_DOWNLOAD=1
export KEY_VAULTS_SECRET="$(node -e "console.log(require('crypto').createHash('sha256').update('lobehub-test-vault').digest('base64'))")"
export S3_PUBLIC_DOMAIN="https://example.com"
export APP_URL="https://home.com"

RESULTS_DIR=/home/test-results
rm -rf "$RESULTS_DIR"
mkdir -p "$RESULTS_DIR"

RETRY_ARGS=""
if pnpm exec vitest run --help 2>/dev/null | grep -q -- "--retry"; then
    RETRY_ARGS="--retry=3"
fi

STABILITY_ARGS=""
if [ "__SERIAL__" = "1" ] && pnpm exec vitest run --help 2>/dev/null | grep -q -- "--no-file-parallelism"; then
    STABILITY_ARGS="--no-file-parallelism"
fi

run_suite() {
    SUITE_LABEL="$1"
    SUITE_DIR="$2"
    SUITE_CONFIG="$3"
    SUITE_JSON="$RESULTS_DIR/$SUITE_LABEL.json"
    SUITE_LOG="$RESULTS_DIR/$SUITE_LABEL.log"
    echo "===== suite $SUITE_LABEL start ====="
    if [ -n "$SUITE_CONFIG" ]; then
        (cd "$SUITE_DIR" && pnpm exec vitest run --config "$SUITE_CONFIG" $RETRY_ARGS $STABILITY_ARGS --reporter=json --outputFile="$SUITE_JSON") > "$SUITE_LOG" 2>&1
    else
        (cd "$SUITE_DIR" && pnpm exec vitest run $RETRY_ARGS $STABILITY_ARGS --reporter=json --outputFile="$SUITE_JSON") > "$SUITE_LOG" 2>&1
    fi
    SUITE_RC=$?
    echo "===== suite $SUITE_LABEL exit code $SUITE_RC ====="
    tail -c 20000 "$SUITE_LOG" 2>/dev/null
    echo ""
}

APP_CONFIG=""
for CANDIDATE in vitest.config.ts vitest.config.mts; do
    if [ -f "$CANDIDATE" ]; then
        APP_CONFIG="$CANDIDATE"
        break
    fi
done
if [ -n "$APP_CONFIG" ]; then
    run_suite app /home/__REPO__ "$APP_CONFIG"
fi

SERVER_CONFIG=""
for CANDIDATE in vitest.server.config.ts vitest.config.server.ts; do
    if [ -f "$CANDIDATE" ]; then
        SERVER_CONFIG="$CANDIDATE"
        break
    fi
done
if [ -n "$SERVER_CONFIG" ] && [ -f /home/pg_ready ]; then
    if bash /home/postgres.sh start; then
        (
            export DATABASE_TEST_URL="postgresql://postgres:postgres@127.0.0.1:5432/postgres"
            export DATABASE_DRIVER=node
            export NEXT_PUBLIC_SERVICE_MODE=server
            run_suite server /home/__REPO__ "$SERVER_CONFIG"
        )
        bash /home/postgres.sh stop
    else
        echo "run_tests: postgres unavailable, server suite skipped"
    fi
fi

RUN_PACKAGE_SUITES=0
if [ -d packages ]; then
    if [ -z "$APP_CONFIG" ]; then
        RUN_PACKAGE_SUITES=1
    elif grep -q 'packages/\*\*' "$APP_CONFIG"; then
        RUN_PACKAGE_SUITES=1
    fi
fi

if [ "$RUN_PACKAGE_SUITES" = "1" ]; then
    for PKG_DIR in packages/*/; do
        PKG_NAME="$(basename "$PKG_DIR")"
        PKG_CONFIG=""
        for CANDIDATE in vitest.config.ts vitest.config.mts; do
            if [ -f "$PKG_DIR$CANDIDATE" ]; then
                PKG_CONFIG="$CANDIDATE"
                break
            fi
        done
        PKG_HAS_VITEST_TEST="$(node -e "const s = ((require('./$PKG_DIR/package.json').scripts || {}).test || ''); console.log(s.includes('vitest') ? '1' : '0')" 2>/dev/null || echo 0)"
        if [ -n "$PKG_CONFIG" ] || [ "$PKG_HAS_VITEST_TEST" = "1" ]; then
            run_suite "pkg_$PKG_NAME" "/home/__REPO__/$PKG_DIR" "$PKG_CONFIG"
        fi
    done
fi

echo "===== test results ====="
node /home/emit_results.js /home/__REPO__ "$RESULTS_DIR"
"""

_PREPARE_SH = r"""#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=6144"
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
export PUPPETEER_SKIP_DOWNLOAD=1
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

PNPM_VERSION="$(node -e "const pm = (require('./package.json').packageManager || ''); console.log(pm.startsWith('pnpm@') ? pm.slice(5).split('+')[0] : '__FALLBACK_PNPM__')" 2>/dev/null || echo "__FALLBACK_PNPM__")"
npm install -g "pnpm@${PNPM_VERSION}"

pnpm install --no-frozen-lockfile || true

if bash /home/postgres.sh start; then
    bash /home/postgres.sh stop
    : > /home/pg_ready
    echo "prepare: postgres ready, server suite enabled"
else
    echo "prepare: postgres unavailable, server suite disabled"
fi

node -e "require('./package.json')"
pnpm exec vitest --version
echo DEPS_OK
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

bash /home/run_tests.sh
"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch

bash /home/run_tests.sh
"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

bash /home/run_tests.sh
"""


class LobeHubImageBaseCommon(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def files(self) -> list[File]:
        return []


class LobeHubImageBase(LobeHubImageBaseCommon):
    def dependency(self) -> str:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def dockerfile(self) -> str:
        return _render(
            _BASE_DOCKERFILE,
            from_image=_NODE_IMAGE,
            org=self.pr.org,
            repo=self.pr.repo,
        )


class LobeHubImageDefaultCommon(Image):
    _base_image_class = LobeHubImageBase
    _fallback_pnpm = _FALLBACK_PNPM

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
        return self._base_image_class(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "postgres.sh", _POSTGRES_SH),
            File(".", "emit_results.js", _EMIT_RESULTS_JS),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, repo=repo, serial=_serial_mode())),
            File(
                ".",
                "prepare.sh",
                _render(
                    _PREPARE_SH,
                    repo=repo,
                    base_sha=base_sha,
                    fallback_pnpm=self._fallback_pnpm,
                ),
            ),
            File(".", "run.sh", _render(_RUN_SH, repo=repo)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, repo=repo)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, repo=repo)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        return _render(
            _PR_DOCKERFILE,
            base_image=base.image_full_name(),
            copy_commands=copy_commands,
            repo=self.pr.repo,
            base_sha=self.pr.base.sha,
        )


class LobeHubImageDefault(LobeHubImageDefaultCommon):
    _base_image_class = LobeHubImageBase


class LobeHubInstance(Instance):
    _image_class = LobeHubImageDefault

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return self._image_class(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return _stage_command(self.pr.repo, "/home/run.sh")

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return _stage_command(self.pr.repo, "/home/test-run.sh")

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return _stage_command(self.pr.repo, "/home/fix-run.sh")

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

        for line in clean_log.split("\n"):
            match = _TESTCASE_RE.match(line)
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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


@Instance.register("lobehub", "lobehub_6452_to_71")
class LOBEHUB_6452_TO_71(LobeHubInstance):
    _image_class = LobeHubImageDefault


for _number in range(71, 6474):
    Instance.register("lobehub", str(_number))(LOBEHUB_6452_TO_71)
