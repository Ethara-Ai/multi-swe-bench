import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Dep pinning commands for v0.14.x.
# Without Cargo.lock, latest transitive deps are resolved, many requiring rustc 1.82+.
# These pin deps to versions compatible with rustc 1.75.
# Remove tower_client example that references hyper::client::conn::http1 (v1.x API)
# which doesn't exist in v0.14.x. Both the file and the Cargo.toml entry must go.
_REMOVE_TOWER_CLIENT = """
rm -f examples/tower_client.rs
sed -i '/^\\[\\[example\\]\\]/{N;N;N;/tower_client/d}' Cargo.toml
"""

_DEP_PIN = """
# Pin transitive deps that require newer rustc versions.
cargo generate-lockfile
cargo update -p url --precise 2.4.1 || true
cargo update -p indexmap --precise 2.6.0 || true
cargo update -p tokio-util@0.7.18 --precise 0.7.12 || cargo update -p tokio-util --precise 0.7.12 || true
cargo update -p tokio --precise 1.38.0 || true
cargo update -p serde_json --precise 1.0.128 || true
cargo update -p serde --precise 1.0.210 || true
cargo update -p syn@2.0.117 --precise 2.0.79 || cargo update -p syn@2.0.100 --precise 2.0.79 || cargo update -p syn --precise 2.0.79 || true
cargo update -p proc-macro2 --precise 1.0.86 || true
cargo update -p log --precise 0.4.22 || true
"""


class ImageDefault(Image):
    """v0.14.x era (PRs 3796-2216): hyper 0.14.x on rust:1.75 with dep pinning."""

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
{remove_tower_client}
export RUSTFLAGS="--cap-lints warn"
{dep_pin}
cargo test --features full || true

""".format(pr=self.pr, dep_pin=_DEP_PIN, remove_tower_client=_REMOVE_TOWER_CLIENT),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{remove_tower_client}
export RUSTFLAGS="--cap-lints warn"
{dep_pin}
cargo test --features full

""".format(pr=self.pr, dep_pin=_DEP_PIN, remove_tower_client=_REMOVE_TOWER_CLIENT),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{remove_tower_client}
git apply /home/test.patch
export RUSTFLAGS="--cap-lints warn"
{dep_pin}
cargo test --features full

""".format(pr=self.pr, dep_pin=_DEP_PIN, remove_tower_client=_REMOVE_TOWER_CLIENT),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{remove_tower_client}
git apply /home/test.patch /home/fix.patch
export RUSTFLAGS="--cap-lints warn"
{dep_pin}
cargo test --features full

""".format(pr=self.pr, dep_pin=_DEP_PIN, remove_tower_client=_REMOVE_TOWER_CLIENT),
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


@Instance.register("hyperium", "hyper_3796_to_2216")
class HYPER_3796_TO_2216(Instance):
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
