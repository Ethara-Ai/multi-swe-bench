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
        return "python:3.11-slim"

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
                """ls -la
###ACTION_DELIMITER###
ls -la .github
###ACTION_DELIMITER###
ls -la .github/workflows
###ACTION_DELIMITER###
pip install poetry==1.7.1
###ACTION_DELIMITER###
poetry install --no-interaction --extras syntax
###ACTION_DELIMITER###
echo 'poetry run pytest tests -v --cov=./src/textual --cov-report=xml:./coverage.xml --cov-report term-missing' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
poetry run pytest tests -v --cov=./src/textual --cov-report=xml:./coverage.xml --cov-report term-missing

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
poetry run pytest tests -v --cov=./src/textual --cov-report=xml:./coverage.xml --cov-report term-missing

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
poetry run pytest tests -v --cov=./src/textual --cov-report=xml:./coverage.xml --cov-report term-missing

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = f"""FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone "${{REPO_URL}}" /home/textual

WORKDIR /home/textual
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{copy_commands}
CMD ["/bin/bash"]
"""
        return dockerfile_content


@Instance.register("Textualize", "textual_4744_to_3140")
class TEXTUAL_4744_TO_3140(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    _POETRY_ENSURE = "pip install poetry==1.7.1 2>/dev/null || true; poetry install --no-interaction --extras syntax 2>/dev/null || true"
    _TEST_CMD = "poetry run pytest tests -v --cov=./src/textual --cov-report=xml:./coverage.xml --cov-report term-missing"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return (
            f"bash -c 'cd /home/textual && {self._POETRY_ENSURE} && {self._TEST_CMD}'"
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return f"bash -c 'cd /home/textual && git apply --whitespace=nowarn /home/test.patch && {self._POETRY_ENSURE} && {self._TEST_CMD}'"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return f"bash -c 'cd /home/textual && git apply --whitespace=nowarn /home/test.patch /home/fix.patch && {self._POETRY_ENSURE} && {self._TEST_CMD}'"

    def parse_log(self, log: str) -> TestResult:
        # Parse the log content and extract test execution results.
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped
        import re
        import json

        # Regex patterns to match test lines
        # Pattern 1: test name followed by status and percentage (e.g., "tests/... PASSED [  0%]")
        pattern1 = re.compile(
            r"^\s*(tests/.*?)\s+(PASSED|FAILED|SKIPPED|XFAILED)\s+\[\s*\d+%\]\s*$",
            re.IGNORECASE,
        )
        # Pattern 2: status followed by test name (e.g., "FAILED tests/...")
        pattern2 = re.compile(
            r"^\s*(PASSED|FAILED|SKIPPED|XFAILED)\s+(tests/[^-]+?)\s*$", re.IGNORECASE
        )
        for line in log.split("\n"):
            line = line.strip()
            match = pattern1.match(line)
            if match:
                test_name = match.group(1).strip()
                status = match.group(2).upper()
            else:
                match = pattern2.match(line)
                if match:
                    status = match.group(1).upper()
                    test_name = match.group(2).strip()
                else:
                    continue  # No match, skip
            # Categorize the test based on status
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)
            elif status == "XFAILED":
                # Handle XFAILED as needed; here, we'll add to failed_tests
                failed_tests.add(test_name)
        parsed_results = {
            "passed_tests": passed_tests,
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
