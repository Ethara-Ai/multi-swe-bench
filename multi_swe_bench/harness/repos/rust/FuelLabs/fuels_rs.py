"""FuelLabs/fuels-rs harness — modern era (PRs #1042–#1644).

Covers PRs where .github/workflows/ci.yml has all three env vars:
RUST_VERSION, FORC_VERSION, FUEL_CORE_VERSION.

This is the default registration (number_interval == "").
"""

from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FuelsRsImageBase(Image):
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
        return "rust:1.93.0"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

RUN rustup target add wasm32-unknown-unknown

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class FuelsRsImageDefault(Image):
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
        return FuelsRsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Extract forc and fuel-core versions from CI config at this commit.
FORC_VERSION=$(grep 'FORC_VERSION:' .github/workflows/ci.yml | head -1 | sed 's/.*: *//' | tr -d ' "'"'"'')
FUEL_CORE_VERSION=$(grep 'FUEL_CORE_VERSION:' .github/workflows/ci.yml | head -1 | sed 's/.*: *//' | tr -d ' "'"'"'')

echo "Installing forc ${{FORC_VERSION}} and fuel-core ${{FUEL_CORE_VERSION}}"

# Detect architecture for binary downloads.
ARCH="$(dpkg --print-architecture)"
case "${{ARCH}}" in
    amd64) FUEL_ARCH="x86_64-unknown-linux-gnu" ;;
    arm64) FUEL_ARCH="aarch64-unknown-linux-gnu" ;;
    *) echo "Unsupported arch: ${{ARCH}}" && exit 1 ;;
esac

# Install forc binary (uses dpkg arch: amd64/arm64).
curl -sSfL "https://github.com/FuelLabs/sway/releases/download/v${{FORC_VERSION}}/forc-binaries-linux_${{ARCH}}.tar.gz" \
    -o /tmp/forc.tar.gz && \
    tar xzf /tmp/forc.tar.gz --strip-components=2 -C /usr/local/bin && \
    rm /tmp/forc.tar.gz || true

# Install fuel-core binary (uses Rust triple: x86_64-.../aarch64-...).
curl -sSfL "https://github.com/FuelLabs/fuel-core/releases/download/v${{FUEL_CORE_VERSION}}/fuel-core-${{FUEL_CORE_VERSION}}-${{FUEL_ARCH}}.tar.gz" \
    -o /tmp/fc.tar.gz && \
    tar xzf /tmp/fc.tar.gz --strip-components=1 -C /usr/local/bin && \
    rm /tmp/fc.tar.gz || true

# Build Sway test projects so compiled artifacts are available for tests.
if command -v forc >/dev/null 2>&1; then
    if [ -d "e2e" ]; then
        forc build --release --terse --path e2e || true
    fi
    if [ -f "scripts/build-test-projects/Cargo.toml" ]; then
        cargo run -p build-test-projects || true
    elif [ -f "packages/fuels/Forc.toml" ]; then
        forc build --path packages/fuels || true
    else
        find . -path '*/tests/*' -name 'Forc.toml' -print0 2>/dev/null | while IFS= read -r -d '' forc_toml; do
            forc build --path "$(dirname "$forc_toml")" || true
        done
    fi
fi

# Generate lockfile. fuels-rs is a library workspace that does not commit
# Cargo.lock, so fresh resolution may pull incompatible newer crate versions.
cargo generate-lockfile

# Pin transitive deps only when the standalone fuel-core-client crate exists
# in this PR's dep tree (modern PRs ~#1337+). Older PRs use fuel-core as a
# single crate and do not need these pins.
if cargo metadata --format-version=1 2>/dev/null | grep -q '"name":"fuel-core-client"'; then
    cargo update -p cynic --precise 3.10.0 || true
    cargo update -p cynic-proc-macros --precise 3.10.0 || true
    cargo update -p fuel-core-client --precise "${{FUEL_CORE_VERSION}}" || true
    cargo update -p async-graphql --precise 7.0.15 || true
    cargo update -p async-graphql-derive --precise 7.0.15 || true
    cargo update -p async-graphql-parser --precise 7.0.15 || true
    cargo update -p async-graphql-value --precise 7.0.15 || true
fi

cargo build --tests || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Rebuild Sway artifacts in case source changed.
if command -v forc >/dev/null 2>&1; then
    if [ -d "e2e" ]; then
        forc build --release --terse --path e2e || true
    fi
    if [ -f "scripts/build-test-projects/Cargo.toml" ]; then
        cargo run -p build-test-projects || true
    elif [ -f "packages/fuels/Forc.toml" ]; then
        forc build --path packages/fuels || true
    else
        find . -path '*/tests/*' -name 'Forc.toml' -print0 2>/dev/null | while IFS= read -r -d '' forc_toml; do
            forc build --path "$(dirname "$forc_toml")" || true
        done
    fi
fi

cargo test --workspace

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

# Rebuild Sway artifacts after applying test patch.
if command -v forc >/dev/null 2>&1; then
    if [ -d "e2e" ]; then
        forc build --release --terse --path e2e || true
    fi
    if [ -f "scripts/build-test-projects/Cargo.toml" ]; then
        cargo run -p build-test-projects || true
    elif [ -f "packages/fuels/Forc.toml" ]; then
        forc build --path packages/fuels || true
    else
        find . -path '*/tests/*' -name 'Forc.toml' -print0 2>/dev/null | while IFS= read -r -d '' forc_toml; do
            forc build --path "$(dirname "$forc_toml")" || true
        done
    fi
fi

cargo test --workspace

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Rebuild Sway artifacts after applying patches.
if command -v forc >/dev/null 2>&1; then
    if [ -d "e2e" ]; then
        forc build --release --terse --path e2e || true
    fi
    if [ -f "scripts/build-test-projects/Cargo.toml" ]; then
        cargo run -p build-test-projects || true
    elif [ -f "packages/fuels/Forc.toml" ]; then
        forc build --path packages/fuels || true
    else
        find . -path '*/tests/*' -name 'Forc.toml' -print0 2>/dev/null | while IFS= read -r -d '' forc_toml; do
            forc build --path "$(dirname "$forc_toml")" || true
        done
    fi
fi

cargo test --workspace

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


def _parse_cargo_test_log(test_log: str) -> TestResult:
    """Parse standard ``cargo test`` output.

    Handles ANSI escape codes and deduplicates with worst-wins policy.
    """
    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass_tests = [re.compile(r"test (\S+) \.\.\. ok")]
    re_fail_tests = [re.compile(r"test (\S+) \.\.\. FAILED")]
    re_skip_tests = [re.compile(r"test (\S+) \.\.\. ignored")]

    for line in test_log.splitlines():
        line = line.strip()

        for re_pass in re_pass_tests:
            match = re_pass.match(line)
            if match:
                passed_tests.add(match.group(1))

        for re_fail in re_fail_tests:
            match = re_fail.match(line)
            if match:
                failed_tests.add(match.group(1))

        for re_skip in re_skip_tests:
            match = re_skip.match(line)
            if match:
                skipped_tests.add(match.group(1))

    # Deduplicate — worst result wins.
    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("FuelLabs", "fuels-rs")
class FuelsRs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return FuelsRsImageDefault(self.pr, self._config)

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
        return _parse_cargo_test_log(test_log)
