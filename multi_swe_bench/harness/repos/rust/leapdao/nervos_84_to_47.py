import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.75.0-bullseye"
_INTERVAL_NAME = "nervos_84_to_47"
_BASE_TAG = "base-84_to_47"
_PKG_DIR = "packages/parent-bridge"
_CONTRACT_TOOLCHAIN = "nightly-2020-06-01"
_TEST_TOOLCHAIN = "1.63.0"
_RISCV_TARGET = "riscv64imac-unknown-none-elf"
_PATCHER_REPO = "https://github.com/xxuejie/ckb-binary-patcher.git"
_PATCHER_REV = "b9489de4b3b9d59bc29bce945279bc6f28413113"

_INFRA_BLOCK = """ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt
"""

_LANG_ENV = """ENV CI=true \\
    CARGO_TERM_COLOR=never \\
    CARGO_NET_RETRY=10 \\
    RUST_BACKTRACE=1
"""

_TOOL_CHECK = """RUN git --version && rustc --version && cargo --version
"""

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

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "+${{BASE_COMMIT}}:refs/heads/msb-base-commit"
git checkout --detach "${{BASE_COMMIT}}"
bash /home/check_git_changes.sh

export CARGO_BUILD_JOBS=4
apt-get update
apt-get install -y --no-install-recommends gcc-riscv64-unknown-elf
rm -rf /var/lib/apt/lists/*
for attempt in 1 2 3 4 5; do
  if rustup toolchain install {contract_toolchain} --profile minimal --target {riscv_target} --no-self-update && rustup toolchain install {test_toolchain} --profile minimal --no-self-update; then break; fi
  sleep 20
done
rustc +{contract_toolchain} --version
rustc +{test_toolchain} --version
if [ ! -x /usr/local/bin/ckb-binary-patcher ]; then
  rm -rf /tmp/ckb-binary-patcher
  git clone {patcher_repo} /tmp/ckb-binary-patcher
  git -C /tmp/ckb-binary-patcher checkout {patcher_rev}
  cargo install --path /tmp/ckb-binary-patcher --root /usr/local
  rm -rf /tmp/ckb-binary-patcher
fi
riscv64-unknown-elf-gcc --version
cd /home/{repo}/{pkg_dir}
bash /home/mswb_contracts.sh vendor
for attempt in 1 2 3 4 5; do
  if cargo +{test_toolchain} fetch; then break; fi
  sleep 15
done
bash /home/mswb_registry_fix.sh
bash /home/mswb_contracts.sh build
cargo +{test_toolchain} test --offline --no-run
cd /home/{repo}
git checkout -- .

test -f /opt/nvc/config && test -d /opt/nvc/vendor && test -x /usr/local/bin/ckb-binary-patcher && test -n "$(find {pkg_dir}/target/debug/deps -maxdepth 1 -type f -perm -u+x -name 'tests-*')" && echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
set -o pipefail
export CI=true
export CARGO_TERM_COLOR=never
export CARGO_BUILD_JOBS=4
cd /home/{repo}/{pkg_dir}
if ! bash /home/mswb_contracts.sh vendor > /tmp/mswb_vendor.log 2>&1; then
  tail -n 20 /tmp/mswb_vendor.log
fi
bash /home/mswb_contracts.sh build
cargo +{test_toolchain} test --offline -- --test-threads=1 > /tmp/mswb_test.log 2>&1
tail -n 60 /tmp/mswb_test.log
python3 /home/mswb_digest.py /tmp/mswb_test.log tests/src
exit 0
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_CONTRACTS_SH = r"""#!/bin/bash
set -o pipefail
mode="$1"
contracts=$(sed -n '/^\[\[contracts\]\]/,/^$/p' capsule.toml | grep -oE '^name = "[^"]+"' | cut -d'"' -f2)
present=()
for c in $contracts; do
  if [ -f "contracts/$c/Cargo.toml" ]; then
    present+=("$c")
  fi
done
if [ "${#present[@]}" -eq 0 ]; then
  echo "MSWB_CONTRACT NONE"
  exit 1
fi

if [ "$mode" = "vendor" ]; then
  args=()
  for c in "${present[@]:1}"; do
    args+=(--sync "contracts/$c/Cargo.toml")
  done
  mkdir -p /opt/nvc
  for attempt in 1 2 3 4 5; do
    rm -rf /opt/nvc/vendor.new
    if cargo +1.75.0 vendor --versioned-dirs --manifest-path "contracts/${present[0]}/Cargo.toml" "${args[@]}" /opt/nvc/vendor.new > /opt/nvc/config.new; then
      rm -rf /opt/nvc/vendor
      mv /opt/nvc/vendor.new /opt/nvc/vendor
      sed 's|/opt/nvc/vendor.new|/opt/nvc/vendor|' /opt/nvc/config.new > /opt/nvc/config
      rm -f /opt/nvc/config.new
      exit 0
    fi
    sleep 10
  done
  exit 1
fi

rm -rf build/debug build/release
mkdir -p build/debug build/release
for c in "${present[@]}"; do
  rm -rf "contracts/$c/target"
done
for pass in 1 2; do
  for c in "${present[@]}"; do
    if [ -f "build/debug/$c" ]; then
      continue
    fi
    if (cd "contracts/$c" && CARGO_HOME=/opt/nvc RUSTFLAGS="-C link-arg=-s -C link-arg=--no-rosegment" cargo +nightly-2020-06-01 build --offline --target riscv64imac-unknown-none-elf) >> /tmp/mswb_contracts.log 2>&1; then
      ckb-binary-patcher -i "contracts/$c/target/riscv64imac-unknown-none-elf/debug/$c" -o "build/debug/$c" && cp "build/debug/$c" "build/release/$c"
    fi
  done
done
for c in "${present[@]}"; do
  if [ -f "build/debug/$c" ]; then
    echo "MSWB_CONTRACT OK $c"
  else
    echo "MSWB_CONTRACT MISSING $c"
  fi
done
exit 0
"""

_REGISTRY_FIX_SH = r"""#!/bin/bash
set -e
for f in /usr/local/cargo/registry/src/*/faster-hex-0.3.*/src/lib.rs; do
  [ -f "$f" ] || continue
  sed -i 's/hex_check_fallback, hex_check_sse,/hex_check_fallback,/' "$f"
done
for f in $(grep -rl --include=build.rs 'let can_enable_asm' /usr/local/cargo/registry/src /usr/local/cargo/git/checkouts 2>/dev/null); do
  [ -f "$f" ] || continue
  if grep -q 'CARGO_CFG_TARGET_ARCH' "$f"; then
    continue
  fi
  sed -i \
    -e 's/let can_enable_asm = (target_pointer_width == "64")/let can_enable_asm = env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_default() == "x86_64" \&\& (target_pointer_width == "64")/' \
    -e 's/if cfg!(feature = "asm") \&\& (!can_enable_asm)/if false \&\& cfg!(feature = "asm") \&\& (!can_enable_asm)/' \
    "$f"
done
echo "MSWB_REGISTRY_FIX_OK"
"""

_DIGEST_PY = r"""import os
import re
import sys

log_path, src_dir = sys.argv[1], sys.argv[2]
prefix = "cargo::tests::lib::"
line_re = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)")
test_re = re.compile(r"#\[test\]\s*(?:#\[[^\]]*\]\s*)*fn\s+([A-Za-z0-9_]+)")

results = {}
try:
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", fh.read())
except OSError:
    text = ""
for line in text.splitlines():
    m = line_re.match(line.strip())
    if not m:
        continue
    status = {"ok": "PASSED", "FAILED": "FAILED", "ignored": "SKIPPED"}[m.group(2)]
    name = m.group(1)
    if results.get(name) != "FAILED":
        results[name] = status

for root, _, files in os.walk(src_dir):
    for fname in sorted(files):
        if not fname.endswith(".rs"):
            continue
        rel = os.path.relpath(os.path.join(root, fname), src_dir)[:-3]
        parts = [p for p in rel.split(os.sep) if p not in ("lib", "main", "mod")]
        mod = "::".join(parts)
        with open(os.path.join(root, fname), encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        for fn in test_re.findall(body):
            name = f"{mod}::{fn}" if mod else fn
            if name not in results:
                results[name] = "FAILED"

for name in sorted(results):
    print("MSWB_RESULT {} {}{}".format(results[name], prefix, name))
"""

_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")


def _parse_mswb_log(test_log: str) -> TestResult:
    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    passed_tests: set = set()
    failed_tests: set = set()
    skipped_tests: set = set()
    for line in test_log.splitlines():
        m = _RESULT_RE.match(line.strip())
        if not m:
            continue
        status, name = m.group(1), m.group(2)
        if status == "FAILED":
            failed_tests.add(name)
        elif status == "PASSED":
            passed_tests.add(name)
        else:
            skipped_tests.add(name)
    passed_tests -= failed_tests
    skipped_tests -= failed_tests | passed_tests
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

    def dependency(self) -> str:
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = _INFRA_BLOCK.format(org=self.pr.org, repo=self.pr.repo)
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {_RUST_IMAGE}\n\n"
            f"{infra}\n"
            f"{_LANG_ENV}\n"
            f"{_TOOL_CHECK}\n"
            "WORKDIR /home/\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}\n\n'
            'CMD ["/bin/bash"]\n'
        )


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
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        prepare = _PREPARE_SH.format(
            repo=repo,
            pkg_dir=_PKG_DIR,
            contract_toolchain=_CONTRACT_TOOLCHAIN,
            test_toolchain=_TEST_TOOLCHAIN,
            riscv_target=_RISCV_TARGET,
            patcher_repo=_PATCHER_REPO,
            patcher_rev=_PATCHER_REV,
        )
        run_tests = _RUN_TESTS_SH.format(
            repo=repo, pkg_dir=_PKG_DIR, test_toolchain=_TEST_TOOLCHAIN
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", _RUN_SH.format(repo=repo)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(repo=repo)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(repo=repo)),
            File(".", "run_tests.sh", run_tests),
            File(".", "mswb_contracts.sh", _CONTRACTS_SH),
            File(".", "mswb_registry_fix.sh", _REGISTRY_FIX_SH),
            File(".", "mswb_digest.py", _DIGEST_PY),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            f"{copy_commands}\n"
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\n\n'
            "RUN bash /home/prepare.sh\n\n"
            f"WORKDIR /home/{self.pr.repo}\n\n"
            f"{Image._HARDENING_BLOCK.rstrip(chr(10))}\n\n"
        )


@Instance.register("leapdao", "nervos")
@Instance.register("leapdao", _INTERVAL_NAME)
class NERVOS_84_TO_47(Instance):
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
        return _parse_mswb_log(test_log)
