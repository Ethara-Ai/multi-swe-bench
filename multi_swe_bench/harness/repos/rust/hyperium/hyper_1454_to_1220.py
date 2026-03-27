import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Shared spmc fix commands for v0.11.x.
# spmc 0.2 is completely yanked; 0.3 changes Sender::send() to require &mut self.
# We upgrade the dep and wrap Sender in Mutex where needed.
_SPMC_FIX = r"""
# Fix spmc: v0.2 is yanked, upgrade to 0.3 which requires &mut self for send().
sed -i 's/spmc = "0.2"/spmc = "0.3"/' Cargo.toml

if [ -f tests/server.rs ]; then
    sed -i "s|tx: &'a spmc::Sender<Reply>|tx: \&'a std::sync::Mutex<spmc::Sender<Reply>>|" tests/server.rs
    sed -i 's|self\.tx\.send(|self.tx.lock().unwrap().send(|g' tests/server.rs
    sed -i 's|reply_tx: spmc::Sender<Reply>|reply_tx: std::sync::Mutex<spmc::Sender<Reply>>|' tests/server.rs
    sed -i 's|reply_tx: reply_tx,|reply_tx: std::sync::Mutex::new(reply_tx),|' tests/server.rs
fi
"""


class ImageDefault(Image):
    """v0.11.x era (PRs 1454-1220): hyper 0.11.x on rust:1.75 with spmc fix."""

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
        return "rust:1.75"

    def image_prefix(self) -> str:
        return "envagent"

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

export RUSTFLAGS="--cap-lints warn"
{spmc_fix}
# Skip doc-tests: deprecated '...' range syntax causes compile errors in doctests.
cargo test --lib --tests || true

""".format(pr=self.pr, spmc_fix=_SPMC_FIX),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export RUSTFLAGS="--cap-lints warn"
{spmc_fix}
cargo test --lib --tests

""".format(pr=self.pr, spmc_fix=_SPMC_FIX),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
export RUSTFLAGS="--cap-lints warn"
{spmc_fix}
cargo test --lib --tests

""".format(pr=self.pr, spmc_fix=_SPMC_FIX),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
export RUSTFLAGS="--cap-lints warn"
{spmc_fix}
cargo test --lib --tests

""".format(pr=self.pr, spmc_fix=_SPMC_FIX),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """FROM rust:1.75

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr, copy_commands=copy_commands)


@Instance.register("hyperium", "hyper_1454_to_1220")
class HYPER_1454_TO_1220(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_pattern = re.compile(r"test (.*) \.\.\. ok")
        failed_pattern = re.compile(r"test (.*) \.\.\. FAILED")
        ignored_pattern = re.compile(r"test (.*) \.\.\. ignored")

        for line in log.splitlines():
            if passed_match := passed_pattern.search(line):
                passed_tests.add(passed_match.group(1).strip())
            elif failed_match := failed_pattern.search(line):
                failed_tests.add(failed_match.group(1).strip())
            elif ignored_match := ignored_pattern.search(line):
                skipped_tests.add(ignored_match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
