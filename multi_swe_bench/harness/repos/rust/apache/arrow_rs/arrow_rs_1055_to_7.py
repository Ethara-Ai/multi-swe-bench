import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "rust:latest"

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

cd /home/arrow-rs
git reset --hard
git checkout {pr.base.sha}

# PR 7 has code in rust/ subdir (monorepo layout); later PRs use root Cargo.toml
if [ -f rust/Cargo.toml ] && [ ! -f Cargo.toml ]; then
    cd rust
    # Exclude members that reference missing dirs or have dep conflicts
    sed -i '/"datafusion"/d; /"datafusion-examples"/d; /"benchmarks"/d; /"arrow-flight"/d; /"integration-testing"/d' Cargo.toml
else
    # PRs 464-1055: exclude arrow-flight and integration-testing (proc-macro2 pin conflict)
    sed -i '/"arrow-flight"/d; /"integration-testing"/d' Cargo.toml
    # Fix E0761: file vs directory module conflict if both exist
    if [ -f arrow/src/compute/kernels.rs ] && [ -d arrow/src/compute/kernels ]; then
        rm arrow/src/compute/kernels.rs
    fi
fi

cargo generate-lockfile
cargo test -p arrow --lib || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/arrow-rs
# PR 7 has code in rust/ subdir
if [ -f rust/Cargo.toml ] && [ ! -f Cargo.toml ]; then
    cd rust
fi
cargo test -p arrow --lib

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/arrow-rs
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# PR 7 has code in rust/ subdir
if [ -f rust/Cargo.toml ] && [ ! -f Cargo.toml ]; then
    cd rust
fi
cargo test -p arrow --lib

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/arrow-rs
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# PR 7 has code in rust/ subdir
if [ -f rust/Cargo.toml ] && [ ! -f Cargo.toml ]; then
    cd rust
fi
cargo test -p arrow --lib

""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """FROM rust:latest

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
""".format(pr=self.pr, copy_commands=copy_commands)


@Instance.register("apache", "arrow-rs_1055_to_7")
class ARROW_RS_1055_TO_7(Instance):
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
