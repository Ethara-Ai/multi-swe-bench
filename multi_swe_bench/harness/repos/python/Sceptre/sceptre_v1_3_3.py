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
                """ls -F
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
/usr/local/bin/python -m pip install --upgrade pip
###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
sed -i '/moto/d' requirements.txt
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
make coverage-ci
###ACTION_DELIMITER###
apt-get update && apt-get install -y make
###ACTION_DELIMITER###
make coverage-ci
###ACTION_DELIMITER###
pip install moto
###ACTION_DELIMITER###
make coverage-ci
###ACTION_DELIMITER###
echo "make coverage-ci" > /home/sceptre/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
make coverage-ci

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
make coverage-ci

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
make coverage-ci

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
RUN apt-get install -y make && \
    /usr/local/bin/python -m pip install --upgrade pip && \
    sed -i '/moto/d' requirements.txt && \
    pip install -r requirements.txt && \
    pip install moto
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("Sceptre", "sceptre_v1_3_3")
class SCEPTRE_V1_3_3(Instance):
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
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped
        test_pattern = re.compile(r"^(tests\/test_.*\.py)")
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        summary_line = re.search(
            r"=============== (\d+) passed, (\d+) failed, (\d+) skipped ================",
            log,
        )
        if summary_line:
            # This is a placeholder, as we need to identify the actual tests
            pass
        for line in log.splitlines():
            if line.startswith("tests/"):
                if " PASSED" in line:
                    match = test_pattern.match(line)
                    if match:
                        passed_tests.add(match.group(1))
                elif " FAILED" in line:
                    match = test_pattern.match(line)
                    if match:
                        failed_tests.add(match.group(1))
                elif " SKIPPED" in line:
                    match = test_pattern.match(line)
                    if match:
                        skipped_tests.add(match.group(1))
        # Fallback for logs without explicit status per line
        if not passed_tests and not failed_tests and not skipped_tests:
            summary_match = re.search(r"=================== (\d+) passed", log)
            if summary_match:
                # We can't get the names, so we can't add to passed_tests
                pass
            failed_match = re.findall(r"^_ E +([^\n]+)", log, re.MULTILINE)
            for fail in failed_match:
                if "tests/" in fail:
                    failed_tests.add(fail.split("::")[0])
        all_tests = set()
        for line in log.splitlines():
            if line.startswith("tests/test"):
                all_tests.add(line.split("::")[0])
        {
            "passed_tests": all_tests - failed_tests - skipped_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
