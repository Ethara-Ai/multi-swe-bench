import base64
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "payload_12530_to_13298"
_NODE_IMAGE = "node:23.11-bookworm"
_PNPM_VERSION = "9.7.1"
_MONGO_SERIES = "7.0"

_APT_PACKAGES = (
    "ca-certificates curl git gnupg wget xz-utils unzip "
    "build-essential pkg-config python3 libvips-dev"
)


def _render(template: str, **tokens: object) -> str:
    out = template
    for key, value in tokens.items():
        out = out.replace(f"@@{key.upper()}@@", str(value))
    leftover = re.findall(r"@@[A-Z_]+@@", out)
    if leftover:
        raise ValueError(f"unsubstituted template tokens: {sorted(set(leftover))}")
    return out


_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM @@BASE_IMAGE@@

ARG TARGETARCH
ARG REPO_URL="https://github.com/@@ORG@@/@@REPO@@.git"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="@@ORG@@/@@REPO@@" \\
      org.opencontainers.image.description="@@ORG@@/@@REPO@@ Docker image" \\
      org.opencontainers.image.source="https://github.com/@@ORG@@/@@REPO@@" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN --mount=type=secret,id=mitm_ca,required=0 \\
    if [ -f /run/secrets/mitm_ca ]; then \\
        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \\
    fi

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends @@APT_PACKAGES@@ && \\
    wget -qO - https://pgp.mongodb.com/server-@@MONGO@@.asc \\
        | gpg --dearmor -o /usr/share/keyrings/mongodb-server-@@MONGO@@.gpg && \\
    echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-@@MONGO@@.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/@@MONGO@@ multiverse" \\
        > /etc/apt/sources.list.d/mongodb-org-@@MONGO@@.list && \\
    apt-get update && \\
    apt-get install -y --no-install-recommends mongodb-org-server mongodb-mongosh && \\
    mkdir -p /data/db && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN npm install -g pnpm@@@PNPM@@ && pnpm --version

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/@@REPO@@ && \\
    cd /home/@@REPO@@ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


_PR_DOCKERFILE = """FROM @@BASE_IMAGE@@

@@PREPARE_COPIES@@
WORKDIR /home/@@REPO@@

RUN bash /home/prepare.sh

@@GRADED_COPIES@@
RUN set -eux; \\
    git checkout --detach @@SHA@@; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse @@SHA@@)"; \\
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
"""


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: not inside a git repository" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "check_git_changes: uncommitted changes" >&2
  git status --porcelain >&2
  exit 1
fi

echo "check_git_changes: clean"
"""


_STRIP_BINARY_DIFFS_PY = r'''#!/usr/bin/env python3
import re
import sys


def strip_binary_diffs(patch_path):
    with open(patch_path, "r", errors="replace") as handle:
        content = handle.read()

    diffs = re.split(r"(?=^diff --git )", content, flags=re.MULTILINE)
    kept = []
    for diff in diffs:
        if not diff.strip():
            continue
        if "GIT binary patch" in diff or "Binary files" in diff:
            sys.stderr.write("strip_binary_diffs: dropped binary hunk\n")
            continue
        kept.append(diff)

    with open(patch_path, "w") as handle:
        handle.write("".join(kept))


if __name__ == "__main__":
    for path in sys.argv[1:]:
        strip_binary_diffs(path)
'''


_SELECT_E2E_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/@@REPO@@

CANDIDATES=$(grep -E '^diff --git a/.*e2e\.spec\.ts' /home/test.patch \
             | sed 's|^diff --git a/||; s| b/.*$||' | sort -u || true)

for f in $CANDIDATES; do
  if [ -n "$f" ] && [ -f "$f" ]; then
    printf '%s\n' "$f"
  fi
done

exit 0
"""


_RUN_E2E_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/@@REPO@@

SPECS=$(bash /home/select-e2e.sh)

if [ -z "$SPECS" ]; then
  echo "run-e2e: no e2e spec from the test patch is present in this tree"
  exit 0
fi

PW_CONFIG=test/playwright.config.ts
if [ ! -f "$PW_CONFIG" ]; then
  echo "run-e2e: $PW_CONFIG not found" >&2
  exit 1
fi

if [ ! -x node_modules/.bin/playwright ]; then
  echo "run-e2e: playwright is not installed in this tree" >&2
  exit 1
fi

# Only the test titles introduced by the test patch; empty means run whole specs.
GREP_PATTERN=@@GREP@@

FOLDERS=$(printf '%s\n' $SPECS | sed -E 's|^test/([^/]+)/.*|\1|' | sort -u)

kill_dev_servers() {
  pkill -f 'test/dev\.ts' 2>/dev/null || true
  pkill -f 'next dev' 2>/dev/null || true
  pkill -f 'next-server' 2>/dev/null || true
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null -m 2 "http://localhost:3000" 2>/dev/null; then
      sleep 1
    else
      break
    fi
  done
}

status=0

for folder in $FOLDERS; do
  echo "run-e2e: === suite $folder ==="

  # Orphaned dev servers from a previous suite hold port 3000 and poison the
  # next suite with a stale .next build (missing [turbopack] chunks).
  kill_dev_servers
  rm -rf .next node_modules/.cache/webpack

  if grep -q "max-old-space-size" package.json; then
    START_MEMORY_DB=true PORT=3000 \
      pnpm dev "$folder" --start-memory-db > "/home/dev-$folder.log" 2>&1 &
  else
    START_MEMORY_DB=true PORT=3000 \
    NODE_OPTIONS="--no-deprecation --no-experimental-strip-types --max-old-space-size=8192" \
      node_modules/.bin/tsx ./test/dev.ts "$folder" --start-memory-db > "/home/dev-$folder.log" 2>&1 &
  fi
  DEV_PID=$!

  ready=0
  for _ in $(seq 1 "${DEV_BOOT_TIMEOUT:-900}"); do
    if ! kill -0 "$DEV_PID" 2>/dev/null; then
      break
    fi
    code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://localhost:3000/admin" 2>/dev/null || true)
    [ -z "$code" ] && code=000
    if [ "$code" != "000" ]; then
      ready=1
      echo "run-e2e: dev server for $folder answered with HTTP $code"
      break
    fi
    sleep 1
  done

  if [ "$ready" != 1 ]; then
    echo "run-e2e: dev server for $folder did not become ready" >&2
    tail -n 60 "/home/dev-$folder.log" >&2 || true
    status=1
    kill "$DEV_PID" 2>/dev/null || true
    kill_dev_servers
    wait "$DEV_PID" 2>/dev/null || true
    continue
  fi

  # Warm the admin shell so first-test route compilation does not eat into
  # test timeouts or race against form initialization assertions.
  for _ in 1 2 3; do
    curl -s -o /dev/null -m 120 "http://localhost:3000/admin" || true
    curl -s -o /dev/null -m 120 "http://localhost:3000/admin/login" || true
  done

  for spec in $SPECS; do
    case "$spec" in
      test/"$folder"/*) ;;
      *) continue ;;
    esac
    REL=${spec#test/}
    echo "run-e2e: --- spec $REL ---"
    if [ -n "$GREP_PATTERN" ]; then
      timeout --preserve-status "${E2E_TIMEOUT:-2700}" \
        node_modules/.bin/tsx node_modules/@playwright/test/cli.js test "$REL" -c "$PW_CONFIG" \
          --grep "$GREP_PATTERN" \
          --workers=1 --retries=2 --reporter=list || status=$?
    else
      timeout --preserve-status "${E2E_TIMEOUT:-2700}" \
        node_modules/.bin/tsx node_modules/@playwright/test/cli.js test "$REL" -c "$PW_CONFIG" \
          --workers=1 --retries=2 --reporter=list || status=$?
    fi
  done

  kill "$DEV_PID" 2>/dev/null || true
  kill_dev_servers
  wait "$DEV_PID" 2>/dev/null || true
done

exit $status
"""


_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/@@REPO@@

git reset --hard
git clean -fdxq
bash /home/check_git_changes.sh

git cat-file -e @@SHA@@^{commit} 2>/dev/null \
  || git fetch --no-tags --depth=2147483647 origin @@SHA@@ \
  || git fetch --no-tags origin "+refs/pull/@@NUMBER@@/head:refs/remotes/origin/pr-@@NUMBER@@"

git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

export CI=true
export NODE_OPTIONS="--no-deprecation --max-old-space-size=8192"
export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
export NEXT_TELEMETRY_DISABLED=1
export DO_NOT_TRACK=1
export npm_config_build_from_source=false

pnpm install --frozen-lockfile \
  || pnpm install --no-frozen-lockfile \
  || pnpm install --no-frozen-lockfile --ignore-scripts

git checkout -- package.json pnpm-lock.yaml 2>/dev/null || true

TARGET_ARCH="$(dpkg --print-architecture)"
NODE_ARCH="$(node -p 'process.arch')"
echo "prepare: target architecture $TARGET_ARCH (node reports $NODE_ARCH)"

sharp_ok=0
for attempt in 1 2 3; do
  if pnpm rebuild sharp || npm rebuild sharp; then
    if node -e "require('sharp'); console.log('SHARP_OK')"; then
      sharp_ok=1
      break
    fi
  fi
  echo "prepare: sharp unavailable on $TARGET_ARCH after attempt $attempt; retrying"
  sleep 10
done
if [ "$sharp_ok" != 1 ]; then
  echo "prepare: WARNING sharp could not be provisioned for $TARGET_ARCH; suites that exercise uploads will fail"
fi

pnpm exec playwright install-deps chromium || true

playwright_ok=0
for attempt in 1 2 3; do
  if pnpm exec playwright install chromium; then
    playwright_ok=1
    break
  fi
  echo "prepare: playwright chromium download for $TARGET_ARCH failed on attempt $attempt; retrying"
  sleep 10
done
test "$playwright_ok" = 1

node -e "require('./package.json'); require.resolve('next'); require.resolve('@playwright/test'); require.resolve('mongodb-memory-server'); console.log('DEPS_OK')"

test -x node_modules/.bin/playwright
test -f test/playwright.config.ts
test -n "$(find "$PLAYWRIGHT_BROWSERS_PATH" -maxdepth 2 -type d -name 'chromium*' -print -quit)"
"""


_STAGE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export DISABLE_LOGGING=true
export NODE_NO_WARNINGS=1
export NODE_OPTIONS="--no-deprecation --no-experimental-strip-types --max-old-space-size=8192"
export NODE_PATH=/home/@@REPO@@/node_modules/.pnpm/node_modules
export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
export NEXT_TELEMETRY_DISABLED=1
export DO_NOT_TRACK=1
export PAYLOAD_DROP_DATABASE=true
export PAYLOAD_PUBLIC_DISABLE_AUTO_LOGIN=false
export START_MEMORY_DB=true
export MONGOMS_SYSTEM_BINARY=/usr/bin/mongod
export MONGOMS_SYSTEM_BINARY_VERSION_CHECK=false
export MONGOMS_DISABLE_POSTINSTALL=1
export MONGOMS_STORAGE_ENGINE=wiredTiger
export DEV_BOOT_TIMEOUT=900
export E2E_TIMEOUT=2700

cd /home/@@REPO@@

@@PATCH_STEP@@
STAGE_LOG=/home/stage-output.log
: > "$STAGE_LOG"
RAN_TESTS=0

if [ -n "$(bash /home/select-e2e.sh 2>/dev/null || true)" ]; then
  RAN_TESTS=1
  echo "=== e2e tests ==="
  if bash /home/run-e2e.sh 2>&1 | tee -a "$STAGE_LOG"; then
    echo "=== e2e runner exited 0 ==="
  else
    echo "=== e2e runner exited non-zero (test failures are expected in this stage) ==="
  fi
else
  echo "=== e2e tests: no e2e spec from the test patch exists at this commit ==="
fi

if [ "$RAN_TESTS" = "1" ]; then
  if ! grep -qE "(^|[[:space:]])([0-9]+ (passed|failed|skipped)|[✔✓√×✕✗✘✖])" "$STAGE_LOG"; then
    echo "FATAL: tests were selected but no recognizable test output was produced" >&2
    tail -n 80 "$STAGE_LOG" >&2 || true
    exit 1
  fi
else
  echo "=== stage ran no tests: nothing the test patch touches exists at this commit ==="
fi

echo "=== Test run complete ==="
"""


_APPLY_TEST_PATCH = r"""python3 /home/strip_binary_diffs.py /home/test.patch
git apply --whitespace=nowarn --verbose /home/test.patch
"""

_APPLY_BOTH_PATCHES = r"""python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch
git apply --whitespace=nowarn --verbose /home/test.patch /home/fix.patch
"""


_MARK_PASS = "✔✓√"
_MARK_FAIL = "×✕✗✘✖"
_MARK_SKIP = "○◌"
_MARK_ALL = _MARK_PASS + _MARK_FAIL + _MARK_SKIP

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

_JEST_FILE_RE = re.compile(r"^(?:PASS|FAIL)\s+(?P<file>[^\s(]+\.[cm]?[jt]sx?)\b")

_PLAYWRIGHT_RE = re.compile(
    r"^(?P<mark>[" + _MARK_ALL + r"-])\s+\d+\s+(?P<name>(?:\[[^\]]+\]\s*)?\S.*›.+)$"
)

_UNIT_RE = re.compile(
    r"^(?P<mark>[" + _MARK_ALL + r"])\s+(?:skipped\s+)?(?P<name>\S.*?)$"
)

_LOCATION_RE = re.compile(r"(?P<file>[\w./@-]+\.[cm]?[jt]sx?):\d+:\d+")

_TRAILING_RES = [
    re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s|m|min)\)$"),
    re.compile(r"\s+\d+(?:\.\d+)?\s*(?:ms|s)$"),
    re.compile(r"\s*\(\d+\s+tests?(?:\s*\|[^)]*)?\)$"),
    re.compile(r"\s*\(retry\s*#?\s*\d+\)$", re.IGNORECASE),
    re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\s*\)$"),
]


def _strip_trailing_metadata(name: str) -> str:
    previous = None
    while previous != name:
        previous = name
        for pattern in _TRAILING_RES:
            name = pattern.sub("", name)
        name = name.rstrip()
    return name


def _normalize_lines(test_log: str) -> list[str]:
    text = _OSC_RE.sub("", test_log)
    text = _CSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.split("\n")


def _canonical_name(name: str) -> str:
    name = _strip_trailing_metadata(name)
    name = _LOCATION_RE.sub(lambda m: m.group("file"), name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


_PATCH_TITLE_RE = re.compile(
    r"^[+-]\s*(?:test|it)(?:\.\w+)?\(\s*['\"`]([^'\"`]{3,})"
)


def _grep_pattern(test_patch: str) -> str:
    titles: list[str] = []
    for line in (test_patch or "").replace("\r", "").split("\n"):
        match = _PATCH_TITLE_RE.match(line)
        if not match:
            continue
        title = match.group(1).strip()
        if title and title not in titles:
            titles.append(title)
    return "|".join(re.escape(title) for title in titles)


def _stage_command(repo: str, script: str, test_patch: str) -> str:
    runner = _render(
        _RUN_E2E_SH,
        repo=repo,
        grep=shlex.quote(_grep_pattern(test_patch)),
    )
    fixup = (
        "#!/bin/bash\n"
        f"export NODE_PATH=/home/{repo}/node_modules/.pnpm/node_modules\n"
        # Old baked stage scripts lack the flag; without it Node 23 loads
        # workspace TS sources in strip-only mode and specs die on `export enum`.
        "sed -i 's/--no-deprecation --max-old-space-size/"
        "--no-deprecation --no-experimental-strip-types --max-old-space-size/' "
        f"/home/{script}\n"
        "sed -i 's/passed|failed|skipped/passed|failed|skipped|No tests found/' "
        f"/home/{script}\n"
        # Replace whatever run-e2e.sh was baked into the image with the
        # current hardened runner, so image rebuilds are never required.
        "cat > /home/run-e2e.sh <<'MSB_RUNE2E_EOF'\n"
        f"{runner}"
        "MSB_RUNE2E_EOF\n"
        # Dev-mode CI is slow; give expect() polls enough headroom to observe
        # post-save UI refreshes (e.g. tenant selector resync).
        f"sed -i 's/EXPECT_TIMEOUT = 6000 \\* smallMultiplier/"
        "EXPECT_TIMEOUT = 20000 * smallMultiplier/' "
        f"/home/{repo}/test/playwright.config.ts || true\n"
        f"exec bash /home/{script}\n"
    )
    blob = base64.b64encode(fixup.encode()).decode()
    return (
        f'bash -c "echo {blob} | base64 -d > /tmp/msb_fixup.sh && '
        'bash /tmp/msb_fixup.sh"'
    )


class PayloadImageBase(Image):
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
        return _NODE_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return _render(
            _BASE_DOCKERFILE,
            base_image=self.dependency(),
            org=self.pr.org,
            repo=self.pr.repo,
            apt_packages=_APT_PACKAGES,
            mongo=_MONGO_SERIES,
            pnpm=_PNPM_VERSION,
        )


class PayloadImageDefault(Image):
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
        return PayloadImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "strip_binary_diffs.py", _STRIP_BINARY_DIFFS_PY),
            File(".", "select-e2e.sh", _render(_SELECT_E2E_SH, repo=repo)),
            File(
                ".",
                "run-e2e.sh",
                _render(
                    _RUN_E2E_SH,
                    repo=repo,
                    grep=shlex.quote(_grep_pattern(self.pr.test_patch)),
                ),
            ),
            File(
                ".",
                "prepare.sh",
                _render(
                    _PREPARE_SH,
                    repo=repo,
                    sha=self.pr.base.sha,
                    number=self.pr.number,
                ),
            ),
            File(
                ".",
                "run.sh",
                _render(_STAGE_SH, repo=repo, patch_step="echo '=== RUN stage: no patches applied ==='"),
            ),
            File(
                ".",
                "test-run.sh",
                _render(_STAGE_SH, repo=repo, patch_step=_APPLY_TEST_PATCH),
            ),
            File(
                ".",
                "fix-run.sh",
                _render(_STAGE_SH, repo=repo, patch_step=_APPLY_BOTH_PATCHES),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        prepare_names = {"check_git_changes.sh", "prepare.sh"}
        prepare_copies = "".join(
            f"COPY {file.name} /home/\n"
            for file in self.files()
            if file.name in prepare_names
        )
        graded_copies = "".join(
            f"COPY {file.name} /home/\n"
            for file in self.files()
            if file.name not in prepare_names
        )
        return _render(
            _PR_DOCKERFILE,
            base_image=base.image_full_name(),
            prepare_copies=prepare_copies,
            graded_copies=graded_copies,
            repo=self.pr.repo,
            sha=self.pr.base.sha,
        )


@Instance.register("payloadcms", _INTERVAL_NAME)
class PAYLOAD_12530_TO_13298(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PayloadImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or _stage_command(self.pr.repo, "run.sh", self.pr.test_patch)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or _stage_command(self.pr.repo, "test-run.sh", self.pr.test_patch)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or _stage_command(self.pr.repo, "fix-run.sh", self.pr.test_patch)

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        current_file = ""

        def classify(mark: str, name: str) -> None:
            name = _canonical_name(name)
            if not name:
                return
            if mark in _MARK_PASS:
                passed_tests.add(name)
            elif mark in _MARK_FAIL:
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        for raw_line in _normalize_lines(test_log):
            line = raw_line.strip()
            if not line:
                continue

            header = _JEST_FILE_RE.match(line)
            if header:
                current_file = header.group("file")
                continue

            match = _PLAYWRIGHT_RE.match(line)
            if match:
                classify(match.group("mark"), match.group("name"))
                continue

            match = _UNIT_RE.match(line)
            if match:
                name = match.group("name")
                looks_like_path = bool(re.match(r"^[\w./@-]+\.[cm]?[jt]sx?\b", name))
                if current_file and not looks_like_path:
                    name = f"{current_file} › {name}"
                classify(match.group("mark"), name)
                continue

        failed_tests -= passed_tests
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