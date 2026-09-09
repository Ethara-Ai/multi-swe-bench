import re
from typing import Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "payload_6782_to_4291"

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
)


_DRIZZLE_OVERRIDE = r"""
NEEDS_PAYLOAD_EXPORT=0
if grep -rqs --include='*.ts' --include='*.tsx' 'drizzle-kit/payload' packages 2>/dev/null; then
  NEEDS_PAYLOAD_EXPORT=1
fi
export NEEDS_PAYLOAD_EXPORT

python3 - <<'PYEOF'
import glob
import json
import os
import re

snapshot = re.compile(r"^(\d+\.\d+\.\d+)-[0-9a-f]{6,}$")
base_version = None

for path in ["package.json"] + sorted(glob.glob("packages/*/package.json")):
    try:
        with open(path) as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        continue
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        pinned = (manifest.get(section) or {}).get("drizzle-kit")
        match = snapshot.match(pinned) if isinstance(pinned, str) else None
        if match:
            base_version = match.group(1)

# The snapshot builds payload pins were cut with a "drizzle-kit/payload"
# subpath export that the plain published release of the same version does not
# carry: 0.20.14 exports only "." and "./utils-studio", so the beta era's
# db-postgres fails to compile with TS2307 on DrizzleSnapshotJSON,
# generateDrizzleJson and generateMigration. 0.20.15 is the first published
# release that exports "./payload" with all three symbols, so bump to it when
# the checked-out tree actually imports that subpath.
if base_version and os.environ.get("NEEDS_PAYLOAD_EXPORT") == "1":
    if tuple(int(part) for part in base_version.split(".")) < (0, 20, 15):
        base_version = "0.20.15"

if base_version:
    with open("package.json") as handle:
        root = json.load(handle)
    root.setdefault("pnpm", {}).setdefault("overrides", {})
    root["pnpm"]["overrides"]["drizzle-kit"] = base_version
    with open("package.json", "w") as handle:
        json.dump(root, handle, indent=2)
    print("=== drizzle-kit snapshot overridden to %s ===" % base_version)
else:
    print("=== no snapshot-pinned drizzle-kit at this commit ===")
PYEOF
"""


_SAVE_BINARY_ASSETS_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__
mkdir -p /home/binassets

python3 - <<'PYEOF'
import os
import re
import subprocess

OUT = "/home/binassets"
recovered = []

for patch in ("/home/test.patch", "/home/fix.patch"):
    if not os.path.exists(patch):
        continue
    with open(patch, encoding="utf-8", errors="replace") as handle:
        body = handle.read()
    for block in re.split(r"(?=^diff --git )", body, flags=re.M):
        if "Binary files" not in block and "GIT binary patch" not in block:
            continue
        header = re.match(r"diff --git a/(.*?) b/(.*?)\n", block)
        index = re.search(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", block, re.M)
        if not header or not index:
            continue
        target, blob = header.group(2), index.group(2)
        if set(blob) == {"0"}:
            continue
        destination = os.path.join(OUT, target)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        try:
            with open(destination, "wb") as handle:
                subprocess.check_call(["git", "cat-file", "blob", blob], stdout=handle)
        except subprocess.CalledProcessError:
            if os.path.exists(destination):
                os.remove(destination)
            print("=== could not rescue %s (blob %s) ===" % (target, blob))
            continue
        recovered.append(target)
        print("=== rescued %s from blob %s ===" % (target, blob))

with open(os.path.join(OUT, "MANIFEST"), "w") as handle:
    for entry in recovered:
        handle.write(entry + "\n")

if not recovered:
    print("=== no binary hunks in this PR's patches ===")
PYEOF
"""


_RESTORE_BINARY_ASSETS = r"""
if [ -s /home/binassets/MANIFEST ]; then
  while IFS= read -r asset || [ -n "$asset" ]; do
    [ -n "$asset" ] || continue
    if [ -f "/home/binassets/$asset" ]; then
      mkdir -p "$(dirname "$asset")"
      cp "/home/binassets/$asset" "$asset"
      echo "=== restored binary fixture: $asset ==="
    fi
  done < /home/binassets/MANIFEST
fi
"""


_SELECT_TESTS = r"""
if [ -f test/jest.config.js ]; then
  echo v3 > /home/era.txt
else
  echo v2 > /home/era.txt
fi
echo "=== detected era: $(cat /home/era.txt) ==="

CHANGED=$(cat /home/test.patch /home/fix.patch 2>/dev/null \
  | sed -n 's|^diff --git a/\(.*\) b/.*$|\1|p' | sort -u)

INT=$(printf '%s\n' "$CHANGED" | grep -E '^test/.*int\.spec\.tsx?$' | sort -u || true)
E2E=$(printf '%s\n' "$CHANGED" | grep -E '^test/.*e2e\.spec\.tsx?$' | sort -u || true)
UNIT=$(printf '%s\n' "$CHANGED" | grep -E '^packages/.+\.spec\.tsx?$' | sort -u || true)

if [ -z "$INT" ] && [ -z "$E2E" ] && [ -z "$UNIT" ]; then
  echo "=== patches name no spec; falling back to suite expansion ==="
  SUITES=$(printf '%s\n' "$CHANGED" | grep -E '^test/[^/]+/' | cut -d/ -f2 | sort -u || true)
  for suite in $SUITES; do
    [ -d "test/$suite" ] || continue
    FOUND=$(find "test/$suite" -name '*int.spec.ts' 2>/dev/null || true)
    [ -n "$FOUND" ] || continue
    INT=$(printf '%s\n%s\n' "$INT" "$FOUND" | sed '/^$/d' | sort -u)
  done
fi

build_pattern() {
  local pattern=""
  local file
  for file in $1; do
    [ -n "$file" ] || continue
    escaped=$(printf '%s' "$file" | sed 's/[][().*+?^$\\|{}]/\\&/g')
    pattern="${pattern}${pattern:+|}/${escaped}\$"
  done
  printf '%s' "$pattern"
}

build_pattern "$INT" > /home/jest_int_pattern.txt
build_pattern "$UNIT" > /home/jest_unit_pattern.txt
printf '%s\n' "$E2E" | sed '/^$/d' > /home/e2e_specs.txt

echo "=== selected jest int pattern:  $(cat /home/jest_int_pattern.txt) ==="
echo "=== selected jest unit pattern: $(cat /home/jest_unit_pattern.txt) ==="
echo "=== selected e2e specs:         $(tr '\n' ' ' < /home/e2e_specs.txt) ==="
"""


_EXEC_TESTS = r"""
ERA=$(cat /home/era.txt 2>/dev/null || echo v2)
INT_PATTERN=$(cat /home/jest_int_pattern.txt 2>/dev/null || true)
UNIT_PATTERN=$(cat /home/jest_unit_pattern.txt 2>/dev/null || true)

if [ "$ERA" = "v3" ]; then
  JEST_INT_CONFIG="test/jest.config.js"
  JEST_UNIT_CONFIG="jest.config.js"
  PW_CONFIG="test/playwright.config.ts"
  JEST_NODE_OPTIONS="--experimental-vm-modules --no-deprecation --max-old-space-size=4096"
else
  JEST_INT_CONFIG="jest.config.js"
  JEST_UNIT_CONFIG="jest.config.js"
  PW_CONFIG="playwright.config.ts"
  JEST_NODE_OPTIONS="--max-old-space-size=4096"
  if [ -n "$UNIT_PATTERN" ]; then
    INT_PATTERN="${INT_PATTERN}${INT_PATTERN:+|}${UNIT_PATTERN}"
    UNIT_PATTERN=""
  fi
fi

echo "=== era: $ERA ==="
echo "=== jest int pattern:  $INT_PATTERN ==="
echo "=== jest unit pattern: $UNIT_PATTERN ==="
echo "=== e2e specs:         $(tr '\n' ' ' < /home/e2e_specs.txt) ==="

STATUS=0

if [ -n "$INT_PATTERN" ]; then
  echo "=== Running Jest (integration) ==="
  NODE_OPTIONS="$JEST_NODE_OPTIONS" pnpm exec jest \
    --config="$JEST_INT_CONFIG" \
    --forceExit \
    --detectOpenHandles \
    --runInBand \
    --reporters=default \
    --testPathPattern="$INT_PATTERN" || STATUS=$?
else
  echo "=== No Jest integration suites selected ==="
fi

if [ -n "$UNIT_PATTERN" ]; then
  echo "=== Running Jest (unit) ==="
  NODE_OPTIONS="$JEST_NODE_OPTIONS" pnpm exec jest \
    --config="$JEST_UNIT_CONFIG" \
    --forceExit \
    --detectOpenHandles \
    --runInBand \
    --reporters=default \
    --testPathPattern="$UNIT_PATTERN" || STATUS=$?
fi

if [ -s /home/e2e_specs.txt ]; then
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    if [ ! -f "$spec" ]; then
      echo "=== E2E spec absent at this revision: $spec ==="
      continue
    fi
    echo "=== Running Playwright spec: $spec ==="
    NODE_OPTIONS="--no-deprecation" pnpm exec playwright test "$spec" \
      -c "$PW_CONFIG" \
      --reporter=list \
      --workers=1 \
      --retries=0 < /dev/null || STATUS=$?
  done < /home/e2e_specs.txt
else
  echo "=== No E2E specs selected ==="
fi

echo "=== Test run complete ==="
exit $STATUS
"""


_START_POSTGRES_SH = r"""#!/bin/bash
set -e

PGVER=$(ls /usr/lib/postgresql 2>/dev/null | sort -V | tail -1)
if [ -z "$PGVER" ]; then
  echo "no postgresql server installed" >&2
  exit 1
fi

pg_ctlcluster "$PGVER" main start 2>/dev/null || service postgresql start || true

for _ in $(seq 1 30); do
  if su postgres -c "psql -c 'SELECT 1'" > /dev/null 2>&1; then
    break
  fi
  sleep 1
done

su postgres -c "psql -c \"ALTER USER postgres WITH PASSWORD 'postgres';\"" > /dev/null 2>&1 || true
if ! su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='payloadtests'\"" | grep -q 1; then
  su postgres -c "createdb payloadtests" || true
fi

su postgres -c "psql -c 'SELECT version()'" > /dev/null 2>&1
echo "=== postgres ready on 127.0.0.1:5432/payloadtests ==="
"""

_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export NODE_ENV=test
export DISABLE_LOGGING=true
export PAYLOAD_DROP_DATABASE=true
export NODE_NO_WARNINGS=1

if [ "$(cat /home/db.txt 2>/dev/null)" = "postgres" ]; then
  bash /home/start-postgres.sh
  export PAYLOAD_DATABASE=postgres
  export POSTGRES_URL="postgresql://postgres:postgres@127.0.0.1:5432/payloadtests"
fi

cd /home/__REPO__
"""


_APPLY_TEST_PATCH = r"""
git reset --hard
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch
"""


_APPLY_BOTH_PATCHES = r"""
git reset --hard
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch
"""


_REBUILD = r"""
CHANGED_PKGS=$( { git diff --name-only HEAD; git ls-files --others --exclude-standard; } \
  | grep -E '^packages/' || true )
if [ -n "$CHANGED_PKGS" ]; then
  echo "=== Rebuilding workspace (packages/ touched) ==="
  pnpm run build
else
  echo "=== No packages/ changes, skipping rebuild ==="
fi
"""


_PROVISION = r"""#!/bin/bash
set -e

FIX_PKGS=$(sed -n 's|^diff --git a/packages/\([^/]*\)/.*|\1|p' /home/fix.patch 2>/dev/null | sort -u)
DB=mongo
if [ -n "$FIX_PKGS" ]; then
  DB=postgres
  for pkg in $FIX_PKGS; do
    case "$pkg" in
      db-postgres|db-sqlite|drizzle) ;;
      *) DB=mongo ;;
    esac
  done
fi
echo "$DB" > /home/db.txt
echo "=== database adapter for this PR: $DB (fix touches: $(echo $FIX_PKGS | tr '\n' ' ')) ==="

apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg git build-essential make python3 sudo lsb-release

wget -qO - https://pgp.mongodb.com/server-6.0.asc \
    | gpg --dearmor -o /usr/share/keyrings/mongodb-server-6.0.gpg
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-6.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/6.0 multiverse" \
    > /etc/apt/sources.list.d/mongodb-org-6.0.list
apt-get update
apt-get install -y --no-install-recommends mongodb-org-server

apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libx11-xcb1 \
    libxshmfence1 fonts-liberation fonts-noto-color-emoji
if [ "$(cat /home/db.txt)" = "postgres" ]; then
  apt-get install -y --no-install-recommends postgresql postgresql-contrib
fi

rm -rf /var/lib/apt/lists/*
test -x /usr/bin/mongod

npm install -g pnpm@8.15.9

mkdir -p /root/.npm/_libvips
case "$(uname -m)" in
    aarch64) SHARP_ARCH=linux-arm64v8 ;;
    x86_64)  SHARP_ARCH=linux-x64 ;;
    *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
esac
for LIBVIPS_VERSION in 8.13.3 8.14.5; do
  curl -fsSL -o "/root/.npm/_libvips/libvips-${LIBVIPS_VERSION}-${SHARP_ARCH}.tar.br" \
    "https://github.com/lovell/sharp-libvips/releases/download/v${LIBVIPS_VERSION}/libvips-${LIBVIPS_VERSION}-${SHARP_ARCH}.tar.br"
done

cd /home/__REPO__
"""


_PREPARE_SH = (
    _PROVISION
    + _DRIZZLE_OVERRIDE
    + r"""
installed=0
for attempt in 1 2 3 4 5; do
  if pnpm install --no-frozen-lockfile; then
    installed=1
    break
  fi
  echo "=== pnpm install attempt ${attempt} failed, retrying ==="
  sleep 20
done
test "$installed" -eq 1

test -x node_modules/.bin/jest
test -x node_modules/.bin/playwright

printf 'require("sharp")\n' > packages/payload/__sharp_probe.cjs
node packages/payload/__sharp_probe.cjs
rm -f packages/payload/__sharp_probe.cjs

pnpm exec playwright install --with-deps chromium || pnpm exec playwright install chromium
ls -d /ms-playwright/chromium-* > /dev/null

pnpm run build

git checkout -- package.json
"""
    + _SELECT_TESTS
)


_RUN_SH = _SCRIPT_HEADER + _EXEC_TESTS
_TEST_RUN_SH = (
    _SCRIPT_HEADER + _APPLY_TEST_PATCH + _RESTORE_BINARY_ASSETS + _REBUILD + _EXEC_TESTS
)
_FIX_RUN_SH = (
    _SCRIPT_HEADER + _APPLY_BOTH_PATCHES + _RESTORE_BINARY_ASSETS + _REBUILD + _EXEC_TESTS
)


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

__CLEAR_ENV__

WORKDIR /home/

RUN git -C /home clone "${REPO_URL}" __REPO__

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout __BASE_SHA__

__COPY_COMMANDS__
RUN bash /home/save-binary-assets.sh

__HARDENING__

RUN bash /home/prepare.sh

__CLEAR_ENV__
"""


class PayloadBundleImageBase(Image):

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
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

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


class PayloadBundleImageDefault(Image):

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
        return PayloadBundleImageBase(self.pr, self._config)

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
            File(".", "save-binary-assets.sh", self._render(_SAVE_BINARY_ASSETS_SH)),
            File(".", "start-postgres.sh", _START_POSTGRES_SH),
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
            .replace(
                "__HARDENING__",
                Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha),
            )
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


def _clean_title(title: str) -> str:
    title = _DURATION_RE.sub("", title.strip())
    return _RETRY_RE.sub("", title).strip()


def payload_bundle_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(symbol: str, name: str) -> None:
        if not name:
            return
        if symbol in _PASS_SYMBOLS:
            passed_tests.add(name)
        elif symbol in _FAIL_SYMBOLS:
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    current_file: str | None = None
    describe_stack: list[str] = []
    in_jest_block = False

    for raw_line in test_log.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        stripped = line.strip()

        pw_match = _PW_TEST_RE.match(stripped)
        if pw_match:
            in_jest_block = False
            spec = pw_match.group(2)
            title = _clean_title(pw_match.group(3))
            record(pw_match.group(1), f"{spec} › {title}")
            continue

        suite_match = _JEST_SUITE_RE.match(stripped)
        if suite_match:
            current_file = suite_match.group(1)
            describe_stack = []
            in_jest_block = True
            continue

        if not stripped:
            continue

        if not in_jest_block:
            continue

        if stripped.startswith("●") or stripped.startswith(
            _JEST_BLOCK_TERMINATORS
        ):
            in_jest_block = False
            continue

        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_jest_block = False
            continue

        test_match = _JEST_TEST_RE.match(line)
        if test_match:
            symbol = test_match.group(2)
            title = _clean_title(test_match.group(3))
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


@Instance.register("payloadcms", _INTERVAL_NAME)
class PAYLOAD_6782_TO_4291(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PayloadBundleImageDefault(self.pr, self._config)

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
        return payload_bundle_parse_log(test_log)


_INTERVAL_NAME_RE = re.compile(r"^payloadcms/payload_(?P<first>\d+)_to_(?P<second>\d+)$")


def _intervals() -> list[tuple[int, int, type]]:
    intervals: list[tuple[int, int, type]] = []
    for name, impl in Instance._registry.items():
        match = _INTERVAL_NAME_RE.match(name)
        if not match:
            continue
        first, second = int(match.group("first")), int(match.group("second"))
        intervals.append((min(first, second), max(first, second), impl))
    return intervals


def _raise(pr: PullRequest):
    raise ValueError(
        f"payloadcms/payload#{pr.number} is not covered by any registered "
        f"payload_<a>_to_<b> interval adapter; known intervals: "
        f"{sorted((lo, hi) for lo, hi, _ in _intervals())}"
    )


@Instance.register("payloadcms", "payload")
class PayloadDispatch(Instance):

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        candidates = sorted(
            (bounds for bounds in _intervals() if bounds[0] <= pr.number <= bounds[1]),
            key=lambda bounds: bounds[1] - bounds[0],
        )
        owners = [impl for _, _, impl in candidates]

        next(iter(owners), None) or _raise(pr)
        return owners[0](pr, config, *args, **kwargs)


# The generated dataset (data/dataset/<org>__<repo>_dataset.jsonl) comes back
# with number_interval set to the bare PR number rather than an interval name,
# so a record round-tripped through it resolves under "payloadcms/<number>" and
# not "payloadcms/payload".  Feeding that file back to build_dataset, or grading
# it with gen_report --mode evaluation, would otherwise fail with
# "Instance 'payloadcms/4291' is not registered".  Registering the router under
# every number this bundle owns closes that path; setdefault keeps it from
# shadowing anything another adapter has already claimed.
for _number in range(4291, 6783):
    Instance._registry.setdefault(f"payloadcms/{_number}", PayloadDispatch)
del _number
