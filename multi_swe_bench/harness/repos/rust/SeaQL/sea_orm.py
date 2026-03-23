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
        return "rust:latest"

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
                "prepare.sh",
                """ls -F
###ACTION_DELIMITER###
cargo test --workspace -- --nocapture || true
###ACTION_DELIMITER###
echo 'cargo test --workspace -- --nocapture' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
cargo test --workspace -- --nocapture

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    patch --batch --fuzz=5 -p1 -i /home/test.patch || true
fi
cargo test --workspace -- --nocapture

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    patch --batch --fuzz=5 -p1 -i /home/test.patch || true
    patch --batch --fuzz=5 -p1 -i /home/fix.patch || true
fi
cargo test --workspace -- --nocapture

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM rust:latest

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
RUN apt-get update && apt-get install -y git pkg-config libssl-dev

# Ensure bash is available
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
RUN git clone https://github.com/SeaQL/sea-orm.git /home/sea-orm

WORKDIR /home/sea-orm
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("SeaQL", "sea-orm")
class SeaORM(Instance):
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

        for line in log.splitlines():
            line = line.strip()
            if line.startswith("test "):
                # Passed: test foo::bar ... ok
                match = re.search(r"test (.*) \.\.\. ok", line)
                if match:
                    passed_tests.add(match.group(1).strip())
                    continue
                # Failed: test foo::bar ... FAILED
                match = re.search(r"test (.*) \.\.\. FAILED", line)
                if match:
                    failed_tests.add(match.group(1).strip())
                    continue
                # Ignored/skipped: test foo::bar ... ignored
                match = re.search(r"test (.*) \.\.\. ignored", line)
                if match:
                    skipped_tests.add(match.group(1).strip())
                    continue

        # Also capture tests from the failures summary block
        if "failures:" in log:
            failure_block_match = re.search(
                r"^failures:\n([\s\S]*?)\n\n", log, re.MULTILINE
            )
            if failure_block_match:
                for test_line in failure_block_match.group(1).splitlines():
                    test_name = test_line.strip()
                    if test_name and not test_name.startswith("----"):
                        failed_tests.add(test_name)

        # Capture panicked threads as failed tests
        for match in re.finditer(r"thread '(.+?)' panicked at", log):
            test_name = match.group(1).strip()
            if (
                test_name not in passed_tests
                and test_name not in failed_tests
                and test_name != "main"
            ):
                failed_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
