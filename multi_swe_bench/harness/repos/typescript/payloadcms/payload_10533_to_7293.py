import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "payload_10533_to_7293"
_BASE_TAG = "base-7293_to_10533"
_LO, _HI = 7293, 10533

_GIT_APPLY_EXCLUDES = (
    "--exclude=*.jpg --exclude=*.jpeg --exclude=*.png --exclude=*.gif "
    "--exclude=*.webp --exclude=*.avif --exclude=*.ico --exclude=*.bmp "
    "--exclude=*.mp4 --exclude=*.webm --exclude=*.mp3 --exclude=*.wav "
    "--exclude=*.woff --exclude=*.woff2 --exclude=*.ttf --exclude=*.eot "
    "--exclude=*.otf --exclude=*.pdf --exclude=pnpm-lock.yaml"
)

_TOOLCHAIN_ENV = (
    "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright",
    "MONGOMS_SYSTEM_BINARY=/usr/bin/mongod",
    "MONGOMS_SYSTEM_BINARY_VERSION_CHECK=false",
    "MONGOMS_RUNTIME_DOWNLOAD=false",
    "MONGOMS_DISABLE_POSTINSTALL=1",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT=0",
    'NODE_OPTIONS="--max-old-space-size=4096 --no-experimental-strip-types"',
)


def _tidy(dockerfile: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", dockerfile).rstrip("\n") + "\n"


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

__PROXY_ARGS__

__ENV_BLOCK__

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

__CERT_SYMLINKS__

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl wget gnupg python3 make g++ sudo lsb-release \
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

RUN git config --global --add safe.directory '*'

__CLEAR_ENV__

RUN git clone "${REPO_URL}" /home/__REPO__ && \
    cd /home/__REPO__ && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout __BASE_SHA__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
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
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
        git submodule foreach --recursive ' \
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

__CLEAR_ENV__
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


_SELECT_TESTS = r"""
CHANGED=$(cat /home/test.patch /home/fix.patch 2>/dev/null \
  | sed -n 's|^diff --git a/\(.*\) b/.*$|\1|p' | sort -u)

printf '%s\n' "$CHANGED" \
  | { grep -E '^test/.*e2e\.spec\.tsx?$' || true; } | sort -u > /home/e2e_specs.txt

printf '%s\n' "$CHANGED" \
  | { grep -E '^test/.*int\.spec\.tsx?$' || true; } | sort -u > /home/int_specs.txt

echo "=== graded e2e specs ==="
cat /home/e2e_specs.txt
echo "=== graded int specs ==="
cat /home/int_specs.txt

if [ ! -s /home/e2e_specs.txt ] && [ ! -s /home/int_specs.txt ]; then
    echo "prepare: no graded spec selected from this PR's patches" >&2
    exit 1
fi
"""


_PREPARE_SH = (
    r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

apt-get update
apt-get install -y --no-install-recommends \
    procps psmisc \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libx11-xcb1 \
    libxshmfence1 fonts-liberation fonts-noto-color-emoji

wget -qO - https://pgp.mongodb.com/server-6.0.asc \
    | gpg --dearmor -o /usr/share/keyrings/mongodb-server-6.0.gpg
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-6.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/6.0 multiverse" \
    > /etc/apt/sources.list.d/mongodb-org-6.0.list
apt-get update
apt-get install -y --no-install-recommends mongodb-org-server
rm -rf /var/lib/apt/lists/*
test -x /usr/bin/mongod
mongod --version

PM=$(node -p "require('./package.json').packageManager || ''")
if [ -n "$PM" ]; then
    corepack prepare "$PM" --activate
else
    PNPM_VER=$(node -p "(((require('./package.json').engines)||{}).pnpm||'8.15.7').replace(/[^0-9.]/g,'')")
    corepack prepare "pnpm@${PNPM_VER}" --activate
fi
node --version
pnpm --version


DRIZZLE_PINNED=$(grep -l '"drizzle-kit": "0\.20\.14-1f2c838"' packages/*/package.json 2>/dev/null || true)
if [ -n "$DRIZZLE_PINNED" ]; then
    echo "prepare: repointing the unpublished drizzle-kit pin in: $DRIZZLE_PINNED"
    sed -i 's/"drizzle-kit": "0\.20\.14-1f2c838"/"drizzle-kit": "0.20.17"/' $DRIZZLE_PINNED
fi

installed=0
for attempt in 1 2 3 4 5; do
    if pnpm install --no-frozen-lockfile; then
        installed=1
        break
    fi
    echo "prepare: pnpm install attempt ${attempt} failed; retrying in 20s" >&2
    sleep 20
done
test "$installed" -eq 1

test -x node_modules/.bin/jest
test -x node_modules/.bin/playwright

pnpm exec playwright install --with-deps chromium || pnpm exec playwright install chromium
ls -d /ms-playwright/chromium-* > /dev/null

pnpm run build

if [ -n "$DRIZZLE_PINNED" ]; then
    git checkout -- $DRIZZLE_PINNED
    git checkout -- pnpm-lock.yaml 2>/dev/null || true
fi

bash /home/check_git_changes.sh
echo "DEPS_OK"
"""
    + _SELECT_TESTS
)


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export NODE_ENV=test
export DISABLE_LOGGING=true
export PAYLOAD_DROP_DATABASE=true
export NODE_NO_WARNINGS=1

cd /home/__REPO__
"""


_E2E_SUPPORT = r"""
DEV_LOG=/home/dev-server.log
DEV_PID=""

E2E_PORT=3000
E2E_URL="http://127.0.0.1:${E2E_PORT}"

e2e_mode() {
    if grep -q 'createServer' test/helpers/initPayloadE2ENoConfig.ts 2>/dev/null; then
        echo in-process
    else
        echo external
    fi
}

MONGO_LOG=/home/mongo-server.log
MONGO_URI_FILE=/home/mongo-uri.txt
MONGO_BOOT=/home/__REPO__/node_modules/.msb-start-mongo.mjs
MONGO_PID=""

start_mongo() {
    if [ -n "${MONGODB_MEMORY_SERVER_URI:-}" ]; then
        return 0
    fi

    cat > "$MONGO_BOOT" <<'MJS'
import { writeFileSync } from 'node:fs'
import { MongoMemoryReplSet } from 'mongodb-memory-server'

const rs = await MongoMemoryReplSet.create({ replSet: { count: 1, dbName: 'payloadmemory' } })

const base = rs.getUri()
const uri = base.includes('?') ? `${base}&retryWrites=true` : `${base}?retryWrites=true`
writeFileSync(process.env.MSB_MONGO_URI_FILE, uri)
console.log(`MONGO_READY ${uri}`)

const shutdown = async () => {
  try {
    await rs.stop()
  } catch {}
  process.exit(0)
}
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
setInterval(() => {}, 1 << 30)
MJS

    rm -f "$MONGO_URI_FILE"
    echo "=== starting mongodb-memory-server ==="
    (
        cd /home/__REPO__ || exit 1
        exec env \
            MSB_MONGO_URI_FILE="$MONGO_URI_FILE" \
            NODE_OPTIONS="--no-deprecation --no-experimental-strip-types" \
            node "$MONGO_BOOT"
    ) > "$MONGO_LOG" 2>&1 &
    MONGO_PID=$!

    for attempt in $(seq 1 90); do
        if [ -s "$MONGO_URI_FILE" ]; then
            MONGODB_MEMORY_SERVER_URI=$(cat "$MONGO_URI_FILE")
            export MONGODB_MEMORY_SERVER_URI
            echo "=== mongo ready: ${MONGODB_MEMORY_SERVER_URI} ==="
            return 0
        fi
        sleep 2
    done

    echo "=== mongodb-memory-server never came up; ${MONGO_LOG} ===" >&2
    cat "$MONGO_LOG" >&2 || true
    return 1
}

stop_mongo() {
    if [ -n "$MONGO_PID" ]; then
        kill -TERM "$MONGO_PID" >/dev/null 2>&1 || true
    fi
    MONGO_PID=""
    return 0
}

_kill_stragglers() {
    pkill -f 'test/dev\.[tj]s' >/dev/null 2>&1 || true
    pkill -f 'next-server' >/dev/null 2>&1 || true
    pkill -f 'next-router-worker' >/dev/null 2>&1 || true
    pkill -f 'next-render-worker' >/dev/null 2>&1 || true
    return 0
}

stop_dev_server() {
    if [ -n "$DEV_PID" ]; then
        echo "=== stopping dev server (pid ${DEV_PID}) ==="
        kill -TERM "$DEV_PID" >/dev/null 2>&1 || true
        sleep 3
        kill -KILL "$DEV_PID" >/dev/null 2>&1 || true
    fi
    _kill_stragglers
    DEV_PID=""
    sleep 3
    return 0
}

_server_answers() {
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "${E2E_URL}/api/access" 2>/dev/null || echo 000)
    case "$code" in
        2*|401|403) return 0 ;;
        *) return 1 ;;
    esac
}

_dev_processes_alive() {
    if [ -n "$DEV_PID" ] && kill -0 "$DEV_PID" 2>/dev/null; then
        return 0
    fi
    pgrep -f 'test/dev\.[tj]s|next-server|next-router-worker' >/dev/null 2>&1
}

start_dev_server() {
    suite="$1"

    stop_dev_server
    : > "$DEV_LOG"

    if [ ! -d "test/${suite}" ]; then
        echo "=== dev server: test suite folder test/${suite} does not exist ===" >&2
        return 1
    fi

    if [ ! -f test/dev.ts ]; then
        echo "=== dev server: test/dev.ts does not exist at this revision ===" >&2
        return 1
    fi

    echo "=== starting Next dev server for suite '${suite}' ==="
    (
        cd /home/__REPO__ || exit 1
        exec env \
            NODE_ENV=test \
            PORT="${E2E_PORT}" \
            DISABLE_LOGGING=true \
            DISABLE_PAYLOAD_HMR=true \
            PAYLOAD_DROP_DATABASE=true \
            NODE_NO_WARNINGS=1 \
            NODE_OPTIONS="--no-deprecation --no-experimental-strip-types --max-old-space-size=4096" \
            PATH="/home/__REPO__/node_modules/.bin:${PATH}" \
            tsx ./test/dev.ts "${suite}"
    ) >> "$DEV_LOG" 2>&1 &
    DEV_PID=$!

    echo "=== waiting for ${E2E_URL} to answer (pid ${DEV_PID}) ==="
    ready=0
    dead=0
    for attempt in $(seq 1 90); do
        if _server_answers; then
            ready=1
            echo "=== dev server answered on probe ${attempt} ==="
            break
        fi
        if _dev_processes_alive; then
            dead=0
        else
            dead=$((dead + 1))
            if [ "$dead" -ge 2 ]; then
                echo "=== no dev server process is alive any more ===" >&2
                break
            fi
        fi
        sleep 5
    done

    if [ "$ready" -ne 1 ]; then
        echo "=== dev server never answered; tail of ${DEV_LOG} ===" >&2
        tail -n 120 "$DEV_LOG" >&2 || true
        stop_dev_server
        return 1
    fi

    echo "=== warming the admin panel ==="
    curl -s -o /dev/null --max-time 900 "${E2E_URL}/admin" || true
    return 0
}

run_playwright_spec() {
    spec="$1"
    echo "=== Running Playwright spec: $spec ==="
    CI=1 NODE_NO_WARNINGS=1 \
    NODE_OPTIONS="--no-deprecation --no-experimental-strip-types" \
        pnpm exec playwright test "$spec" \
            -c test/playwright.config.ts \
            --reporter=list \
            --workers=1 \
            --retries=1 < /dev/null || return $?
    return 0
}
"""


_EXEC_TESTS = r"""
echo "=== graded e2e specs: $(tr '\n' ' ' < /home/e2e_specs.txt) ==="
echo "=== graded int specs: $(tr '\n' ' ' < /home/int_specs.txt) ==="

if grep -q -- '--experimental-vm-modules' package.json; then
    JEST_NODE_OPTIONS="--experimental-vm-modules --no-deprecation --no-experimental-strip-types --max-old-space-size=4096"
else
    JEST_NODE_OPTIONS="--no-deprecation --no-experimental-strip-types --max-old-space-size=4096"
fi

STATUS=0

if [ -s /home/int_specs.txt ]; then
    PATTERN=""
    while IFS= read -r spec || [ -n "$spec" ]; do
        [ -n "$spec" ] || continue
        escaped=$(printf '%s' "$spec" | sed 's/[][().*+?^$\\|{}]/\\&/g')
        PATTERN="${PATTERN}${PATTERN:+|}/${escaped}\$"
    done < /home/int_specs.txt
    echo "=== Running Jest (integration) ==="
    NODE_OPTIONS="$JEST_NODE_OPTIONS" pnpm exec jest \
        --config=test/jest.config.js \
        --forceExit \
        --detectOpenHandles \
        --runInBand \
        --reporters=default \
        --testPathPattern="$PATTERN" || STATUS=$?
fi

if [ -s /home/e2e_specs.txt ]; then
    E2E_MODE=$(e2e_mode)
    echo "=== e2e mode: ${E2E_MODE} ==="

    if [ "$E2E_MODE" = "external" ] && ! start_mongo; then
        echo "=== no database, so the e2e specs cannot run ===" >&2
        STATUS=1
        E2E_MODE=abort
    fi

    SUITES=$(sed -n 's|^test/\([^/][^/]*\)/.*$|\1|p' /home/e2e_specs.txt | sort -u)

    for suite in $SUITES; do
        SPECS=""
        while IFS= read -r spec || [ -n "$spec" ]; do
            [ -n "$spec" ] || continue
            case "$spec" in
                test/"${suite}"/*) ;;
                *) continue ;;
            esac
            if [ ! -f "$spec" ]; then
                echo "=== E2E spec absent at this revision: $spec ==="
                continue
            fi
            SPECS="${SPECS}${SPECS:+ }${spec}"
        done < /home/e2e_specs.txt

        [ -n "$SPECS" ] || continue
        [ "$E2E_MODE" != "abort" ] || continue

        if [ "$E2E_MODE" = "external" ]; then
            if ! start_dev_server "$suite"; then
                echo "=== suite '${suite}': first dev server attempt failed, retrying once ===" >&2
                if ! start_dev_server "$suite"; then
                    echo "=== suite '${suite}': dev server unavailable, skipping its specs ===" >&2
                    STATUS=1
                    continue
                fi
            fi
        fi

        for spec in $SPECS; do
            run_playwright_spec "$spec" || STATUS=$?
        done

        if [ "$E2E_MODE" = "external" ]; then
            stop_dev_server
        fi
    done
    stop_mongo
fi

echo "=== Test run complete ==="
exit $STATUS
"""


_APPLY_TEST_PATCH = r"""
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch
"""


_APPLY_BOTH_PATCHES = r"""
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch
"""


_REBUILD = r"""
CHANGED_PKGS=$( { git diff --name-only HEAD; git ls-files --others --exclude-standard; } \
  | grep -E '^packages/' || true )
if [ -n "$CHANGED_PKGS" ]; then
    echo "=== rebuilding workspace (packages/ touched) ==="
    pnpm run build || echo "=== rebuild failed; continuing with the existing dist ===" >&2
else
    echo "=== no packages/ changes, skipping rebuild ==="
fi
"""


_RUN_SH = _SCRIPT_HEADER + _E2E_SUPPORT + _EXEC_TESTS
_TEST_RUN_SH = (
    _SCRIPT_HEADER + _E2E_SUPPORT + _APPLY_TEST_PATCH + _REBUILD + _EXEC_TESTS
)
_FIX_RUN_SH = (
    _SCRIPT_HEADER + _E2E_SUPPORT + _APPLY_BOTH_PATCHES + _REBUILD + _EXEC_TESTS
)


class PayloadV3ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return _tidy(
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", self._merged_env_block())
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
            .replace("__CLEAR_ENV__", self.clear_env)
        )

    def _merged_env_block(self) -> str:
        assignments = [
            line[len("ENV ") :]
            for line in self.global_env.splitlines()
            if line.startswith("ENV ")
        ]
        assignments.extend(_TOOLCHAIN_ENV)
        return DockerfileEnhancer._ENV_BLOCK + "".join(
            " \\\n    " + assignment for assignment in assignments
        )


class PayloadV3ImageDefault(Image):
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
        return PayloadV3ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__EXCLUDES__", _GIT_APPLY_EXCLUDES)
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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return _tidy(
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__CLEAR_ENV__", self.clear_env)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DURATION_RE = re.compile(r"\s*\((?:\d+(?:[.,]\d+)?\s*(?:ms|s|m)\s*)+\)\s*$")
_RETRY_RE = re.compile(r"\s*\(retry\s*#\d+\)\s*$")

_PASS_SYMBOLS = "✓✔√"
_FAIL_SYMBOLS = "✕✗×✘"
_SKIP_SYMBOLS = "○◯✎↓"

_JEST_SUITE_RE = re.compile(r"^(?:PASS|FAIL)\s+(\S+\.(?:spec|test)\.[cm]?[jt]sx?)")
_JEST_TEST_RE = re.compile(
    r"^(\s+)([" + _PASS_SYMBOLS + _FAIL_SYMBOLS + _SKIP_SYMBOLS + r"])\s+(\S.*)$"
)
_PW_TEST_RE = re.compile(
    r"^([" + _PASS_SYMBOLS + _FAIL_SYMBOLS + r"\-–])\s+\d+\s+"
    r"(?:\[[^\]]+\]\s*›\s*)?"
    r"(\S+?):\d+:\d+\s*›\s*(\S.*)$"
)
_JEST_BLOCK_TERMINATORS = (
    "Test Suites:",
    "Tests:",
    "Snapshots:",
    "Time:",
    "Ran all test suites",
    "console.log",
    "console.error",
    "console.warn",
    "console.info",
    "console.debug",
    "at ",
)
_JEST_SKIP_PREFIXES = ("skipped ", "todo ")

_PASS = "pass"
_FAIL = "fail"
_SKIP = "skip"


def _clean_title(title: str) -> str:
    title = _DURATION_RE.sub("", title.strip())
    return _RETRY_RE.sub("", title).strip()


def payload_v3_parse_log(test_log: str) -> TestResult:
    
    outcomes: dict[str, str] = {}

    def record(symbol: str, name: str) -> None:
        if not name:
            return
        if symbol in _PASS_SYMBOLS:
            outcomes[name] = _PASS
        elif symbol in _FAIL_SYMBOLS:
            outcomes[name] = _FAIL
        else:
            outcomes[name] = _SKIP

    current_file: Optional[str] = None
    describe_stack: list[str] = []
    in_jest_block = False

    for raw_line in test_log.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        stripped = line.strip()

        pw = _PW_TEST_RE.match(stripped)
        if pw:
            in_jest_block = False
            record(pw.group(1), f"{pw.group(2)} › {_clean_title(pw.group(3))}")
            continue

        suite = _JEST_SUITE_RE.match(stripped)
        if suite:
            current_file = suite.group(1)
            describe_stack = []
            in_jest_block = True
            continue

        if not stripped or not in_jest_block:
            continue

        if stripped.startswith("●") or stripped.startswith(_JEST_BLOCK_TERMINATORS):
            in_jest_block = False
            continue

        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_jest_block = False
            continue

        match = _JEST_TEST_RE.match(line)
        if match:
            symbol = match.group(2)
            title = _clean_title(match.group(3))
            if symbol in _SKIP_SYMBOLS:
                for prefix in _JEST_SKIP_PREFIXES:
                    if title.startswith(prefix):
                        title = title[len(prefix) :].strip()
                        break
            context = describe_stack[: max(indent // 2 - 1, 0)]
            parts = ([current_file] if current_file else []) + context + [title]
            record(symbol, " › ".join(part for part in parts if part))
        else:
            level = max(indent // 2, 1)
            describe_stack = describe_stack[: level - 1]
            describe_stack.append(stripped)

    passed_tests = {name for name, status in outcomes.items() if status == _PASS}
    failed_tests = {name for name, status in outcomes.items() if status == _FAIL}
    skipped_tests = {name for name, status in outcomes.items() if status == _SKIP}

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("payloadcms", _INTERVAL_NAME)
class PAYLOAD_10533_TO_7293(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PayloadV3ImageDefault(self.pr, self._config)

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
        return payload_v3_parse_log(test_log)


_INCUMBENT = Instance._registry.get("payloadcms/payload")


@Instance.register("payloadcms", "payload")
class PayloadV3Dispatch(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _LO <= pr.number <= _HI:
            return PAYLOAD_10533_TO_7293(pr, config, *args, **kwargs)
        if _INCUMBENT is not None:
            return _INCUMBENT(pr, config, *args, **kwargs)
        raise ValueError(
            f"payloadcms/payload#{pr.number} is outside {_LO}-{_HI} and no other "
            f"payload adapter is registered under the bare name"
        )


for _number in range(_LO, _HI + 1):
    Instance._registry.setdefault(f"payloadcms/{_number}", PAYLOAD_10533_TO_7293)
del _number
