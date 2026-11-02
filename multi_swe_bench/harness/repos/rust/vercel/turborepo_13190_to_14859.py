from __future__ import annotations

import json
import os
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24-bookworm"
_BASE_TAG = "base-13190_to_14859"
_RUSTUP_VERSION = "1.28.2"
_RUSTUP_SHA256_AMD64 = "20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c"
_RUSTUP_SHA256_ARM64 = "e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c"
_RUST_TOOLCHAINS = ("nightly-2026-04-10", "nightly-2026-05-22", "nightly-2026-07-03")
_NEXTEST_VERSION = "0.9.148"
_NEXTEST_SHA256_AMD64 = "223ba936714cc861a9cdedad5a8eb664dfdd69d6fbdf6cea6aa8184f523290dc"
_NEXTEST_SHA256_ARM64 = "81469eb963ef883fd590e60189e77d6aba3d91b418591d3391f50fe3e9480d2b"
_BUN_VERSION = "1.4.3"
_BUN_SHA256_AMD64 = "1fc2edac843102909e3a1be1d8d9802cc6071cf074e67e88231f4ffe0f8b397b"
_BUN_SHA256_ARM64 = "efa9813da5ed72423bf847f916e8d2c47c0d776add972354026a75e10da9aa21"
_ZIG_SHA256 = {
    ("0.15.2", "aarch64"): "958ed7d1e00d0ea76590d27666efbf7a932281b3d7ba0c6b01b0ff26498f667f",
    ("0.15.2", "x86_64"): "02aa270f183da276e5b5920b1dac44a63f1a49e55050ebde3aecc9eb82f93239",
    ("0.17.0", "aarch64"): "9e8d11661d4ae3bd57702a3832781e23ad151dde5798e16a5ccd503f65234ff8",
    ("0.17.0", "x86_64"): "1cbe9df9f27e6b78d14ccbca43b6703a404ef79ef1c463de901d7f088d4e2026",
}
_ZIG_CASES = "\n".join(
    f"    {version}-{arch}) zig_sum={digest} ;;" for (version, arch), digest in _ZIG_SHA256.items()
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_BIN_RE = re.compile(r"^=== MSB_BIN: (\S+) (\S+) ===$")
_NEXTEST_RE = re.compile(
    r"^(PASS|FLAKY|LEAK|LEAK-FAIL|FAIL|EXEC FAIL|TIMEOUT|ABORT|SIG[A-Z]+|SKIP)"
    r"\s+\[[^\]]*\]\s+(?:\(\s*\d+/\s*\d+\)\s+)?(\S+)\s+(\S.*?)\s*$"
)
_NEXTEST_PASSED = {"PASS", "FLAKY", "LEAK"}
_JEST_START = "=== MSB_JEST_JSON ==="
_JEST_END = "=== MSB_JEST_END ==="

_MEMBERS_JQ = r'.workspace_root as $r | .workspace_members as $m | .packages[] | select(.id as $i | $m | index($i)) | "\(.name) \(.manifest_path | ltrimstr($r + "/") | rtrimstr("/Cargo.toml"))"'
_BINMAP_JQ = r'.workspace_root as $r | .workspace_members as $m | .packages[] | select(.id as $i | $m | index($i)) | .name as $n | .targets[] | (if (.kind | index("test")) then "\($n)::\(.name)" elif (.kind | index("bin")) then "\($n)::bin/\(.name)" elif (.kind | index("bench")) then "\($n)::bench/\(.name)" elif (.kind | index("example")) then "\($n)::example/\(.name)" elif (.kind | index("custom-build")) then empty else $n end) as $id | "=== MSB_BIN: \($id) \(.src_path | ltrimstr($r + "/")) ==="'
_FILES_AWK = r'/^diff --git a\// { sub(/^a\//, "", $3); print $3 }'
_CRATES_AWK = r'NR == FNR { f[$0]; next } { for (p in f) if (index(p, $2 "/") == 1) { print; break } }'
_JEST_AWK = r'$1 == "packages" && $NF ~ /\.test\.[cm]?[jt]sx?$/ { print $1 "/" $2 }'

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""


def _test_cmds(repo: str) -> str:
    return f"""cd /home/{repo}
fatal=0

awk '{_FILES_AWK}' /home/test.patch | sort -u > /tmp/msb-test-files.txt
cargo metadata --offline --no-deps --format-version 1 | jq -r '{_MEMBERS_JQ}' | sort > /tmp/msb-members.txt
awk '{_CRATES_AWK}' /tmp/msb-test-files.txt /tmp/msb-members.txt > /tmp/msb-crates.txt
awk -F/ '{_JEST_AWK}' /tmp/msb-test-files.txt | sort -u > /tmp/msb-jest.txt
[ -s /tmp/msb-crates.txt ] || [ -s /tmp/msb-jest.txt ] || {{ echo "=== MSB: no test targets found in test.patch ==="; exit 1; }}
cargo metadata --offline --no-deps --format-version 1 | jq -r '{_BINMAP_JQ}'

while read -r pkg dir; do
  echo "=== MSB_CRATE: $dir ==="
  rc=0
  cargo nextest run --offline -p "$pkg" --no-fail-fast < /dev/null 2>&1 || rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 100 ] || [ "$rc" -eq 101 ] || {{ echo "=== MSB: cargo nextest run -p $pkg exited with status $rc ==="; fatal=$rc; }}
done < /tmp/msb-crates.txt

while read -r dir; do
  echo "=== MSB_JEST: $dir ==="
  rm -f /tmp/msb-jest.json
  rc=0
  pnpm --dir "$dir" exec jest --ci --coverage=false --passWithNoTests --json --outputFile=/tmp/msb-jest.json < /dev/null 2>&1 || rc=$?
  [ "$rc" -le 1 ] || {{ echo "=== MSB: jest in $dir exited with status $rc ==="; fatal=$rc; }}
  if [ -s /tmp/msb-jest.json ]; then
    echo "{_JEST_START}"
    cat /tmp/msb-jest.json
    echo
    echo "{_JEST_END}"
  else
    echo "=== MSB: jest in $dir wrote no results ==="
    fatal=1
  fi
done < /tmp/msb-jest.txt

exit "$fatal"
"""


class Turborepo13190To14859ImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/80-retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential pkg-config clang libclang-dev cmake \\
    protobuf-compiler libprotobuf-dev libssl-dev capnproto jq xz-utils procps unzip \\
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    RUSTUP_AUTO_INSTALL=0 \\
    PATH=/usr/local/cargo/bin:$PATH

RUN set -eu; \\
    case "$(dpkg --print-architecture)" in \\
      amd64) triple=x86_64-unknown-linux-gnu; sum={_RUSTUP_SHA256_AMD64} ;; \\
      arm64) triple=aarch64-unknown-linux-gnu; sum={_RUSTUP_SHA256_ARM64} ;; \\
      *) echo "unsupported architecture $(dpkg --print-architecture)"; exit 1 ;; \\
    esac; \\
    curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/rustup-init "https://static.rust-lang.org/rustup/archive/{_RUSTUP_VERSION}/$triple/rustup-init"; \\
    echo "$sum  /tmp/rustup-init" | sha256sum -c -; \\
    chmod +x /tmp/rustup-init; \\
    /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none; \\
    rm -f /tmp/rustup-init; \\
    rustup set auto-self-update disable; \\
    rustup --version | grep -q "^rustup {_RUSTUP_VERSION} "

RUN set -eu; \\
    rustup toolchain install {" ".join(_RUST_TOOLCHAINS)} --profile minimal --component rustfmt --component clippy; \\
    for toolchain in {" ".join(_RUST_TOOLCHAINS)}; do \\
      rustup toolchain list | grep -q "^$toolchain-" || {{ echo "toolchain $toolchain missing"; exit 1; }}; \\
    done

RUN set -eu; \\
    case "$(dpkg --print-architecture)" in \\
      amd64) triple=x86_64-unknown-linux-gnu; sum={_NEXTEST_SHA256_AMD64} ;; \\
      arm64) triple=aarch64-unknown-linux-gnu; sum={_NEXTEST_SHA256_ARM64} ;; \\
      *) echo "unsupported architecture $(dpkg --print-architecture)"; exit 1 ;; \\
    esac; \\
    curl --retry 5 -fsSL -o /tmp/nextest.tar.gz "https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-{_NEXTEST_VERSION}/cargo-nextest-{_NEXTEST_VERSION}-$triple.tar.gz"; \\
    echo "$sum  /tmp/nextest.tar.gz" | sha256sum -c -; \\
    tar -xzf /tmp/nextest.tar.gz -C /usr/local/cargo/bin; \\
    rm -f /tmp/nextest.tar.gz; \\
    cargo-nextest nextest --version | grep -q "^cargo-nextest {_NEXTEST_VERSION} "

RUN set -eu; \\
    case "$(dpkg --print-architecture)" in \\
      amd64) bun_pkg=bun-linux-x64-baseline; sum={_BUN_SHA256_AMD64} ;; \\
      arm64) bun_pkg=bun-linux-aarch64; sum={_BUN_SHA256_ARM64} ;; \\
      *) echo "unsupported architecture $(dpkg --print-architecture)"; exit 1 ;; \\
    esac; \\
    curl --retry 5 -fsSL -o /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v{_BUN_VERSION}/$bun_pkg.zip"; \\
    echo "$sum  /tmp/bun.zip" | sha256sum -c -; \\
    unzip -q /tmp/bun.zip -d /tmp; \\
    install -m 0755 "/tmp/$bun_pkg/bun" /usr/local/bin/bun; \\
    rm -rf /tmp/bun.zip "/tmp/$bun_pkg"; \\
    [ "$(bun --version)" = "{_BUN_VERSION}" ]

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Turborepo13190To14859ImageDefault(Image):
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
        return Turborepo13190To14859ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "{self.pr.base.sha}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "{self.pr.base.sha}"
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

export CARGO_NET_RETRY=10 CARGO_HTTP_TIMEOUT=120 CARGO_BUILD_JOBS=6

toolchain="$(sed -n 's/^channel *= *"\\(.*\\)"/\\1/p' rust-toolchain.toml | head -n 1)"
[ -n "$toolchain" ] || {{ echo "prepare.sh: no channel in rust-toolchain.toml"; exit 1; }}
rustup toolchain list | grep -q "^$toolchain-" || {{ echo "prepare.sh: toolchain $toolchain is not in the base image"; exit 1; }}
rustup default "$toolchain"
rustup show active-toolchain | grep -q "^$toolchain-" || {{ echo "prepare.sh: toolchain $toolchain is not active"; exit 1; }}

zig_action=.github/actions/setup-zig/action.yml
if [ -f "$zig_action" ]; then
  zig_version="$(awk '$1 == "version:" {{ v = 1 }} v && $1 == "default:" {{ gsub(/["\\047]/, "", $2); print $2; exit }}' "$zig_action")"
  [ -n "$zig_version" ] || {{ echo "prepare.sh: no zig version in $zig_action"; exit 1; }}
  case "$zig_version-$(uname -m)" in
{_ZIG_CASES}
    *) echo "prepare.sh: no pinned checksum for zig $zig_version on $(uname -m)"; exit 1 ;;
  esac
  zig_tar="zig-$(uname -m)-linux-$zig_version.tar.xz"
  curl --retry 5 -fsSL -o "/tmp/$zig_tar" "https://ziglang.org/download/$zig_version/$zig_tar"
  echo "$zig_sum  /tmp/$zig_tar" | sha256sum -c -
  mkdir -p /usr/local/zig
  tar -xJf "/tmp/$zig_tar" -C /usr/local/zig --strip-components 1
  rm -f "/tmp/$zig_tar"
  [ "$(zig version)" = "$zig_version" ] || {{ echo "prepare.sh: zig $zig_version is not active"; exit 1; }}
fi

package_manager="$(jq -r '.packageManager // empty' package.json)"
[ -n "$package_manager" ] || {{ echo "prepare.sh: no packageManager in package.json"; exit 1; }}
corepack enable
corepack prepare "$package_manager" --activate
pnpm --version | grep -qx "${{package_manager#pnpm@}}" || {{ echo "prepare.sh: $package_manager is not active"; exit 1; }}
pnpm install --frozen-lockfile --ignore-scripts

cargo fetch --locked
cargo metadata --offline --no-deps --format-version 1 | jq -r '{_MEMBERS_JQ}' | sort > /tmp/msb-members.txt
[ -s /tmp/msb-members.txt ] || {{ echo "prepare.sh: no workspace packages found"; exit 1; }}
cargo test --workspace --offline --locked --no-run < /dev/null || {{ echo "prepare.sh: cargo test --no-run failed"; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
{_RUN_HEADER}

{_test_cmds(self.pr.repo)}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "test-run.sh: git apply failed" >&2; exit 1; }}
{_test_cmds(self.pr.repo)}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "fix-run.sh: git apply failed" >&2; exit 1; }}
{_test_cmds(self.pr.repo)}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("Turborepo13190To14859ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}} \\
    CARGO_INCREMENTAL=0 \\
    CARGO_TERM_COLOR=never \\
    CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=gcc \\
    RUSTFLAGS="-C debuginfo=0" \\
    RUST_BACKTRACE=1 \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    TURBO_TELEMETRY_DISABLED=1 \\
    DO_NOT_TRACK=1 \\
    INSTA_UPDATE=no \\
    INSTA_WORKSPACE_ROOT=/home/{self.pr.repo} \\
    PATH=/usr/local/zig:$PATH

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("vercel", "turborepo_13190_to_14859")
class TURBOREPO_13190_TO_14859(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Turborepo13190To14859ImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        jest_buckets = {
            "passed": passed,
            "failed": failed,
            "pending": skipped,
            "skipped": skipped,
            "todo": skipped,
            "disabled": skipped,
        }
        root = f"/home/{self.pr.repo}"

        binaries: dict[str, str] = {}
        jest_lines: list[str] | None = None
        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()

            if jest_lines is not None:
                if line != _JEST_END:
                    jest_lines.append(raw)
                    continue
                try:
                    report = json.loads("".join(jest_lines))
                except ValueError:
                    report = {}
                jest_lines = None
                for suite in report.get("testResults", []):
                    path = os.path.relpath(suite.get("name", ""), root)
                    for case in suite.get("assertionResults", []):
                        bucket = jest_buckets.get(case.get("status", ""))
                        if bucket is not None:
                            bucket.add(f"{path}::{case.get('fullName', '')}")
                continue

            if line == _JEST_START:
                jest_lines = []
                continue

            binary = _BIN_RE.match(line)
            if binary:
                binaries[binary.group(1)] = binary.group(2)
                continue

            result = _NEXTEST_RE.match(line)
            if not result:
                continue

            status, binary_id, name = result.group(1), result.group(2), result.group(3)
            path = binaries.get(binary_id, binary_id)
            if status in _NEXTEST_PASSED:
                bucket = passed
            elif status == "SKIP":
                bucket = skipped
            else:
                bucket = failed
            bucket.add(f"{path}::{name}")

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
