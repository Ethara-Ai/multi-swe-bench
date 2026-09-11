"""lobehub 7964-10011 -- ONE base image, TWO test-layout eras.

base-10011_to_7964 (node:22-bookworm) is shared by all ten PRs. The split
below is NOT a toolchain split: nine of ten pin .nvmrc lts/jod (node 22) and
pr-10011 pins lts/krypton (node 24) but installs 4122 packages and builds
cleanly on 22 with no EBADENGINE warning, so one base serves both.

What differs is where the tests live. lobehub became a monorepo between
2025-06 and 2025-08:

    LOBEHUB_8060_TO_7964    pr-7964, pr-8060
        vitest.config.ts + vitest.server.config.ts at the repo root; tests
        under src/; the server suite needs a live postgres.

    LOBEHUB_10011_TO_8985   pr-8985 8985/9087/9198/9457/9477/9534/9693/10011
        vitest.config.mts at the root, whose test.exclude lists
        "**/packages/**"; tests live under src/ AND packages/model-runtime,
        packages/utils. The server suite moved into packages/database.

Era B therefore runs the root app suite plus the package suites named by the
repo's own CI matrix (.github/workflows/test.yml), not a set invented here.
packages/database is absent from that matrix and no era-B PR touches it, so
era B installs no postgres at all -- which is also why its prepare.sh is
shorter than era A's.

Both eras keep the seven-file contract: postgres.sh and emit_results.js are
written by prepare.sh as heredocs, and each era composes its three stage
scripts from ONE shared body constant so run/test/fix cannot drift apart.
"""

from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.typescript.lobehub.lobehub_6452_to_71 import (
    LobeHubImageBaseCommon,
    LobeHubImageDefaultCommon,
    LobeHubInstance,
)

__all__ = [
    "LOBEHUB_8060_TO_7964",
    "LOBEHUB_10011_TO_8985",
    "ImageBase",
]

PRS_ERA_A = [7964, 8060]
PRS_ERA_B = [8985, 9087, 9198, 9457, 9477, 9534, 9693, 10011]
BASE_TAG = "base-10011_to_7964"
NODE_IMAGE = "node:22-bookworm"
PG_MAJOR = "16"
PG_DATA = "/var/lib/postgresql/testdata"

CHECK_GIT_CHANGES = r"""#!/bin/bash
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

# Written by prepare.sh as heredocs rather than COPYd, so the PR image keeps
# to the seven-file contract. Byte-identical to the stock helpers.
POSTGRES_SH = r"""#!/bin/bash
set -u

PGBIN=/usr/lib/postgresql/16/bin
PGDATA=/var/lib/postgresql/testdata
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

PGLOG=/var/lib/postgresql/postgres.log

mkdir -p /var/run/postgresql
chown -R postgres:postgres /var/run/postgresql
: > "$PGLOG"
chown postgres:postgres "$PGLOG"
mkdir -p "$PGDATA"
chown -R postgres:postgres "$PGDATA"
chmod 700 "$PGDATA"

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

READY=0
ATTEMPT=0
while [ "$ATTEMPT" -lt 60 ]; do
    if is_ready; then
        READY=1
        break
    fi
    ATTEMPT=$((ATTEMPT + 1))
    sleep 2
done

if [ "$READY" -ne 1 ]; then
    echo "postgres: server did not become ready"
    tail -n 60 "$PGLOG" 2>/dev/null || true
    exit 1
fi

as_postgres "$PGBIN/psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c \"ALTER USER postgres WITH PASSWORD 'postgres'\"" >/dev/null 2>&1

if ! as_postgres "$PGBIN/psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector'"; then
    echo "postgres: pgvector extension unavailable"
    exit 1
fi

echo "postgres: ready with pgvector"
"""

EMIT_JS = r"""const fs = require('fs');
const path = require('path');

const repoDir = process.argv[2];
const reportFiles = process.argv.slice(3);
const emitted = [];

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

for (const reportFile of reportFiles) {
    if (!reportFile || !fs.existsSync(reportFile)) continue;
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

process.stdout.write(emitted.map((line) => line + '\n').join(''));
"""

PNPM_SETUP = r"""PNPM_VERSION="$(node -e "try { const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log(pm.split('@')[1]); else console.log('10.10.0'); } catch (e) { console.log('10.10.0'); }")"
npm install -g "pnpm@${PNPM_VERSION}"
"""

# ONE graded body per era, interpolated into that era's run/test/fix scripts.
TEST_BODY_ERA_A = r"""REPO_DIR=/home/lobehub
APP_JSON=/home/vitest-app.json
SERVER_JSON=/home/vitest-server.json
APP_LOG=/home/vitest-app.log
SERVER_LOG=/home/vitest-server.log

cd "$REPO_DIR"

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=6144"

rm -f "$APP_JSON" "$SERVER_JSON" "$APP_LOG" "$SERVER_LOG"

HEARTBEAT_PID=""

start_heartbeat() {
    (
        while true; do
            sleep 30
            echo "[heartbeat] $1 still running at $(date -u +%H:%M:%S)"
        done
    ) &
    HEARTBEAT_PID=$!
}

stop_heartbeat() {
    if [ -n "$HEARTBEAT_PID" ]; then
        kill "$HEARTBEAT_PID" >/dev/null 2>&1 || true
        wait "$HEARTBEAT_PID" 2>/dev/null || true
        HEARTBEAT_PID=""
    fi
}

start_heartbeat "app suite"
pnpm exec vitest run --config vitest.config.ts --reporter=json --outputFile="$APP_JSON" > "$APP_LOG" 2>&1 || true
stop_heartbeat

echo "===== app suite output ====="
tail -c 100000 "$APP_LOG" 2>/dev/null || true

if [ -f /home/pg_ready ] && [ -f vitest.server.config.ts ]; then
    bash /home/postgres.sh start || echo "run_tests: postgres unavailable, server suite will report failures"
    (
        export DATABASE_DRIVER=node
        export DATABASE_TEST_URL="postgresql://postgres:postgres@127.0.0.1:5432/postgres"
        export NEXT_PUBLIC_SERVICE_MODE=server
        export KEY_VAULTS_SECRET="$(node -e "console.log(require('crypto').createHash('sha256').update('lobehub-test-vault').digest('base64'))")"
        export S3_PUBLIC_DOMAIN="https://example.com"
        export APP_URL="https://home.com"
        pnpm exec vitest run --config vitest.server.config.ts --reporter=json --outputFile="$SERVER_JSON" > "$SERVER_LOG" 2>&1
    ) &
    SERVER_PID=$!
    start_heartbeat "server suite"
    wait "$SERVER_PID" || true
    stop_heartbeat
    echo "===== server suite output ====="
    tail -c 100000 "$SERVER_LOG" 2>/dev/null || true
fi

echo "===== test results ====="
node /home/emit_results.js "$REPO_DIR" "$APP_JSON" "$SERVER_JSON"
exit 0
"""

TEST_BODY_ERA_B = r"""REPO_DIR=/home/lobehub
APP_JSON=/home/vitest-app.json

cd "$REPO_DIR"

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=6144"

rm -f /home/vitest-*.json /home/vitest-*.log

HEARTBEAT_PID=""

start_heartbeat() {
    (
        while true; do
            sleep 30
            echo "[heartbeat] $1 still running at $(date -u +%H:%M:%S)"
        done
    ) &
    HEARTBEAT_PID=$!
}

stop_heartbeat() {
    if [ -n "$HEARTBEAT_PID" ]; then
        kill "$HEARTBEAT_PID" >/dev/null 2>&1 || true
        wait "$HEARTBEAT_PID" 2>/dev/null || true
        HEARTBEAT_PID=""
    fi
}

REPORTS=""

start_heartbeat "app suite"
pnpm exec vitest run --reporter=json --outputFile="$APP_JSON" > /home/vitest-app.log 2>&1 || true
stop_heartbeat
REPORTS="$APP_JSON"

echo "===== app suite output ====="
tail -c 60000 /home/vitest-app.log 2>/dev/null || true

# Package list is the repo's own CI matrix (.github/workflows/test.yml). The
# root vitest config excludes **/packages/**, so these would otherwise never run
# -- and pr-8985/9087/9198/9457/9477/10011 put their tests here.
CI_PACKAGES="file-loaders prompts model-runtime web-crawler electron-server-ipc utils python-interpreter context-engine agent-runtime model-bank"

for pkg in $CI_PACKAGES; do
    pkg_dir="packages/$pkg"
    [ -d "$pkg_dir" ] || continue
    pkg_json="/home/vitest-$pkg.json"
    pkg_log="/home/vitest-$pkg.log"
    start_heartbeat "package $pkg"
    ( cd "$pkg_dir" && pnpm exec vitest run --reporter=json --outputFile="$pkg_json" ) > "$pkg_log" 2>&1 || true
    stop_heartbeat
    if [ -f "$pkg_json" ]; then
        REPORTS="$REPORTS $pkg_json"
    fi
    echo "===== package $pkg output ====="
    tail -c 20000 "$pkg_log" 2>/dev/null || true
done

echo "===== test results ====="
node /home/emit_results.js "$REPO_DIR" $REPORTS
exit 0
"""


class ImageBase(LobeHubImageBaseCommon):
    def dependency(self) -> Union[str, Image]:
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

{self.global_env}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

WORKDIR /home/

{clone}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class EraImageDefault(LobeHubImageDefaultCommon):
    """Shared image plumbing. Subclasses set WITH_POSTGRES and TEST_BODY."""

    _base_image_class = ImageBase
    WITH_POSTGRES = False
    TEST_BODY = ""

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        nl = chr(10)

        prepare = "#!/bin/bash" + nl + "set -e" + nl * 2
        prepare += "export CI=true" + nl
        prepare += 'export NODE_OPTIONS="--max-old-space-size=6144"' + nl
        prepare += "export NPM_CONFIG_FUND=false" + nl
        prepare += "export NPM_CONFIG_AUDIT=false" + nl * 2

        if self.WITH_POSTGRES:
            prepare += "export PGBIN=/usr/lib/postgresql/" + PG_MAJOR + "/bin" + nl
            prepare += "export PGDATA=" + PG_DATA + nl * 2

        prepare += "apt-get update" + nl
        deps = "gnupg libvips-dev" if self.WITH_POSTGRES else "libvips-dev"
        prepare += "apt-get install -y --no-install-recommends " + deps + nl * 2

        if self.WITH_POSTGRES:
            prepare += (
                "curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc "
                "| gpg --dearmor -o /usr/share/keyrings/pgdg.gpg" + nl
            )
            prepare += (
                'echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] '
                'https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" '
                "> /etc/apt/sources.list.d/pgdg.list" + nl
            )
            prepare += "apt-get update" + nl
            prepare += (
                "apt-get install -y --no-install-recommends postgresql-" + PG_MAJOR
                + " postgresql-" + PG_MAJOR + "-pgvector" + nl
            )
        prepare += "rm -rf /var/lib/apt/lists/*" + nl * 2

        if self.WITH_POSTGRES:
            prepare += "cat > /home/postgres.sh <<'MSWEBENCH_PG_EOF'" + nl
            prepare += POSTGRES_SH.lstrip(nl)
            prepare += "MSWEBENCH_PG_EOF" + nl * 2

        prepare += "cat > /home/emit_results.js <<'MSWEBENCH_JS_EOF'" + nl
        prepare += EMIT_JS.lstrip(nl)
        prepare += "MSWEBENCH_JS_EOF" + nl * 2

        prepare += "cd /home/" + repo + nl
        prepare += "git reset --hard" + nl
        prepare += "git clean -fdx" + nl
        prepare += "bash /home/check_git_changes.sh" + nl
        prepare += "git checkout --detach " + sha + nl
        prepare += "bash /home/check_git_changes.sh" + nl * 2

        prepare += PNPM_SETUP.lstrip(nl) + nl
        prepare += "pnpm install --no-frozen-lockfile" + nl * 2

        if self.WITH_POSTGRES:
            prepare += "if bash /home/postgres.sh start; then" + nl
            prepare += "    bash /home/postgres.sh stop" + nl
            prepare += "    : > /home/pg_ready" + nl
            prepare += '    echo "prepare: postgres ready, server suite enabled"' + nl
            prepare += "else" + nl
            prepare += '    echo "prepare: postgres unavailable, server suite disabled"' + nl
            prepare += "fi" + nl * 2

        prepare += 'node -e "require(\'./package.json\')"' + nl
        prepare += "pnpm exec vitest --version" + nl
        prepare += "echo DEPS_OK" + nl

        def stage(patch_cmd: str) -> str:
            head = "#!/bin/bash" + nl + "set -eo pipefail" + nl * 2
            head += "cd /home/" + repo + nl
            if patch_cmd:
                head += patch_cmd + nl
            return head + nl + self.TEST_BODY.lstrip(nl)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", stage("")),
            File(
                ".",
                "test-run.sh",
                stage("git apply --whitespace=nowarn /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                stage(
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

RUN bash /home/prepare.sh

{self.clear_env}

"""


class ImageDefaultEraA(EraImageDefault):
    WITH_POSTGRES = True
    TEST_BODY = TEST_BODY_ERA_A


class ImageDefaultEraB(EraImageDefault):
    WITH_POSTGRES = False
    TEST_BODY = TEST_BODY_ERA_B


@Instance.register("lobehub", "lobehub_8060_to_7964")
class LOBEHUB_8060_TO_7964(LobeHubInstance):
    _image_class = ImageDefaultEraA

    def dependency(self) -> Optional[Image]:
        return ImageDefaultEraA(self.pr, self._config)


@Instance.register("lobehub", "lobehub_10011_to_8985")
class LOBEHUB_10011_TO_8985(LobeHubInstance):
    _image_class = ImageDefaultEraB

    def dependency(self) -> Optional[Image]:
        return ImageDefaultEraB(self.pr, self._config)


# Deliberately NOT registering bare per-number keys: lobehub_13716_to_6474
# already registers str(n) for 6474..14599, and re-registering would shadow it
# for any other dataset using a bare number as number_interval.

