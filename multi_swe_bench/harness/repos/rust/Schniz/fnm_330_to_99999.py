import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FnmModernRustImageBase(Image):
    """
    Base Docker image for modern Rust era of fnm (PRs 330+).

    Shared layer: rust:1.81 + apt packages (git) + repo clone.
    Per-PR images layer on top with commit checkout, cargo build/test, and patches.
    """

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
        return "rust:1.81"

    def image_tag(self) -> str:
        return "base-modern-rust"

    def workdir(self) -> str:
        return "base-modern-rust"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV RUST_BACKTRACE=1

WORKDIR /home/

RUN apt-get update && apt-get install -y git

{code}

{self.clear_env}

"""


class FnmModernRustImageDefault(Image):
    """
    Per-PR Docker image for modern Rust era of fnm (PRs 330+, v1.25 to v1.39+).

    Build system: cargo (Rust edition 2018 for PRs 450-526, edition 2021 for PRs 614+)
    Test command: cargo test --verbose
    Base image: FnmModernRustImageBase (rust:1.81 + apt packages + repo clone)

    Docker evidence:
      - PR#450 (sha=a810248e48f2, v1.25): edition 2018, no rust-toolchain.toml,
        22 tests pass on rust:1.81, 2 fail (ARM64 benign)
      - PR#614 (sha=d740ae14c81d, v1.31): edition 2021, 22-24 tests pass
      - PR#839 (sha=8c76ca64a268, v1.34): edition 2021, 24 tests pass
      - PR#1137 (sha=8ce01e302b4a, v1.37): edition 2021, rust-toolchain.toml
        channel=1.78, 25 tests pass
      - PR#1326 (sha=144b3e910fe9, v1.38): edition 2021, rust-toolchain.toml
        channel=1.81, 26 tests pass, 2 fail (ARM64 benign)
      - All PRs in this range compile and test successfully on rust:1.81
      - cargo test output: "test <name> ... ok", "test <name> ... FAILED",
        "test <name> ... ignored"
    """

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
        return FnmModernRustImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

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

cargo test --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export RUST_BACKTRACE=1
cargo test --verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export RUST_BACKTRACE=1
git apply --whitespace=nowarn /home/test.patch
cargo test --verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export RUST_BACKTRACE=1
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cargo test --verbose

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


@Instance.register("Schniz", "fnm_330_to_99999")
class FNM_330_TO_99999(Instance):
    """
    Instance for modern Rust era of fnm (PRs 330+, rust:1.81).

    Covers:
      - PRs 450-526: edition 2018, no rust-toolchain.toml
      - PRs 614-896: edition 2021, no rust-toolchain.toml
      - PRs 1137-1265: edition 2021, rust-toolchain channel=1.78
      - PRs 1268-1326: edition 2021, rust-toolchain channel=1.81

    All compile and test successfully on rust:1.81.

    Test output format (cargo test --verbose):
      test log_level::tests::test_is_writable ... ok
      test downloader::tests::test_installing_node_12::f ... FAILED
      test archive::zip::tests::test_zip_extraction::f ... ok
      test some_module::tests::ignored_test ... ignored
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FnmModernRustImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape sequences for reliable parsing
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", log)

        for line in clean_log.splitlines():
            line = line.strip()
            if line.startswith("test "):
                match = re.search(r"test (.*) \.\.\. ok", line)
                if match:
                    passed_tests.add(match.group(1).strip())
                match = re.search(r"test (.*) \.\.\. ignored", line)
                if match:
                    skipped_tests.add(match.group(1).strip())
                match = re.search(r"test (.*) \.\.\. FAILED", line)
                if match:
                    failed_tests.add(match.group(1).strip())

        if "failures:" in clean_log:
            # Use the last 'failures:' block (clean test name list, not stdout details)
            all_blocks = list(
                re.finditer(r"failures:\n([\s\S]*?)(?:\n\n|\Z)", clean_log)
            )
            if all_blocks:
                for test in all_blocks[-1].group(1).splitlines():
                    if test.strip() and not test.strip().startswith("----"):
                        failed_tests.add(test.strip())

        # Dedup: a test that appears in both passed and failed should be failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
