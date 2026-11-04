import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_RUST_VERSION = "1.99"
_RUST_IMAGE = f"rust:{_RUST_VERSION}-bookworm"
_NODE_VERSION = "24.11.0"
_NEXTEST_VERSION = "0.9.150"
_WASM_PACK_VERSION = "0.13.1"
_JUST_VERSION = "1.40.0"
_PLUGIN_DIR = "packages/chrome-plugin"

_RUN_ENV = """export CI=true
export NO_COLOR=1
export PATH=/usr/local/node/bin:$PATH
export RUSTUP_TOOLCHAIN="$(ls /usr/local/rustup/toolchains | head -n1)"
export RUST_BACKTRACE=1
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_BUILD_JOBS=6
export CARGO_NET_RETRY=10
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export DISABLE_WASM_OPT=1"""

_TEST_COMMANDS = f"""rc=0
cargo nextest run --workspace --locked --no-fail-fast --status-level all --final-status-level none --color never || rc=$?
specs=()
while IFS= read -r spec; do
  if [ -f "{_PLUGIN_DIR}/$spec" ]; then
    specs+=("$spec")
  fi
done < <(sed -nE 's#^diff --git a/{_PLUGIN_DIR}/(tests/[^ ]+\\.spec\\.ts) b/.*#\\1#p' /home/test.patch | sort -u)
if [ "${{#specs[@]}}" -gt 0 ]; then
  if just build-chrome-plugin; then
    for spec in "${{specs[@]}}"; do
      echo "=== PLAYWRIGHT SPEC $spec"
      (
        cd {_PLUGIN_DIR}
        env -u WAYLAND_DISPLAY XDG_SESSION_TYPE=x11 xvfb-run --auto-servernum pnpm exec playwright test --headed --project chromium --reporter=list --repeat-each=1 --retries=2 --workers=2 "$spec"
      ) || rc=$?
    done
  else
    rc=1
  fi
fi
exit "$rc\""""


class HarperImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return "base"

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

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class HarperImageDefault(Image):
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
        return HarperImageBase(self.pr, self._config)

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

retry() {{
  local attempt
  for attempt in 1 2 3 4 5; do
    if "$@"; then
      return 0
    fi
    sleep 30
  done
  return 1
}}

fetch_tar() {{
  local url="$1"
  shift
  curl -fsSL --http1.1 --retry 5 --retry-all-errors -o /tmp/download.tar "$url" && tar -xf /tmp/download.tar "$@"
}}

retry apt-get -o Acquire::Retries=5 update
retry apt-get -o Acquire::Retries=5 install -y --no-install-recommends xvfb xauth xz-utils perl pkg-config
rm -rf /var/lib/apt/lists/*

case "$(uname -m)" in
  x86_64) node_arch=x64; rust_arch=x86_64 ;;
  aarch64) node_arch=arm64; rust_arch=aarch64 ;;
  *) echo "unsupported architecture: $(uname -m)"; exit 1 ;;
esac

mkdir -p /usr/local/node
retry fetch_tar "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-$node_arch.tar.xz" --strip-components=1 -C /usr/local/node
PNPM_VERSION=$(node -p "require('./package.json').packageManager.split('@')[1].split('+')[0]")
retry npm install -g "pnpm@$PNPM_VERSION"

retry rustup target add wasm32-unknown-unknown

retry fetch_tar "https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-{_NEXTEST_VERSION}/cargo-nextest-{_NEXTEST_VERSION}-$rust_arch-unknown-linux-gnu.tar.gz" -C /usr/local/cargo/bin
retry fetch_tar "https://github.com/rustwasm/wasm-pack/releases/download/v{_WASM_PACK_VERSION}/wasm-pack-v{_WASM_PACK_VERSION}-$rust_arch-unknown-linux-musl.tar.gz" --strip-components=1 -C /usr/local/cargo/bin --wildcards '*/wasm-pack'
retry fetch_tar "https://github.com/casey/just/releases/download/{_JUST_VERSION}/just-{_JUST_VERSION}-$rust_arch-unknown-linux-musl.tar.gz" -C /usr/local/cargo/bin just

rm -f /tmp/download.tar
retry just build-chrome-plugin
(
  cd {_PLUGIN_DIR}
  retry pnpm exec playwright install --with-deps chromium
)
rm -rf /var/lib/apt/lists/*
retry cargo nextest run --workspace --locked --no-run

test -f {_PLUGIN_DIR}/build/manifest.json
(
  cd {_PLUGIN_DIR}
  pnpm exec playwright --version
  node --input-type=module -e "import {{ chromium }} from 'playwright'; import {{ accessSync }} from 'node:fs'; accessSync(chromium.executablePath());"
)
cargo nextest --version
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
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

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
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("Automattic", "harper")
class HARPER(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HarperImageDefault(self.pr, self._config)

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
        browser_status: dict[str, str] = {}

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        nextest = re.compile(
            r"^\s*(?:TRY \d+ )?(PASS|LEAK-FAIL|LEAK|FAIL|SKIP|TIMEOUT|ABORT|SIG[A-Z]+)\s+\[[^\]]*\]\s+(?:\([^)]*\)\s+)?(\S+)\s+(\S.*?)\s*$"
        )
        playwright = re.compile(
            r"^\s*([✓✘-])\s+\d+\s+\[[^\]]+\]\s+›\s+(\S+?):\d+:\d+\s+›\s+(.+?)(?:\s+\(retry #\d+\))?(?:\s+\((?:[\d.]+\w+\s*)+\))?\s*$"
        )

        spec_marker = re.compile(r"^=== PLAYWRIGHT SPEC (\S+)\s*$")
        current_spec = ""

        for line in log.splitlines():
            found = spec_marker.match(line)
            if found:
                current_spec = found.group(1)
                continue
            found = playwright.match(line)
            if found:
                status, spec, title = found.groups()
                browser_status[f"{_PLUGIN_DIR}/{current_spec or spec} > {title}"] = status
                continue
            found = nextest.match(line)
            if not found:
                continue
            status, binary, test = found.groups()
            package, _, kind = binary.partition("::")
            if not kind:
                target = f"{package}[lib]"
            elif kind.startswith("bin/"):
                target = f"{package}[{kind}]"
            else:
                target = f"{package}/tests/{kind}.rs"
            name = f"{target}::{test}"
            if status in ("PASS", "LEAK"):
                passed_tests.add(name)
            elif status == "SKIP":
                skipped_tests.add(name)
            else:
                failed_tests.add(name)

        for name, status in browser_status.items():
            if status == "✓":
                passed_tests.add(name)
            elif status == "✘":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
