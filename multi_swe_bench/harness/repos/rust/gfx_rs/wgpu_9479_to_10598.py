from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.96.1-trixie"
_BASE_TAG = "base-9479_to_10598"
_RUST_TOOLCHAINS = ("1.93", "1.95", "1.96.1")
_MESA_VERSION = "25.0.7-2+deb13u1"
_VULKAN_SDKS = ("1.4.328.1", "1.4.357.0")
_NEXTEST_VERSION = "0.9.148"
_NEXTEST_SHA256_AMD64 = "223ba936714cc861a9cdedad5a8eb664dfdd69d6fbdf6cea6aa8184f523290dc"
_NEXTEST_SHA256_ARM64 = "81469eb963ef883fd590e60189e77d6aba3d91b418591d3391f50fe3e9480d2b"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_NEXTEST_RE = re.compile(
    r"^(PASS|FLAKY|LEAK|LEAK-FAIL|FAIL|EXEC FAIL|TIMEOUT|ABORT|SIG[A-Z]+|SKIP)"
    r"\s+\[[^\]]*\]\s+(?:\(\s*\d+/\s*\d+\)\s+)?(\S+)\s+(\S.*?)\s*$"
)
_NEXTEST_PASSED = {"PASS", "FLAKY", "LEAK"}
_GPU_TAG_RE = re.compile(r"^\[([^\]]*)\]\s+\[[^\]]*\]\s+(\S+)$")
_SNAPSHOT_RE = re.compile(r"^=== MSB_SNAPSHOT: (PASS|FAIL) (\S+) ===$")
_CTS_RE = re.compile(r"^\[(pass|fail|warn|skip)\] (\S+) \([\d.]+ms\)\. Log:$")

_FILES_AWK = r'/^diff --git a\// { sub(/^a\//, "", $3); print $3 }'
_CTS_AWK = r'/^diff --git / { f = ($3 == "a/cts_runner/test.lst"); next } f && /^\+/ && !/^\+\+\+/ { s = substr($0, 2); sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); if (s == "" || s ~ /^\/\//) next; sub(/^fails-if *\([^)]*\) +/, "", s); print s }'

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""


def _plan_cmds(repo: str) -> str:
    return f"""cd /home/{repo}

awk '{_FILES_AWK}' /home/test.patch | sort -u > /tmp/msb-test-files.txt
: > /tmp/msb-plan.txt
while read -r f; do
  case "$f" in
    naga/tests/*) echo naga >> /tmp/msb-plan.txt ;;
    tests/tests/wgpu-validation/*) echo wgpu-validation >> /tmp/msb-plan.txt ;;
    tests/tests/wgpu-gpu/*) echo wgpu-gpu >> /tmp/msb-plan.txt ;;
    tests/src/*|tests/Cargo.toml) printf 'wgpu-validation\\nwgpu-gpu\\n' >> /tmp/msb-plan.txt ;;
    player/tests/*) echo player >> /tmp/msb-plan.txt ;;
    cts_runner/test.lst) echo cts >> /tmp/msb-plan.txt ;;
  esac
done < /tmp/msb-test-files.txt
sort -u -o /tmp/msb-plan.txt /tmp/msb-plan.txt
grep '^naga/tests/out/' /tmp/msb-test-files.txt > /tmp/msb-snapshots.txt || [ "$?" -eq 1 ]
awk '{_CTS_AWK}' /home/test.patch | sort -u > /tmp/msb-cts.txt
if [ ! -s /tmp/msb-cts.txt ]; then
  grep -vx cts /tmp/msb-plan.txt > /tmp/msb-plan.tmp || [ "$?" -eq 1 ]
  mv /tmp/msb-plan.tmp /tmp/msb-plan.txt
fi
"""


def _suite_args(kind_var: str) -> str:
    return f"""case "${kind_var}" in
    naga) args="-p naga" ;;
    wgpu-validation) args="-p wgpu-test --test wgpu-validation" ;;
    wgpu-gpu) args="-p wgpu-test --test wgpu-gpu" ;;
    player) args="-p player --test player" ;;
  esac"""


def _test_cmds(repo: str) -> str:
    return f"""{_plan_cmds(repo)}
[ -s /tmp/msb-plan.txt ] || {{ echo "=== MSB: no Rust test suites found in test.patch ==="; exit 1; }}

fatal=0
while read -r kind; do
  {_suite_args("kind")}
  echo "=== MSB_SUITE: $kind ==="
  if [ "$kind" = cts ]; then
    rc=0
    cargo build --offline --locked -p cts_runner --bin cts_runner < /dev/null 2>&1 || rc=$?
    [ "$rc" -eq 0 ] || {{ echo "=== MSB: cts_runner build exited with status $rc ==="; continue; }}
    while read -r sel; do
      echo "=== MSB_CTS: $sel ==="
      rc=0
      (cd cts && WGPU_CTS_XTASK=1 DENO_WEBGPU_BACKEND=vulkan ../target/debug/cts_runner ./tools/run_deno --verbose "$sel") < /dev/null 2>&1 || rc=$?
      [ "$rc" -le 1 ] || echo "=== MSB: cts_runner exited with status $rc for $sel ==="
    done < /tmp/msb-cts.txt
    continue
  fi
  if [ "$kind" = wgpu-gpu ]; then
    rc=0
    WGPU_INFO_SAVE_GPUCONFIG_REPORT=1 cargo nextest run --offline --locked --no-fail-fast --all-features -p wgpu-info -E 'binary(wgpu-info)' generate_gpuconfig_report < /dev/null 2>&1 || rc=$?
    [ "$rc" -eq 0 ] && grep -q name .gpuconfig || {{ echo "=== MSB: no GPU adapter found (status $rc) ==="; fatal=1; continue; }}
  fi
  if [ "$kind" = naga ]; then
    rm -rf /tmp/msb-snap && mkdir -p /tmp/msb-snap
    while read -r f; do
      [ -f "$f" ] && mkdir -p "/tmp/msb-snap/$(dirname "$f")" && cp "$f" "/tmp/msb-snap/$f"
    done < /tmp/msb-snapshots.txt
  fi
  rc=0
  cargo nextest run --offline --locked --no-fail-fast --all-features $args < /dev/null 2>&1 || rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 100 ] || [ "$rc" -eq 101 ] || {{ echo "=== MSB: cargo nextest run $args exited with status $rc ==="; fatal=$rc; }}
  if [ "$kind" = naga ] && [ "$rc" -ne 101 ]; then
    while read -r f; do
      [ -f "/tmp/msb-snap/$f" ] || continue
      if [ -f "$f" ] && cmp -s "$f" "/tmp/msb-snap/$f"; then
        echo "=== MSB_SNAPSHOT: PASS $f ==="
      else
        echo "=== MSB_SNAPSHOT: FAIL $f ==="
      fi
    done < /tmp/msb-snapshots.txt
  fi
done < /tmp/msb-plan.txt

exit "$fatal"
"""


class Wgpu9479To10598ImageBase(Image):
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
        return _RUST_IMAGE

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
    git ca-certificates curl build-essential pkg-config cmake ninja-build python3 jq xz-utils procps \\
    libvulkan1 mesa-vulkan-drivers={_MESA_VERSION} \\
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_AUTO_INSTALL=0 \\
    PATH=/opt/vulkan-sdk/current/bin:$PATH

RUN set -eu; \\
    rustup set auto-self-update disable; \\
    for toolchain in {" ".join(_RUST_TOOLCHAINS)}; do \\
      rustup toolchain install "$toolchain" --profile minimal \\
        --component cargo --component rustfmt --component clippy --component rust-analyzer \\
        --target wasm32-unknown-unknown; \\
      rustup toolchain list | grep -q "^$toolchain" || {{ echo "toolchain $toolchain missing"; exit 1; }}; \\
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
    for v in {" ".join(_VULKAN_SDKS)}; do \\
      mkdir -p /tmp/sdk; \\
      for r in SPIRV-Headers SPIRV-Tools SPIRV-Cross; do \\
        mkdir -p "/tmp/sdk/$r"; \\
        curl --retry 5 -fsSL "https://github.com/KhronosGroup/$r/archive/refs/tags/vulkan-sdk-$v.tar.gz" \\
          | tar -xz -C "/tmp/sdk/$r" --strip-components 1; \\
      done; \\
      cmake -S /tmp/sdk/SPIRV-Tools -B /tmp/sdk/bt -G Ninja -DCMAKE_BUILD_TYPE=Release \\
        -DSPIRV-Headers_SOURCE_DIR=/tmp/sdk/SPIRV-Headers -DSPIRV_SKIP_TESTS=ON -DSPIRV_WERROR=OFF \\
        -DCMAKE_INSTALL_PREFIX="/opt/vulkan-sdk/$v"; \\
      cmake --build /tmp/sdk/bt -j6 --target install; \\
      cmake -S /tmp/sdk/SPIRV-Cross -B /tmp/sdk/bc -G Ninja -DCMAKE_BUILD_TYPE=Release \\
        -DSPIRV_CROSS_ENABLE_TESTS=OFF -DCMAKE_INSTALL_PREFIX="/opt/vulkan-sdk/$v"; \\
      cmake --build /tmp/sdk/bc -j6 --target install; \\
      rm -rf /tmp/sdk; \\
      for tool in spirv-as spirv-val spirv-cross; do \\
        [ -x "/opt/vulkan-sdk/$v/bin/$tool" ] || {{ echo "$tool missing for SDK $v"; exit 1; }}; \\
      done; \\
    done

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Wgpu9479To10598ImageDefault(Image):
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
        return Wgpu9479To10598ImageBase(self.pr, self.config)

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

export CARGO_NET_RETRY=10 CARGO_HTTP_TIMEOUT=120

toolchain="$(sed -n 's/^channel *= *"\\(.*\\)"/\\1/p' rust-toolchain.toml | head -n 1)"
[ -n "$toolchain" ] || {{ echo "prepare.sh: no channel in rust-toolchain.toml"; exit 1; }}
rustup toolchain list | grep -q "^$toolchain" || {{ echo "prepare.sh: toolchain $toolchain is not in the base image"; exit 1; }}
rustup show active-toolchain | grep -q "^$toolchain" || {{ echo "prepare.sh: toolchain $toolchain is not active"; exit 1; }}

sdk_action=.github/actions/install-vulkan-sdk/action.yml
sdk="$(awk '$1 == "full-version:" {{ v = 1 }} v && $1 == "default:" {{ gsub(/["\\047]/, "", $2); print $2; exit }}' "$sdk_action")"
[ -n "$sdk" ] || {{ echo "prepare.sh: no Vulkan SDK version in $sdk_action"; exit 1; }}
[ -d "/opt/vulkan-sdk/$sdk" ] || {{ echo "prepare.sh: Vulkan SDK $sdk is not in the base image"; exit 1; }}
ln -sfn "/opt/vulkan-sdk/$sdk" /opt/vulkan-sdk/current
for tool in spirv-as spirv-val spirv-cross; do
  [ "$(readlink -f "$(command -v "$tool")")" = "/opt/vulkan-sdk/$sdk/bin/$tool" ] || {{ echo "prepare.sh: $tool is not from SDK $sdk"; exit 1; }}
done

vulkaninfo_icd="$(ls /usr/share/vulkan/icd.d/lvp_icd*.json 2>/dev/null | head -n 1)"
[ -n "$vulkaninfo_icd" ] || {{ echo "prepare.sh: lavapipe ICD missing"; exit 1; }}

cargo fetch --locked
{_plan_cmds(self.pr.repo)}
[ -s /tmp/msb-plan.txt ] || {{ echo "prepare.sh: no test suites found in test.patch"; exit 1; }}
while read -r kind; do
  if [ "$kind" = cts ]; then
    cts_rev="$(tr -d '[:space:]' < cts_runner/revision.txt)"
    [ -n "$cts_rev" ] || {{ echo "prepare.sh: empty cts_runner/revision.txt"; exit 1; }}
    rm -rf cts
    mkdir -p cts
    curl --retry 5 -fsSL "https://github.com/gpuweb/cts/archive/$cts_rev.tar.gz" | tar -xz -C cts --strip-components 1
    [ -f cts/tools/run_deno ] || {{ echo "prepare.sh: CTS $cts_rev has no tools/run_deno"; exit 1; }}
    cargo build --offline --locked -p cts_runner --bin cts_runner < /dev/null || {{ echo "prepare.sh: cargo build -p cts_runner failed"; exit 1; }}
    continue
  fi
  {_suite_args("kind")}
  cargo test --offline --locked --no-run --all-features $args < /dev/null || {{ echo "prepare.sh: cargo test --no-run $args failed"; exit 1; }}
  if [ "$kind" = wgpu-gpu ]; then
    cargo test --offline --locked --no-run --all-features -p wgpu-info < /dev/null || {{ echo "prepare.sh: cargo test --no-run -p wgpu-info failed"; exit 1; }}
  fi
done < /tmp/msb-plan.txt
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
            raise ValueError("Wgpu9479To10598ImageDefault dependency must be an Image")
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
    CARGO_BUILD_JOBS=6 \\
    RUST_BACKTRACE=1 \\
    WGPU_CI=true \\
    LVP_POISON_MEMORY=true

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("gfx-rs", "wgpu_9479_to_10598")
class WGPU_9479_TO_10598(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Wgpu9479To10598ImageDefault(self.pr, self._config)

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

        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()

            snapshot = _SNAPSHOT_RE.match(line)
            if snapshot:
                name = f"snapshot::{snapshot.group(2)}"
                (passed if snapshot.group(1) == "PASS" else failed).add(name)
                continue

            cts = _CTS_RE.match(line)
            if cts:
                status, name = cts.group(1), f"cts::{cts.group(2)}"
                if status in ("pass", "warn"):
                    passed.add(name)
                elif status == "skip":
                    skipped.add(name)
                else:
                    failed.add(name)
                continue

            result = _NEXTEST_RE.match(line)
            if not result:
                continue

            status, binary_id, name = result.group(1), result.group(2), result.group(3)
            if binary_id.startswith("wgpu-info"):
                continue

            gpu = _GPU_TAG_RE.match(name)
            if gpu:
                tag, name = gpu.group(1), gpu.group(2)
                if tag.startswith(("Skipped", "Unsupported")):
                    skipped.add(f"{binary_id}::{name}")
                    continue

            full = f"{binary_id}::{name}"
            if status in _NEXTEST_PASSED:
                passed.add(full)
            elif status == "SKIP":
                skipped.add(full)
            else:
                failed.add(full)

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
