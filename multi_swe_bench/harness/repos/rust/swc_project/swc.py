from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SwcImageBase(Image):
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
        return "rust:latest"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""FROM {image_name}
{global_block}
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    cmake \\
    pkg-config \\
    libssl-dev \\
    curl \\
    nodejs \\
    npm \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g yarn jest@27 mocha || true

{code}
{clear_block}"""


class SwcImageDefault(Image):
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
        return SwcImageBase(self.pr, self.config)

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

# Install the correct Rust toolchain if rust-toolchain or rust-toolchain.toml exists
if [ -f rust-toolchain.toml ] || [ -f rust-toolchain ]; then
    rustup show
fi

# Initialize submodules (test262-parser-tests, terser, etc.)
git submodule update --init --recursive 2>&1 || true

# Pre-download crates
export CI=1
export CARGO_INCREMENTAL=0
export RUST_LOG=off

if [ -d crates ]; then
    # New era: crates/ workspace layout
    cargo test --workspace --no-run 2>&1 || true
else
    # Old era: ecmascript/ layout
    cargo test --all --all-features --no-run 2>&1 || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash

cd /home/{pr.repo}

export CI=1
export CARGO_INCREMENTAL=0
export RUST_LOG=off

if [ -d crates ]; then
    # New era: extract crate names from test.patch paths like crates/<name>/...
    CRATES=$(grep '^diff --git a/crates/' /home/test.patch 2>/dev/null | sed 's|diff --git a/crates/\([^/]*\)/.*|\1|' | sort -u)
    if [ -n "$CRATES" ]; then
        for crate in $CRATES; do
            echo "=== Testing crate: $crate ==="
            cargo test -p "$crate" 2>&1 || true
        done
    else
        cargo test --workspace 2>&1 || true
    fi
else
    # Old era: ecmascript/ layout — run all tests
    cargo test --all --all-features 2>&1 || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch 2>&1 || true

export CI=1
export CARGO_INCREMENTAL=0
export RUST_LOG=off

if [ -d crates ]; then
    CRATES=$(grep '^diff --git a/crates/' /home/test.patch 2>/dev/null | sed 's|diff --git a/crates/\([^/]*\)/.*|\1|' | sort -u)
    if [ -n "$CRATES" ]; then
        for crate in $CRATES; do
            echo "=== Testing crate: $crate ==="
            cargo test -p "$crate" 2>&1 || true
        done
    else
        cargo test --workspace 2>&1 || true
    fi
else
    cargo test --all --all-features 2>&1 || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch 2>&1 || true
git apply --whitespace=nowarn /home/fix.patch 2>&1 || true

export CI=1
export CARGO_INCREMENTAL=0
export RUST_LOG=off

if [ -d crates ]; then
    CRATES=$(grep '^diff --git a/crates/' /home/test.patch 2>/dev/null | sed 's|diff --git a/crates/\([^/]*\)/.*|\1|' | sort -u)
    if [ -n "$CRATES" ]; then
        for crate in $CRATES; do
            echo "=== Testing crate: $crate ==="
            cargo test -p "$crate" 2>&1 || true
        done
    else
        cargo test --workspace 2>&1 || true
    fi
else
    cargo test --all --all-features 2>&1 || true
fi

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

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""FROM {name}:{tag}
{global_block}
{copy_commands}
{prepare_commands}
{clear_block}"""


@Instance.register("swc-project", "swc")
class SwcProjectSwc(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SwcImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"test (\S+) ... ok")]
        re_fail_tests = [re.compile(r"test (\S+) ... FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) ... ignored")]

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

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
