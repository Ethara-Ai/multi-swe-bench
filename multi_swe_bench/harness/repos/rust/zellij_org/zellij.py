import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ZellijImageBase(Image):
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

        return f"""FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class ZellijImageDefault(Image):
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
        return ZellijImageBase(self.pr, self.config)

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

# Strip wasm targets BEFORE cd to prevent rustup auto-sync
for f in /home/{pr.repo}/rust-toolchain.toml /home/{pr.repo}/rust-toolchain; do
    if [ -f "$f" ]; then
        sed -i '/targets/d' "$f"
        sed -i '/wasm32/d' "$f"
    fi
done

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Re-strip after checkout (checkout restores original files)
for f in rust-toolchain.toml rust-toolchain; do
    if [ -f "$f" ]; then
        sed -i '/targets/d' "$f"
        sed -i '/wasm32/d' "$f"
    fi
done

rustup target remove wasm32-wasi 2>/dev/null || true
rustup target remove wasm32-wasip1 2>/dev/null || true
rustup show active-toolchain || true

# Create dummy .wasm plugin stubs so include_bytes! in zellij-utils/src/consts.rs succeeds.
# The real plugins would need cross-compilation to wasm, but tests don't need them.
if [ -f zellij-utils/src/consts.rs ]; then
    PLUGIN_NAMES=$(grep -oP 'add_plugin!\\(\\s*\\w+,\\s*"\\K[^"]+' zellij-utils/src/consts.rs || true)
    for target_dir in target/wasm32-wasi/debug target/wasm32-wasip1/debug; do
        mkdir -p "$target_dir"
        for plugin in $PLUGIN_NAMES; do
            touch "$target_dir/$plugin"
        done
    done
fi

# Also handle early-era code that may use different plugin embedding patterns
# Create stubs for common plugin names in both target dirs
for target_dir in target/wasm32-wasi/debug target/wasm32-wasip1/debug; do
    mkdir -p "$target_dir"
done

# Also create stubs in assets/plugins/ for early-era PRs that use include_bytes!
# (e.g., src/install.rs uses include_bytes!("assets/plugins/compact-bar.wasm"))
mkdir -p assets/plugins
for name in compact-bar.wasm status-bar.wasm tab-bar.wasm strider.wasm; do
    touch "assets/plugins/$name"
done

# Pre-build dependencies to speed up test runs
cargo test --workspace --no-run 2>&1 || true

# Reset any changes from sed so git is clean for patch application
# BUT re-strip wasm targets from rust-toolchain.toml after reset, so run scripts
# don't trigger rustup auto-sync of deprecated wasm32-wasi target
git checkout -- . 2>/dev/null || true
for f in rust-toolchain.toml rust-toolchain; do
    if [ -f "$f" ]; then
        sed -i '/targets/d' "$f"
        sed -i '/wasm32/d' "$f"
    fi
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

# Strip wasm targets BEFORE cd to prevent rustup auto-sync of deprecated wasm32-wasi
for f in /home/{pr.repo}/rust-toolchain.toml /home/{pr.repo}/rust-toolchain; do
    if [ -f "$f" ]; then
        sed -i '/targets/d' "$f"
        sed -i '/wasm32/d' "$f"
    fi
done

cd /home/{pr.repo}

if [ -f zellij-utils/src/consts.rs ]; then
    PLUGIN_NAMES=$(grep -oP 'add_plugin!\\(\\s*\\w+,\\s*"\\K[^"]+' zellij-utils/src/consts.rs || true)
    for target_dir in target/wasm32-wasi/debug target/wasm32-wasip1/debug; do
        mkdir -p "$target_dir"
        for plugin in $PLUGIN_NAMES; do
            touch "$target_dir/$plugin"
        done
    done
fi
mkdir -p assets/plugins
for name in compact-bar.wasm status-bar.wasm tab-bar.wasm strider.wasm; do
    touch "assets/plugins/$name"
done

cargo test --workspace 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

if [ -f /home/{pr.repo}/rust-toolchain.toml ]; then
    sed -i '/targets/d' /home/{pr.repo}/rust-toolchain.toml
    sed -i '/wasm32/d' /home/{pr.repo}/rust-toolchain.toml
fi
if [ -f /home/{pr.repo}/rust-toolchain ]; then
    sed -i '/targets/d' /home/{pr.repo}/rust-toolchain
    sed -i '/wasm32/d' /home/{pr.repo}/rust-toolchain
fi

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.wasm' --exclude='*.gif' --exclude='*.png' /home/test.patch || git apply --whitespace=nowarn --reject --exclude='*.wasm' --exclude='*.gif' --exclude='*.png' /home/test.patch 2>&1 || true

if [ -f zellij-utils/src/consts.rs ]; then
    PLUGIN_NAMES=$(grep -oP 'add_plugin!\\(\\s*\\w+,\\s*"\\K[^"]+' zellij-utils/src/consts.rs || true)
    for target_dir in target/wasm32-wasi/debug target/wasm32-wasip1/debug; do
        mkdir -p "$target_dir"
        for plugin in $PLUGIN_NAMES; do
            touch "$target_dir/$plugin"
        done
    done
fi
mkdir -p assets/plugins
for name in compact-bar.wasm status-bar.wasm tab-bar.wasm strider.wasm; do
    touch "assets/plugins/$name"
done

cargo test --workspace 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

if [ -f /home/{pr.repo}/rust-toolchain.toml ]; then
    sed -i '/targets/d' /home/{pr.repo}/rust-toolchain.toml
    sed -i '/wasm32/d' /home/{pr.repo}/rust-toolchain.toml
fi
if [ -f /home/{pr.repo}/rust-toolchain ]; then
    sed -i '/targets/d' /home/{pr.repo}/rust-toolchain
    sed -i '/wasm32/d' /home/{pr.repo}/rust-toolchain
fi

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.wasm' --exclude='*.gif' --exclude='*.png' /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --reject --exclude='*.wasm' --exclude='*.gif' --exclude='*.png' /home/test.patch /home/fix.patch 2>&1 || true

if [ -f zellij-utils/src/consts.rs ]; then
    PLUGIN_NAMES=$(grep -oP 'add_plugin!\\(\\s*\\w+,\\s*"\\K[^"]+' zellij-utils/src/consts.rs || true)
    for target_dir in target/wasm32-wasi/debug target/wasm32-wasip1/debug; do
        mkdir -p "$target_dir"
        for plugin in $PLUGIN_NAMES; do
            touch "$target_dir/$plugin"
        done
    done
fi
mkdir -p assets/plugins
for name in compact-bar.wasm status-bar.wasm tab-bar.wasm strider.wasm; do
    touch "assets/plugins/$name"
done

cargo test --workspace 2>&1

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


@Instance.register("zellij-org", "zellij")
class Zellij(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ZellijImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

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

        # Deduplicate: ensure no test appears in multiple categories
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
