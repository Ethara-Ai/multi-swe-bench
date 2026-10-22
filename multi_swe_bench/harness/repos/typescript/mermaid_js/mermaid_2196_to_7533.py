import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "mermaid_2196_to_7533"
_REPO_KEY = "mermaid"
_ORG_KEY = "mermaid-js"

_BUILDER_NODE_IMAGE = "node:22-bookworm"
_BASE_TAG = "base-2196_to_7533"

_MITM_PROXY_ARGS = '''\
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'''

_MITM_ENV_BLOCK = '''\
ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}'''

_MITM_CERT_SYMLINKS = '''\
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt'''

_HARDENING_BLOCK = """\
RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
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
    fi"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

_DETECT_TOOLCHAIN_SH = """#!/bin/bash
set -eo pipefail

cd /home/@@REPO@@

PM=npm
PM_VERSION=""
RUNNER=jest
NODE_MAJOR=18

read_pm_version() {
    node -e "try { const p = require('./package.json'); const m = (p.packageManager || '').match(/$1@([0-9]+(\\\\.[0-9]+)*)/); if (m) console.log(m[1]); } catch (e) {}" 2>/dev/null || true
}

if [ -f pnpm-lock.yaml ]; then
    PM=pnpm
    PM_VERSION=$(read_pm_version pnpm)
    case "${PM_VERSION%%.*}" in
        ""|6|7|8) NODE_MAJOR=18 ;;
        9)        NODE_MAJOR=20 ;;
        *)        NODE_MAJOR=22 ;;
    esac
elif [ -f yarn.lock ]; then
    PM=yarn
    PM_VERSION=$(read_pm_version yarn)
    NODE_MAJOR=16
fi

for pin_file in .node-version .nvmrc; do
    if [ -f "$pin_file" ]; then
        PINNED=$(tr -d ' \\t\\r\\n' < "$pin_file" | sed 's/^[vV]//')
        case "$PINNED" in
            [0-9]*) NODE_MAJOR="${PINNED%%.*}" ;;
        esac
        break
    fi
done

if node -e "const p = require('./package.json'); const d = Object.assign({}, p.dependencies, p.devDependencies); process.exit(d.vitest ? 0 : 1)" 2>/dev/null; then
    RUNNER=vitest
fi

{
  echo "PM=$PM"
  echo "PM_VERSION=$PM_VERSION"
  echo "RUNNER=$RUNNER"
  echo "NODE_MAJOR=$NODE_MAJOR"
} > /home/toolchain.env

echo "detect: PM=$PM PM_VERSION=${PM_VERSION:-unset} RUNNER=$RUNNER NODE_MAJOR=$NODE_MAJOR"
"""

_CODEGEN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export HUSKY=0
export NODE_OPTIONS=--max-old-space-size=4096
export CYPRESS_INSTALL_BINARY=0
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1

cd /home/@@REPO@@

TOPPED_UP=0
topup_install() {
    if [ "$TOPPED_UP" = "1" ]; then
        return 0
    fi
    TOPPED_UP=1
    . /home/toolchain.env
    case "$PM" in
        pnpm) pnpm install --no-frozen-lockfile 2>&1 | tail -25 \
                  || pnpm install --no-frozen-lockfile --ignore-scripts 2>&1 | tail -25 \
                  || true ;;
        yarn) yarn install --ignore-engines 2>&1 | tail -25 \
                  || yarn install --ignore-engines --ignore-scripts 2>&1 | tail -25 \
                  || yarn install 2>&1 | tail -25 || true ;;
        *)    npm install --legacy-peer-deps 2>&1 | tail -25 \
                  || npm install --legacy-peer-deps --ignore-scripts 2>&1 | tail -25 \
                  || true ;;
    esac
    if [ -n "${RUNNER:-}" ] && [ ! -x "./node_modules/.bin/$RUNNER" ]; then
        case "$PM" in
            pnpm) pnpm install --no-frozen-lockfile --ignore-scripts 2>&1 | tail -15 || true ;;
            yarn) yarn install --ignore-engines --ignore-scripts 2>&1 | tail -15 || true ;;
            *)    npm install --legacy-peer-deps --ignore-scripts 2>&1 | tail -15 || true ;;
        esac
    fi
}

CHANGED_MANIFESTS=$(
    {
        git ls-files --others --exclude-standard 2>/dev/null
        git diff --name-only 2>/dev/null
    } | grep -E '(^|/)(package\\.json|pnpm-workspace\\.yaml)$' | sort -u || true
)
if [ -n "$CHANGED_MANIFESTS" ]; then
    echo "codegen: patch changed dependency manifest(s):"
    echo "$CHANGED_MANIFESTS" | sed 's/^/  /'
    topup_install
fi

LANGIUM=""
resolve_langium() {
    for candidate in ./node_modules/.bin/langium ./packages/parser/node_modules/.bin/langium; do
        if [ -x "$candidate" ]; then
            LANGIUM="$(cd "$(dirname "$candidate")" && pwd)/langium"
            return 0
        fi
    done
    LANGIUM=""
    return 1
}

if [ -f packages/parser/langium-config.json ]; then
    if ! resolve_langium; then
        echo "codegen: langium toolchain absent, topping up workspace install"
        topup_install
        resolve_langium || true
    fi
    if [ -z "$LANGIUM" ]; then
        echo "codegen: FATAL langium grammar generation is required but the binary is unresolvable"
        exit 1
    fi
    ( cd packages/parser && "$LANGIUM" generate )
    echo "codegen: langium generate done"
fi

if [ -x ./node_modules/.bin/tsx ]; then
    if [ -f .esbuild/build.ts ]; then
        ./node_modules/.bin/tsx .esbuild/build.ts
        echo "codegen: esbuild bundle done"
    elif [ -f .vite/build.ts ]; then
        ./node_modules/.bin/tsx .vite/build.ts
        echo "codegen: vite bundle done"
    fi
fi
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export HUSKY=0
export NODE_OPTIONS=--max-old-space-size=4096
export CYPRESS_INSTALL_BINARY=0
export CYPRESS_CACHE_FOLDER=/home/@@REPO@@/.cache/Cypress
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1

cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@BASE_SHA@@
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "$(git rev-parse @@BASE_SHA@@)"
echo "prepare: pinned to @@BASE_SHA@@"

bash /home/detect_toolchain.sh
. /home/toolchain.env

if [ "$(node -p 'process.versions.node.split(".")[0]')" != "$NODE_MAJOR" ]; then
    echo "prepare: switching node to ${NODE_MAJOR}"
    npm install -g n
    n install "$NODE_MAJOR"
    hash -r
fi
node --version

case "$PM" in
    pnpm)
        if [ -n "$PM_VERSION" ]; then
            npm install -g "pnpm@$PM_VERSION" 2>&1 || npm install -g pnpm 2>&1 || true
        else
            npm install -g pnpm 2>&1 || true
        fi
        pnpm install --frozen-lockfile 2>&1 \
            || pnpm install --no-frozen-lockfile 2>&1 \
            || pnpm install --no-frozen-lockfile --ignore-scripts 2>&1 \
            || true
        ;;
    yarn)
        if [ -n "$PM_VERSION" ]; then
            npm install -g "yarn@$PM_VERSION" 2>&1 || npm install -g yarn@1 2>&1 || true
        else
            npm install -g yarn@1 2>&1 || true
        fi
        yarn install --frozen-lockfile 2>&1 \
            || yarn install --ignore-engines 2>&1 \
            || yarn install --ignore-engines --ignore-scripts 2>&1 \
            || true
        ;;
    *)
        npm install --legacy-peer-deps 2>&1 \
            || npm install --legacy-peer-deps --ignore-scripts 2>&1 \
            || true
        ;;
esac

if [ ! -d node_modules ]; then
    echo "prepare: FATAL node_modules absent after install"
    exit 1
fi

bash /home/codegen.sh

if [ -f packages/parser/langium-config.json ] && [ ! -d packages/parser/src/language/generated ]; then
    echo "prepare: FATAL langium generated sources missing"
    exit 1
fi

RUNNER_BIN=""
for candidate in "./node_modules/.bin/$RUNNER" packages/*/node_modules/.bin/"$RUNNER"; do
    if [ -x "$candidate" ]; then
        RUNNER_BIN="$(cd "$(dirname "$candidate")" && pwd)/$RUNNER"
        break
    fi
done
if [ -z "$RUNNER_BIN" ]; then
    echo "prepare: FATAL ${RUNNER} is unresolvable after install"
    exit 1
fi
"$RUNNER_BIN" --version > /dev/null
echo "RUNNER_BIN=$RUNNER_BIN" >> /home/toolchain.env
echo "prepare: runner ${RUNNER} -> ${RUNNER_BIN}"
echo "DEPS_OK"
"""

_INVOKE_RUNNER = """
. /home/toolchain.env
RUNNER_BIN="${RUNNER_BIN:-./node_modules/.bin/$RUNNER}"
if [ ! -x "$RUNNER_BIN" ]; then
    echo "run: FATAL ${RUNNER} is unresolvable at ${RUNNER_BIN}"
    exit 1
fi

TARGETS=""
if [ -s /home/test_files.txt ]; then
    while IFS= read -r candidate; do
        if [ -n "$candidate" ] && [ -f "$candidate" ]; then
            TARGETS="$TARGETS $candidate"
        fi
    done < /home/test_files.txt
    if [ -n "$TARGETS" ]; then
        echo "run: targets =$TARGETS"
    fi
else
    ALL_TESTS=$(git ls-files \
            '*.spec.ts' '*.spec.tsx' '*.spec.mts' '*.spec.cts' \
            '*.spec.js' '*.spec.jsx' \
            '*.test.ts' '*.test.tsx' '*.test.mts' '*.test.cts' \
            '*.test.js' '*.test.jsx' 2>/dev/null \
        | grep -vE '(^|/)(cypress|e2e)/' || true)
    if [ -s /home/fix_test_files.txt ] && [ -n "$ALL_TESTS" ]; then
        ALL_TESTS=$(printf '%s\\n' "$ALL_TESTS" | grep -vxF -f /home/fix_test_files.txt || true)
    fi
    for candidate in $ALL_TESTS; do
        if [ -f "$candidate" ]; then
            TARGETS="$TARGETS $candidate"
        fi
    done
    if [ -n "$TARGETS" ]; then
        echo "run: full-suite fallback, $(printf '%s\\n' $TARGETS | wc -l | tr -d ' ') file(s)"
    fi
fi

if [ -z "$TARGETS" ]; then
    if [ "$STAGE" = "baseline" ]; then
        echo "run: no target test file exists at baseline; nothing to execute"
        exit 0
    fi
    echo "run: FATAL no runnable test file at stage ${STAGE}"
    exit 1
fi

TEST_STATUS=0
if [ "$RUNNER" = "vitest" ]; then
    timeout -k 60 3600 "$RUNNER_BIN" run --reporter=verbose $TARGETS 2>&1 | tee /home/test_output.log || TEST_STATUS=$?
else
    timeout -k 60 3600 "$RUNNER_BIN" --ci --verbose $TARGETS 2>&1 | tee /home/test_output.log || TEST_STATUS=$?
fi

if [ "$TEST_STATUS" = "124" ] || [ "$TEST_STATUS" = "137" ]; then
    echo "run: FATAL ${RUNNER} exceeded the time budget at stage ${STAGE}; output is truncated"
    exit 1
fi
if ! grep -qE "(Test Files|Tests:|Tests |Test Suites:|^PASS |^FAIL )" /home/test_output.log; then
    echo "run: FATAL ${RUNNER} produced no recognizable output"
    exit 1
fi
echo "run: ${RUNNER} exited with ${TEST_STATUS}"
"""

_LOCK_DETECT = """
if [ -f pnpm-lock.yaml ]; then
    LOCK_EXCLUDE="pnpm-lock.yaml"
elif [ -f yarn.lock ]; then
    LOCK_EXCLUDE="yarn.lock"
else
    LOCK_EXCLUDE="package-lock.json"
fi
APPLY_ARGS="--exclude=$LOCK_EXCLUDE --whitespace=nowarn"
for binary_glob in '*.png' '*.jpg' '*.jpeg' '*.gif' '*.ico' '*.pdf' '*.jar' \
                   '*.mp4' '*.woff' '*.woff2' '*.ttf' '*.eot' '*.zip'; do
    APPLY_ARGS="$APPLY_ARGS --exclude=$binary_glob"
done
APPLY_ARGS="$APPLY_ARGS --exclude=dist/* --exclude=*/dist/*"

_apply_patch() {
    patch_file="$1"
    label="$2"
    if git apply $APPLY_ARGS "$patch_file" 2>/tmp/_apply_err; then
        echo "apply($label): clean"
        return 0
    fi
    echo "apply($label): direct apply rejected, retrying as a three-way merge"
    tail -5 /tmp/_apply_err || true
    if ! git apply --3way $APPLY_ARGS "$patch_file" 2>/tmp/_apply_err; then
        echo "apply($label): FATAL patch does not apply to this tree"
        tail -5 /tmp/_apply_err || true
        return 1
    fi
    if [ -n "$(git diff --name-only --diff-filter=U 2>/dev/null)" ]; then
        echo "apply($label): FATAL three-way merge left conflicted paths"
        return 1
    fi
    echo "apply($label): clean (three-way)"
    return 0
}
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export FORCE_COLOR=0
export NO_COLOR=1
STAGE=baseline

cd /home/@@REPO@@
@@INVOKE@@
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export FORCE_COLOR=0
export NO_COLOR=1
STAGE=test

cd /home/@@REPO@@
@@LOCK_DETECT@@
_apply_patch /home/test.patch test
bash /home/codegen.sh
@@INVOKE@@
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export FORCE_COLOR=0
export NO_COLOR=1
STAGE=fix

cd /home/@@REPO@@
@@LOCK_DETECT@@
_apply_patch /home/test.patch test
_apply_patch /home/fix.patch fix
bash /home/codegen.sh
@@INVOKE@@
"""


def _render(template: str, repo: str, base_sha: str = "") -> str:
    return (
        template.replace("@@INVOKE@@", _INVOKE_RUNNER)
        .replace("@@LOCK_DETECT@@", _LOCK_DETECT)
        .replace("@@BASE_SHA@@", base_sha)
        .replace("@@REPO@@", repo)
    )


_DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_TEST_FILE_RE = re.compile(r"\.(?:test|spec)\.(?:ts|tsx|js|jsx|mts|cts)$")
_NON_UNIT_DIR_RE = re.compile(r"(?:^|/)(?:cypress|e2e|node_modules)/")


def _changed_paths(patch: str) -> list[str]:
    paths: list[str] = []
    pending: Optional[str] = None
    for line in (patch or "").replace("\r\n", "\n").split("\n"):
        match = _DIFF_PATH_RE.match(line)
        if match:
            if pending is not None:
                paths.append(pending)
            pending = match.group(2)
            continue
        if pending is not None and line.startswith("deleted file mode"):
            pending = None
    if pending is not None:
        paths.append(pending)
    return paths


def _unit_test_files(patch: str) -> str:
    targets = sorted(
        {
            path
            for path in _changed_paths(patch)
            if _TEST_FILE_RE.search(path) and not _NON_UNIT_DIR_RE.search(path)
        }
    )
    if not targets:
        return ""
    return "\n".join(targets) + "\n"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_SUITE_HEAD_RE = re.compile(r"^(?:PASS|FAIL)\s+(\S+\.(?:test|spec)[^\s\[]*)")
_FILE_NAME_RE = re.compile(r"^\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mts|cts)$")
_TRAILING_TIME_RE = re.compile(r"\s*\(?\d+(?:[.,]\d+)?\s*(?:ms|s|m)\)?$")
_TRAILING_COUNT_RE = re.compile(r"\s*\(\d+\s+tests?[^)]*\)$")
_SUMMARY_RE = re.compile(
    r"^(?:Test Suites:|Tests:|Tests\s|Snapshots:|Time:|Ran all test suites"
    r"|Test Files|Start at|Duration|Coverage|Summary of all failing tests"
    r"|RUN |DONE |ERROR |Serialized Error)"
)
_SKIP_PREFIX_RE = re.compile(r"^(?:skipped|todo)\s+")
_PASS_MARKS = "\u2714\u2713\u221a"
_FAIL_MARKS = "\u2715\u00d7\u2717"
_SKIP_MARKS = "\u25cb\u25cc\u2193\u270e"
_STATUS_MARKS = _PASS_MARKS + _FAIL_MARKS + _SKIP_MARKS


def _strip_meta(text: str) -> str:
    cleaned = text.strip()
    while True:
        shorter = _TRAILING_TIME_RE.sub("", cleaned)
        shorter = _TRAILING_COUNT_RE.sub("", shorter)
        shorter = shorter.rstrip()
        if shorter == cleaned:
            return cleaned
        cleaned = shorter


def _qualify(name: str, current_file: str, headers: dict[int, str], indent: int) -> str:
    if " > " in name:
        return name
    parts: list[str] = []
    if current_file:
        parts.append(current_file)
    for level in sorted(headers):
        if level < indent:
            parts.append(headers[level])
    parts.append(name)
    return " > ".join(parts)


def _has_status_child(
    entries: list[tuple[int, str]], position: int, indent: int
) -> bool:
    if position + 1 >= len(entries):
        return False
    next_indent, next_text = entries[position + 1]
    if next_indent <= indent:
        return False
    return next_text[0] in _STATUS_MARKS


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    entries: list[tuple[int, str]] = []
    for raw_line in test_log.splitlines():
        line = _ANSI_RE.sub("", raw_line).replace("\t", "    ").rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        entries.append((len(line) - len(line.lstrip()), stripped))

    current_file = ""
    headers: dict[int, str] = {}
    in_suite_body = False

    for position, (indent, stripped) in enumerate(entries):
        suite = _SUITE_HEAD_RE.match(stripped)
        if suite:
            current_file = suite.group(1)
            headers = {}
            in_suite_body = True
            continue

        if stripped.startswith("\u25cf") or _SUMMARY_RE.match(stripped):
            headers = {}
            in_suite_body = False
            continue

        mark = stripped[0]

        if mark in _STATUS_MARKS:
            name = _strip_meta(stripped[1:])
            if mark in _SKIP_MARKS:
                name = _strip_meta(_SKIP_PREFIX_RE.sub("", name))
            if not name:
                continue
            if _FILE_NAME_RE.match(name):
                current_file = name
                headers = {}
                in_suite_body = True
                continue
            if _has_status_child(entries, position, indent):
                headers = {
                    level: text for level, text in headers.items() if level < indent
                }
                headers[indent] = name
                in_suite_body = True
                continue
            full_name = _qualify(name, current_file, headers, indent)
            if mark in _FAIL_MARKS:
                failed_tests.add(full_name)
            elif mark in _PASS_MARKS:
                passed_tests.add(full_name)
            else:
                skipped_tests.add(full_name)
            continue

        if in_suite_body:
            headers = {
                level: text for level, text in headers.items() if level < indent
            }
            headers[indent] = _strip_meta(stripped)

    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    passed_tests -= skipped_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class ImageBase(Image):

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
        return _BUILDER_NODE_IMAGE

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

        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        global_env = self.global_env.strip()
        clear_env = self.clear_env.strip()
        global_section = f"\n{global_env}\n" if global_env else ""
        clear_section = f"\n{clear_env}\n" if clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{_MITM_PROXY_ARGS}

{_MITM_ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_MITM_CERT_SYMLINKS}
{global_section}
WORKDIR /home/

{code}

WORKDIR /home/{repo}
{clear_section}
CMD ["/bin/bash"]
"""


class ImageDefault(Image):

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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}-{interval}".format(
            number=self.pr.number, interval="2196_to_7533"
        )

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "detect_toolchain.sh",
                _render(_DETECT_TOOLCHAIN_SH, repo=repo),
            ),
            File(".", "codegen.sh", _render(_CODEGEN_SH, repo=repo)),
            File(".", "test_files.txt", _unit_test_files(self.pr.test_patch)),
            File(".", "fix_test_files.txt", _unit_test_files(self.pr.fix_patch)),
            File(
                ".",
                "prepare.sh",
                _render(_PREPARE_SH, repo=repo, base_sha=self.pr.base.sha),
            ),
            File(".", "run.sh", _render(_RUN_SH, repo=repo)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, repo=repo)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, repo=repo)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        return """FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout --detach ${{BASE_COMMIT}}

{hardening}
""".format(
            name=name,
            tag=tag,
            sha=self.pr.base.sha,
            copy_commands=copy_commands,
            hardening=_HARDENING_BLOCK,
        )


@Instance.register(_ORG_KEY, _REPO_KEY)
class MERMAID(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        return _parse_log(test_log)


Instance.register(_ORG_KEY, _INTERVAL_NAME)(MERMAID)
