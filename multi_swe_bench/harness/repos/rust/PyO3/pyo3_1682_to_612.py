import re
from typing import Optional

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
        return "rust:1.63"

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

# Install Python dev headers and create python symlink (v0.8-v0.9 need 'python' not 'python3')
apt-get update && apt-get install -y python3-dev
ln -sf /usr/bin/python3 /usr/bin/python

# Install nightly toolchain required for older PyO3 (concat_idents, specialization)
rustup install nightly-2025-02-01
rustup default nightly-2025-02-01

# Fix parking_lot dependency for v0.9 and v0.10 entries
sed -i 's/parking_lot = {{ version = "0.10", features = \\["nightly"\\] }}/parking_lot = "0.12"/' Cargo.toml
sed -i 's/parking_lot = "0.10.2"/parking_lot = "0.12"/' Cargo.toml

# Build with cap-lints warn to avoid deny(warnings) breakage on modern nightly
RUSTFLAGS='--cap-lints warn' cargo test || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
RUSTFLAGS='--cap-lints warn' cargo test
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Fix parking_lot dependency that patches may (re-)introduce
sed -i 's/parking_lot = {{ version = "0.10", features = \\["nightly"\\] }}/parking_lot = "0.12"/' Cargo.toml
sed -i 's/parking_lot = "0.10.2"/parking_lot = "0.12"/' Cargo.toml
RUSTFLAGS='--cap-lints warn' cargo test
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Fix parking_lot dependency that patches may (re-)introduce
sed -i 's/parking_lot = {{ version = "0.10", features = \\["nightly"\\] }}/parking_lot = "0.12"/' Cargo.toml
sed -i 's/parking_lot = "0.10.2"/parking_lot = "0.12"/' Cargo.toml
RUSTFLAGS='--cap-lints warn' cargo test
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """FROM rust:1.63

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git python3-dev
RUN ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/PyO3/pyo3.git /home/pyo3

WORKDIR /home/pyo3
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN rustup install nightly-2025-02-01 && rustup default nightly-2025-02-01
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("PyO3", "pyo3_1682_to_612")
class PYO3_1682_TO_612(Instance):
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

        passed_pattern = re.compile(r"test (\S+) ... ok")
        failed_pattern = re.compile(r"test (\S+) ... FAILED")
        ignored_pattern = re.compile(r"test (\S+) ... ignored")

        for line in test_log.splitlines():
            line = line.strip()
            if passed_match := passed_pattern.match(line):
                passed_tests.add(passed_match.group(1))
            elif failed_match := failed_pattern.match(line):
                failed_tests.add(failed_match.group(1))
            elif ignored_match := ignored_pattern.match(line):
                skipped_tests.add(ignored_match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
