import re
import json
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
        return "rust:1.85"

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

apt-get update && apt-get install -y protobuf-compiler

cd /home/anki
git reset --hard
git checkout {pr.base.sha}
git submodule update --init --recursive

export PROTOC=/usr/bin/protoc
cargo test --workspace || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/anki
export PROTOC=/usr/bin/protoc
cargo test --workspace

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/anki
if ! git -C /home/anki apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PROTOC=/usr/bin/protoc
cargo test --workspace

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/anki
if ! git -C /home/anki apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PROTOC=/usr/bin/protoc
cargo test --workspace

""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM rust:1.85

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git protobuf-compiler

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/ankitects/anki.git /home/anki

WORKDIR /home/anki
RUN git reset --hard
RUN git checkout {pr.base.sha}
RUN git submodule update --init --recursive
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("ankitects", "anki_4255_to_2579")
class ANKI_4255_TO_2579(Instance):
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
        # Parse cargo test output.
        # Example output lines:
        #   test cloze::test::cloze_only ... ok
        #   test some::module::test_name ... FAILED
        #   test some::module::test_name ... ignored
        #   test result: ok. 306 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in test_log.splitlines():
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
        if "failures:" in test_log:
            match = re.search(r"failures:\n([\s\S]*?)\n\n", test_log)
            if match:
                for test in match.group(1).splitlines():
                    if test.strip() and not test.strip().startswith("----"):
                        failed_tests.add(test.strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
