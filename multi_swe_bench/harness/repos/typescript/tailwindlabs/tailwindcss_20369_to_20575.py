"""tailwindlabs/tailwindcss -- v4.3 era (PRs #20369..#20575, base branch `main`).

Era toolchain (identical at every base.sha in this interval):
    node 24 (CI NODE_VERSION) . pnpm 11.9.0 (packageManager) . vitest 4
    Rust 1.95.0 (rust-toolchain.toml) for the native `@tailwindcss/oxide` addon.

SHARED BASE per era (tag `base-20369_to_20575`): clones full history, installs
the toolchain; carries NO base commit and NO history scrub. Each PR image pins
its own base.sha in prepare.sh, installs + builds, then scrubs git history.

Routing: JSONL rows carry number_interval = "tailwindcss_20369_to_20575".
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "tailwindcss_20369_to_20575"
BASE_TAG = "base-20369_to_20575"
NODE_IMAGE = "node:24-bookworm"
PNPM_VERSION = "11.9.0"
RUST_TOOLCHAIN = "1.95.0"

CHECK_GIT_CHANGES = """#!/bin/bash
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

ENV_BLOCK = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096
export CARGO_HOME=/usr/local/cargo
export RUSTUP_HOME=/usr/local/rustup
export PATH=/usr/local/cargo/bin:$PATH
"""

RUN_HEADER = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
"""
)

# Binary hunks without a payload cannot be applied; exclude them explicitly so
# the remaining hunks still apply (no `|| true` -- a real conflict must fail).
APPLY_FN = r"""
apply_patches() {
    local excl=() f
    for f in $(awk '/^diff --git /{p=$3; sub(/^a\//,"",p)} /^Binary files .* differ$/{if(p!="")print p}' "$@" | sort -u); do
        excl+=("--exclude=$f")
        echo "apply_patches: excluding $f (binary change with no payload in the patch)"
    done
    git apply --whitespace=nowarn "${excl[@]}" "$@"
}
"""

INSTALL_CMD = "pnpm install --frozen-lockfile"

# Only fires when a patch touches a manifest/lockfile (none of the current
# interval does); keeps node_modules consistent with the patched manifests.
REINSTALL_BLOCK = """
if { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | grep -qE '(^|/)package\\.json$|^pnpm-lock\\.yaml$|^pnpm-workspace\\.yaml$'; then
    pnpm install --no-frozen-lockfile
fi
"""

# Published `@tailwindcss/oxide` release whose Rust sources are closest to each
# base commit -- used ONLY on amd64 (see OXIDE_BUILD). Measured with
# `git diff --shortstat <tag> <sha> -- crates/oxide crates/node/src Cargo.lock`;
# crates/node/src (the N-API surface) is byte-identical in all four and in both
# releases, so the JS<->native API always matches:
#   e8f8b19c (#20575)            v4.3.4  identical
#   9833b487/dfddfcd5 (#20561/9) v4.3.4  differs only by v4.3.4's extra pug.rs pre-processor
#   f6b7c581 (#20369)            v4.3.3  differs in scanner/mod.rs (no #20369 test loads oxide)
# Unlisted SHAs fall back to the version in crates/node/package.json.
_OXIDE_PREBUILT_AMD64 = {
    "e8f8b19c4c2d36eadb8cc0f5a58bf6529d3e76bf": "4.3.4",
    "9833b487a8b94fa6218b0daaea638354ed5fdefe": "4.3.4",
    "dfddfcd5de5837305d8e2e3effb0adcd14fc9262": "4.3.4",
    "f6b7c5818e2ee7b6aa33bc96e206919fb41617b2": "4.3.3",
}

# Native addon, then every JS package except @tailwindcss/standalone (a bun
# single-binary bundler no unit suite imports; bun is unreliable under emulation).
#   arm64 (native): compile the addon from THIS commit (platform target only).
#   amd64: rustc segfaults under QEMU user-mode emulation on the arm64 build host
#     (`rustc --version` -> SIGSEGV), so drop in the published napi binary + loader
#     of the closest release instead. All three files are gitignored in crates/node,
#     and the loader prefers the local ./tailwindcss-oxide.linux-x64-gnu.node.
OXIDE_BUILD = """case "$(uname -m)" in
    aarch64|arm64)
        rustc --version
        pnpm --filter @tailwindcss/oxide run build:platform
        ;;
    x86_64)
        OXIDE_VER="__OXIDE_VER__"
        [ -n "$OXIDE_VER" ] || OXIDE_VER="$(node -p "require('./crates/node/package.json').version")"
        echo "amd64: using published @tailwindcss/oxide@${OXIDE_VER} binary (no rustc under emulation)"
        OXIDE_TMP="$(mktemp -d)"
        (cd "$OXIDE_TMP" && npm pack --silent "@tailwindcss/oxide@${OXIDE_VER}" "@tailwindcss/oxide-linux-x64-gnu@${OXIDE_VER}")
        mkdir -p "$OXIDE_TMP/loader" "$OXIDE_TMP/bin"
        tar -xzf "$OXIDE_TMP/tailwindcss-oxide-${OXIDE_VER}.tgz" -C "$OXIDE_TMP/loader"
        tar -xzf "$OXIDE_TMP/tailwindcss-oxide-linux-x64-gnu-${OXIDE_VER}.tgz" -C "$OXIDE_TMP/bin"
        cp "$OXIDE_TMP/loader/package/index.js" "$OXIDE_TMP/loader/package/index.d.ts" crates/node/
        cp "$OXIDE_TMP/bin/package/tailwindcss-oxide.linux-x64-gnu.node" crates/node/
        cp "$OXIDE_TMP/bin/package/tailwindcss-oxide.linux-x64-gnu.node" crates/node/npm/linux-x64-gnu/
        rm -rf "$OXIDE_TMP"
        ;;
    *)
        echo "unsupported arch: $(uname -m)"
        exit 1
        ;;
esac
"""

BUILD_CMD = (
    OXIDE_BUILD
    + """pnpm --recursive --filter './packages/*' --filter '!@tailwindcss/standalone' run build
"""
)

PREPARE_SH = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
    git fetch origin "__SHA__"
fi
git checkout --detach "__SHA__"
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

node --version
pnpm --version
"""
    + INSTALL_CMD
    + "\n"
    + BUILD_CMD
    + """
pnpm exec vitest --version
node -e "require('./crates/node'); require.resolve('vitest')"
node -e "const r = require('module').createRequire(require('path').resolve('packages/tailwindcss/package.json')); for (const m of ['lightningcss', 'dedent']) r(m)"
test -f packages/tailwindcss/dist/lib.js
echo "DEPS_OK"

"""
)


def _test_targets(test_patch: str) -> str:
    """Unit-test files the test patch touches.

    Excluded: `integrations/**` and `*.spec.*` -- the root vitest.config.mts
    excludes both (integrations spawn real CLIs/bundlers + need network;
    `.spec` files are Playwright UI tests). Snapshot `.snap` files are data,
    not test modules. When nothing remains, run the whole unit suite.
    """
    test_re = re.compile(r"\.test\.(?:ts|tsx|js|jsx|mts|mjs|cts|cjs)$")
    out: list[str] = []
    for path in re.findall(r"^diff --git a/(\S+) b/", test_patch, re.M):
        if path.startswith("integrations/") or not test_re.search(path):
            continue
        if path not in out:
            out.append(path)
    return " ".join(out)


TEST_BLOCK = """
pnpm exec vitest run --hideSkippedTests --reporter=verbose __TESTS__

"""

RUN_SH = RUN_HEADER + TEST_BLOCK

TEST_RUN_SH = (
    RUN_HEADER + APPLY_FN + "apply_patches /home/test.patch\n" + REINSTALL_BLOCK + TEST_BLOCK
)

FIX_RUN_SH = (
    RUN_HEADER
    + APPLY_FN
    + "apply_patches /home/test.patch /home/fix.patch\n"
    + REINSTALL_BLOCK
    + TEST_BLOCK
)


class TailwindcssImageBase_20369_TO_20575(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6
FROM {NODE_IMAGE}

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
    CI=true \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    CARGO_HOME=/usr/local/cargo \\
    RUSTUP_HOME=/usr/local/rustup \\
    PATH=/usr/local/cargo/bin:$PATH \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image (node24 pnpm11 vitest4 rust1.95 era)" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \\
        if [ -f "$f" ]; then sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "$f"; fi; \\
    done && \\
    apt-get -o Acquire::Retries=5 update && \\
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\
    bash \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    pkg-config \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

# Rust toolchain pinned to rust-toolchain.toml -- arm64 only. On amd64 the image
# is built under QEMU on an arm64 host, where rustc segfaults; prepare.sh uses the
# published oxide binary there instead, so no Rust toolchain is needed.
RUN set -eux; \\
    case "${{TARGETARCH:-$(dpkg --print-architecture)}}" in \\
        arm64) RUST_TRIPLE=aarch64-unknown-linux-gnu ;; \\
        amd64) echo "amd64: Rust toolchain not installed (prebuilt oxide binary used)"; exit 0 ;; \\
        *) echo "unsupported arch: ${{TARGETARCH}}"; exit 1 ;; \\
    esac; \\
    curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/rustup-init \\
        "https://static.rust-lang.org/rustup/dist/${{RUST_TRIPLE}}/rustup-init"; \\
    chmod +x /tmp/rustup-init; \\
    /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain {RUST_TOOLCHAIN}; \\
    rm -f /tmp/rustup-init; \\
    chmod -R a+w "$RUSTUP_HOME" "$CARGO_HOME"; \\
    rustc --version; cargo --version

RUN npm install -g pnpm@{PNPM_VERSION} && pnpm --version

RUN git config --global --add safe.directory '*'

RUN node --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class TailwindcssImageDefault_20369_TO_20575(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return TailwindcssImageBase_20369_TO_20575(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, script: str) -> str:
        return (
            script.replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
            .replace("__TESTS__", _test_targets(self.pr.test_patch))
            .replace("__OXIDE_VER__", _OXIDE_PREBUILT_AMD64.get(self.pr.base.sha, ""))
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._render(PREPARE_SH)),
            File(".", "run.sh", self._render(RUN_SH)),
            File(".", "test-run.sh", self._render(TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
    git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git remote)"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        cd /home/{self.pr.repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("tailwindlabs", _INTERVAL)
class TAILWINDCSS_20369_TO_20575(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TailwindcssImageDefault_20369_TO_20575(self.pr, self._config)

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
        # Vitest 4 `--reporter=verbose`, one line per test:
        #   ✓ |tailwindcss| src/ast.test.ts > describe > test 3ms
        #   × |tailwindcss| src/ast.test.ts > describe > test 12ms
        #   ↓ |tailwindcss| src/ast.test.ts > describe > test   (skipped/todo)
        # The `|project| file > suite > test` path makes names unique across
        # the monorepo; the trailing duration (and any retry/heap suffix) is
        # stripped so the name is identical in all three stages.
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        line_re = re.compile(r"^\s*(?P<mark>[✓✔√×✗✕✖↓])\s+(?P<name>\|[^|]+\|\s+\S+\s+>\s+.+)$")
        suffix_re = re.compile(
            r"(?:\s+\(retry x\d+\))?(?:\s+\d+(?:\.\d+)?\s*m?s)?"
            r"(?:\s+\d+(?:\.\d+)?\s*MB heap used)?\s*$"
        )
        # `test.each` with a constant title (e.g. index.test.ts
        # "@custom-variant must have a valid name", 12 cases) prints identical
        # lines; number repeats in output order so no case is collapsed.
        # Order within a file is deterministic, so names match across stages.
        seen: dict[str, int] = {}

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).rstrip()
            m = line_re.match(line)
            if not m:
                continue
            name = suffix_re.sub("", m.group("name")).strip()
            if not name:
                continue
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [{seen[name]}]"
            mark = m.group("mark")
            if mark in "✓✔√":
                passed_tests.add(name)
            elif mark == "↓":
                skipped_tests.add(name)
            else:
                failed_tests.add(name)

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
