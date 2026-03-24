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
        return "python:3.7-alpine"

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
ls -F requirements/
###ACTION_DELIMITER###
pip install -r requirements/prod.txt
###ACTION_DELIMITER###
pip install --upgrade pip
###ACTION_DELIMITER###
apt-get update && apt-get install -y gcc musl-dev libffi-dev
###ACTION_DELIMITER###
apk add --no-cache build-base
###ACTION_DELIMITER###
pip install -r requirements/prod.txt
###ACTION_DELIMITER###
pip install cython
###ACTION_DELIMITER###
pip install -r requirements/prod.txt
###ACTION_DELIMITER###
pip install 'boto3>=1.3.0,<2'
###ACTION_DELIMITER###
pip install 'click>=7.0,<9.0'
###ACTION_DELIMITER###
pip install 'colorama>=0.2.5,<0.4.4'
###ACTION_DELIMITER###
pip install 'cfn-flip>=1.2.3,<2.0'
###ACTION_DELIMITER###
pip install 'deepdiff>=5.5.0,<6.0'
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip install 'Jinja2>=2.8,<3'
###ACTION_DELIMITER###
pip install 'jsonschema>=3.2,<3.3'
###ACTION_DELIMITER###
pip install 'networkx>=2.4,<2.6'
###ACTION_DELIMITER###
pip install 'packaging>=16.8,<17.0'
###ACTION_DELIMITER###
pip install 'PyYaml>=5.1,<6.0'
###ACTION_DELIMITER###
pip install  --no-use-pep517 'PyYaml>=5.1,<6.0'
###ACTION_DELIMITER###
pip install 'sceptre-cmd-resolver>=1.1.3,<2'
###ACTION_DELIMITER###
pip install 'sceptre-file-resolver>=1.0.4,<2'
###ACTION_DELIMITER###

###ACTION_DELIMITER###
pip install 'six>=1.11.0,<2.0.0'
###ACTION_DELIMITER###
pip install -r requirements/dev.txt
###ACTION_DELIMITER###
apk add --no-cache libffi-dev
###ACTION_DELIMITER###
pip install -r requirements/dev.txt
###ACTION_DELIMITER###
pytest
###ACTION_DELIMITER###

###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider' > /home/sceptre/test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --no-header -rA --tb=no -p no:cacheprovider

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
pytest --no-header -rA --tb=no -p no:cacheprovider

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
pytest --no-header -rA --tb=no -p no:cacheprovider

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
FROM python:3.7-alpine

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apk add --no-cache git

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
RUN apk add --no-cache build-base libffi-dev openssl-dev && \
    pip install --upgrade pip cython wheel "setuptools<58" && \
    pip install "pyyaml>=6.0.1" && \
    pip install -r requirements/prod.txt --no-build-isolation && \
    pip install -r requirements/dev.txt --no-build-isolation
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("Sceptre", "sceptre_v2_7_1")
class SCEPTRE_V2_7_1(Instance):
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
        # Implement the log parsing logic here
        # This pattern is to find all collected tests
        collected_tests_pattern = re.compile(r"collected (\d+) items")
        collected_tests_match = collected_tests_pattern.search(log)
        if collected_tests_match:
            # This is just for info, not used in the logic for now
            int(collected_tests_match.group(1))
        # This pattern identifies the final summary line
        final_summary_pattern = re.compile(
            r"(=========== (\d+) failed, (\d+) passed, (\d+) skipped,?(.*) in (.*)s ===========)"
        )
        final_summary_match = final_summary_pattern.search(log)
        if final_summary_match:
            int(final_summary_match.group(2))
            int(final_summary_match.group(3))
            skipped_count = int(final_summary_match.group(4))
        # Lets get the failed tests from the FAILURES section
        failures_section_pattern = re.compile(
            r"================================= FAILURES ==================================(.*?)=========================== short test summary info ============================",
            re.DOTALL,
        )
        failures_section_match = failures_section_pattern.search(log)
        if failures_section_match:
            failures_text = failures_section_match.group(1)
            # The test name is between ____ and ____
            failed_test_pattern = re.compile(
                r"____________________ (.*?) ---------------------"
            )
            failed_test_matches = failed_test_pattern.findall(failures_text)
            for match in failed_test_matches:
                # The name can have newline characters, so we need to clean it
                cleaned_name = "".join(match.splitlines())
                failed_tests.add(cleaned_name)
        # If the above fails, let's try another method for failed tests:
        if not failed_tests:
            short_summary_failures_pattern = re.compile(
                r"=========================== short test summary info ============================(.*)",
                re.DOTALL,
            )
            short_summary_failures_match = short_summary_failures_pattern.search(log)
            if short_summary_failures_match:
                summary_text = short_summary_failures_match.group(1)
                failed_and_error_pattern = re.compile(
                    r"^(?:FAILED|ERROR) (.*?)(?: - .*)?$", re.MULTILINE
                )
                matches = failed_and_error_pattern.findall(summary_text)
                for match in matches:
                    failed_tests.add(match.strip())
        # For skipped tests, they are usually in the format `filename:lineno: reason`
        # and also in the summary. I will use the summary.
        if final_summary_match and skipped_count > 0:
            # Unfortunately, skipped tests are not always listed with their names.
            # Sometimes it is just a count. When they are listed, it's often just a file.
            # Let's try to find them when they are explicitly skipped.
            skipped_pattern = re.compile(r"(tests/.*\.py):\d+: SKIPPED")
            skipped_matches = skipped_pattern.findall(log)
            skipped_tests.update(skipped_matches)
        # For passed tests, we will get all collected tests and subtract the failed and skipped ones.
        test_collection_pattern = re.compile(
            r"(tests/.*?\.py::.*?)(?:   |\s+\[ d+%\]\s+)(?:PASSED|FAILED|SKIPPED|ERROR)"
        )
        set(test_collection_pattern.findall(log))
        # We should also get tests from the dots format
        test_dot_format_pattern = re.compile(r"(tests/.*\.py) (\.s*)+")
        test_dot_format_matches = test_dot_format_pattern.findall(log)
        {match[0] for match in test_dot_format_matches}
        # This is too complex. I will go with a simpler approach.
        # Clear all sets and start fresh with a reliable method.
        passed_tests.clear()
        failed_tests.clear()
        skipped_tests.clear()
        summary_block_pattern = re.compile(
            r"=========================== short test summary info ============================(.*?)=================================",
            re.DOTALL,
        )
        summary_block_match = summary_block_pattern.search(log)
        if summary_block_match:
            summary_block = summary_block_match.group(1)
            failed_pattern = re.compile(
                r"^(?:FAILED|ERROR) (.*?)(?: - .*)?$", re.MULTILINE
            )
            failed_matches = failed_pattern.findall(summary_block)
            for match in failed_matches:
                failed_tests.add(match.strip())
        # If we couldn't find the summary block, let's look for FAILED lines
        if not failed_tests:
            failed_pattern = re.compile(r"^FAILED (.*?)(?: - .*)?$", re.MULTILINE)
            failed_matches = failed_pattern.findall(log)
            for match in failed_matches:
                failed_tests.add(match.strip())
        # For passed tests, let's look for PASSED lines or dots.
        # This is not reliable as passed tests are not listed.
        # Let's use the totals from the final summary.
        summary_totals_pattern = re.compile(
            r"(\d+) failed, (\d+) passed, (\d+) skipped"
        )
        summary_totals_match = summary_totals_pattern.search(log)
        if summary_totals_match:
            int(summary_totals_match.group(1))
            int(summary_totals_match.group(2))
            skipped_count = int(summary_totals_match.group(3))
        # Now, let's populate the sets based on the counts.
        # We can't get the names of passed tests if they aren't listed.
        # The prompt says "Test names should be extracted cleanly without including error messages..."
        # This implies the names are there.
        # Let's go back to finding all tests, then separating them.
        all_tests_pattern = re.compile(r"(tests/.*?\.py::.*?)", re.MULTILINE)
        all_tests = set(all_tests_pattern.findall(log))
        for test in all_tests:
            # Check the line containing the test for its status
            for line in log.splitlines():
                if test in line:
                    if "PASSED" in line or "." in line.split(test)[-1]:
                        passed_tests.add(test)
                        break
                    elif (
                        "FAILED" in line
                        or "F" in line.split(test)[-1]
                        or "ERROR" in line
                        or "E" in line.split(test)[-1]
                    ):
                        failed_tests.add(test)
                        break
                    elif "SKIPPED" in line or "s" in line.split(test)[-1]:
                        skipped_tests.add(test)
                        break
            else:
                # if not status found assume it passed
                passed_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
