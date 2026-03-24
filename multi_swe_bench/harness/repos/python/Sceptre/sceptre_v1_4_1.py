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
        return "python:3.6-slim"

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
                """ls
###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
sed -i 's/moto==0.4.31/moto/' requirements.txt
###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
pip install moto==1.3.14
###ACTION_DELIMITER###
coverage run --source sceptre -m pytest
###ACTION_DELIMITER###
echo '#!/bin/bash
coverage run --source sceptre -m pytest' > /home/sceptre/test_commands.sh
###ACTION_DELIMITER###
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
coverage run --source sceptre -m pytest

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
coverage run --source sceptre -m pytest

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
coverage run --source sceptre -m pytest

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

# Choose an appropriate base image based on the project's requirements - replace [base image] with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM python:3.6-slim

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN CODENAME=$(cat /etc/os-release | grep VERSION_CODENAME | cut -d= -f2) && \
    echo "deb [trusted=yes] http://archive.debian.org/debian $CODENAME main" > /etc/apt/sources.list && \
    apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/Sceptre/sceptre.git /home/sceptre

WORKDIR /home/sceptre
RUN git reset --hard
RUN git checkout {pr.base.sha}

# Install project dependencies
RUN sed -i 's/moto==0.4.31/moto/' requirements.txt && \
    pip install -r requirements.txt && \
    pip install moto==1.3.14
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("Sceptre", "sceptre_v1_4_1")
class SCEPTRE_V1_4_1(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        test_file_pattern = re.compile(r"^(tests/[^:]+\.py)")
        failed_pattern = re.compile(r"ERROR collecting (tests/.+\.py)")
        lines = log.splitlines()
        for i, line in enumerate(lines):
            line = line.strip()
            # Check for collection errors.
            failed_match = failed_pattern.search(line)
            if failed_match:
                test_file = failed_match.group(1)
                failed_tests.add(test_file)
                if test_file in passed_tests:
                    passed_tests.remove(test_file)
                if test_file in skipped_tests:
                    skipped_tests.remove(test_file)
                continue
            # Check for test files.
            match = test_file_pattern.match(line)
            if match:
                test_file = match.group(1)
                # Extract status from the rest of the line or the next line.
                status_line = line[len(test_file) :].strip()
                if not status_line and i + 1 < len(lines):
                    next_line_is_test_file = test_file_pattern.match(
                        lines[i + 1].strip()
                    )
                    if not next_line_is_test_file:
                        status_line = lines[i + 1].strip()
                if test_file in failed_tests:
                    continue
                # Prioritize pass over skip
                if "." in status_line:
                    passed_tests.add(test_file)
                    if test_file in skipped_tests:
                        skipped_tests.remove(test_file)
                elif "s" in status_line:
                    if test_file not in passed_tests:
                        skipped_tests.add(test_file)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
