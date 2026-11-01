import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_INTERVAL = "cc_switch_6780_to_7892"
_RUST_IMAGE = "rust:1.95-bookworm"
_NODE_IMAGE = "node:22.12.0-bookworm"
_RUST_TOOLCHAIN = "1.95"
_PNPM_VERSION = "10.12.3"
_SCCACHE_VERSION = "v0.10.0"
_NEXTEST_VERSION = "0.9.150"
_MANIFEST = "src-tauri/Cargo.toml"

_RUN_ENV = """export CI=true
export NO_COLOR=1
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export CARGO_HTTP_TIMEOUT=120
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C linker-features=-lld"
export RUST_BACKTRACE=1"""

_TEST_COMMANDS = f"""rc=0
pnpm exec vitest run --reporter=verbose --slowTestThreshold=2147483647 || rc=$?
cargo nextest run --manifest-path {_MANIFEST} --locked --no-fail-fast --test-threads 1 --status-level all --final-status-level none --color never || rc=$?
exit "$rc\""""


class CcSwitch6780To7892ImageBase(Image):
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
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_RUST_IMAGE}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CARGO_HTTP_CAINFO=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential pkg-config libssl-dev \\
    libgtk-3-dev librsvg2-dev libayatana-appindicator3-dev \\
    libwebkit2gtk-4.1-dev libsoup-3.0-dev \\
    && rm -rf /var/lib/apt/lists/*

COPY --from={_NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from={_NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \\
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \\
    node --version && npm --version

RUN npm install -g pnpm@{_PNPM_VERSION} && \\
    pnpm config set store-dir /root/.pnpm-store --global && \\
    pnpm --version

RUN rustup toolchain install {_RUST_TOOLCHAIN} --profile minimal --component rustfmt --component clippy && \\
    rustup run {_RUST_TOOLCHAIN} rustc --version

RUN case "${{TARGETARCH}}" in amd64) target=x86_64-unknown-linux-gnu ;; arm64) target=aarch64-unknown-linux-gnu ;; *) echo "unsupported TARGETARCH=${{TARGETARCH}}"; exit 1 ;; esac && \\
    curl -fsSL --retry 5 -o /tmp/nextest.tar.gz "https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-{_NEXTEST_VERSION}/cargo-nextest-{_NEXTEST_VERSION}-$target.tar.gz" && \\
    tar -xzf /tmp/nextest.tar.gz -C /usr/local/cargo/bin && \\
    rm -f /tmp/nextest.tar.gz && \\
    cargo nextest --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

RUN cd /home/{self.pr.repo} && \\
    (cargo +{_RUST_TOOLCHAIN} fetch --manifest-path {_MANIFEST} --locked && pnpm fetch --frozen-lockfile || true) && \\
    git clean -fdx && \\
    git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class CcSwitch6780To7892ImageDefault(Image):
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
        return CcSwitch6780To7892ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{_RUN_ENV}
TOOLCHAIN=$(sed -nE 's/^channel *= *"([^"]+)".*/\\1/p' rust-toolchain.toml)
test -n "$TOOLCHAIN"
if [[ "$(rustup toolchain list)" != *"$TOOLCHAIN-"* ]]; then
  rustup toolchain install "$TOOLCHAIN" --profile minimal --component rustfmt --component clippy
fi
[[ "$(rustup show active-toolchain)" == "$TOOLCHAIN-"* ]]
rustc --version
test "$(node --version)" = "v$(tr -d '[:space:]v' < .node-version)"
PNPM_VERSION=$(node -p "require('./package.json').packageManager.split('@')[1].split('+')[0]")
if [ "$(pnpm --version)" != "$PNPM_VERSION" ]; then
  npm install -g "pnpm@$PNPM_VERSION"
fi
test "$(pnpm --version)" = "$PNPM_VERSION"
pnpm install --frozen-lockfile --prefer-offline
mkdir -p dist
cargo fetch --manifest-path {_MANIFEST} --locked

cargo nextest --version
pnpm exec vitest --version
node -e "require.resolve('vitest'); require.resolve('jsdom'); require.resolve('@testing-library/react'); require.resolve('@vitejs/plugin-react'); const r = require('module').createRequire(require.resolve('vite/package.json')); r('esbuild').transformSync('let a = 1'); r('rollup')"
cargo nextest run --manifest-path {_MANIFEST} --locked --offline --no-run
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_RUN_ENV}

cd /home/{repo}
{_TEST_COMMANDS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_RUN_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{_TEST_COMMANDS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_RUN_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_TEST_COMMANDS}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        prepare_files = ("check_git_changes.sh", "prepare.sh")
        prepare_copy = "".join(f"COPY {file.name} /home/\n" for file in self.files() if file.name in prepare_files)
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files() if file.name not in prepare_files)

        return f"""FROM {base.image_full_name()}

ARG TARGETARCH

{prepare_copy}
RUN --mount=type=cache,id=cc-switch-sccache-${{TARGETARCH}},target=/root/.cache/sccache,sharing=locked \\
    set -eux; \\
    case "$(uname -m)" in x86_64) arch=x86_64; port=4227 ;; aarch64) arch=aarch64; port=4226 ;; *) arch=""; port=4225 ;; esac; \\
    mkdir -p /tmp/sccache-bin; \\
    ok=0; \\
    if [ -n "$arch" ] && curl -fsSL --retry 5 -o /tmp/sccache.tar.gz "https://github.com/mozilla/sccache/releases/download/{_SCCACHE_VERSION}/sccache-{_SCCACHE_VERSION}-$arch-unknown-linux-musl.tar.gz" \\
        && tar -xzf /tmp/sccache.tar.gz -C /tmp/sccache-bin --strip-components=1; then \\
        export SCCACHE_DIR=/root/.cache/sccache SCCACHE_CACHE_SIZE=20G SCCACHE_IDLE_TIMEOUT=0 SCCACHE_SERVER_PORT=$port; \\
        for i in 1 2 3 4 5; do \\
            if /tmp/sccache-bin/sccache --start-server && /tmp/sccache-bin/sccache --show-stats >/dev/null; then ok=1; break; fi; \\
            /tmp/sccache-bin/sccache --stop-server || true; sleep 5; \\
        done; \\
    fi; \\
    if [ "$ok" = 1 ]; then export RUSTC_WRAPPER=/tmp/sccache-bin/sccache; fi; \\
    bash /home/prepare.sh; \\
    if [ "$ok" = 1 ]; then /tmp/sccache-bin/sccache --show-stats || true; /tmp/sccache-bin/sccache --stop-server || true; fi; \\
    rm -rf /tmp/sccache-bin /tmp/sccache.tar.gz

{copy_commands}
RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("farion1231", _INTERVAL)
class CC_SWITCH_6780_TO_7892(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CcSwitch6780To7892ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        vitest = re.compile(
            r"^\s*([✓×↓])\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?\s>\s.+?)(?:\s+\d+ms)?$"
        )
        nextest = re.compile(
            r"^\s*(?:TRY \d+ )?(PASS|LEAK-FAIL|LEAK|FAIL|SKIP|TIMEOUT|ABORT|SIG[A-Z]+)\s+\[[^\]]*\]\s+(?:\([^)]*\)\s+)?(\S+)\s+(\S.*?)\s*$"
        )

        for line in log.splitlines():
            line = line.rstrip()
            found = vitest.match(line)
            if found:
                name = found.group(2)
                status = found.group(1)
                if status == "✓":
                    passed_tests.add(name)
                elif status == "×":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue
            found = nextest.match(line)
            if not found:
                continue
            status, binary, test = found.groups()
            package, _, kind = binary.partition("::")
            if not kind:
                target = f"src-tauri[{package}]"
            elif kind.startswith("bin/"):
                target = f"src-tauri[{kind}]"
            else:
                target = f"src-tauri/tests/{kind}.rs"
            name = f"{target}::{test}"
            if status in ("PASS", "LEAK"):
                passed_tests.add(name)
            elif status == "SKIP":
                skipped_tests.add(name)
            else:
                failed_tests.add(name)

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
