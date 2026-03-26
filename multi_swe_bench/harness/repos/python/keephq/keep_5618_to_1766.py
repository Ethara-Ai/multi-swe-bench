from __future__ import annotations
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Per-PR test targets and extra pytest args
# Each PR maps to (test_paths, extra_args)
PR_TEST_CONFIG = {
    5618: {
        "test_paths": "tests/test_servicenow_provider.py",
        "extra_args": "--ignore=tests/providers/jira_provider/",
    },
    4721: {
        "test_paths": "keep/providers/fluxcd_provider/test_fluxcd_provider.py",
        "extra_args": "",
    },
    1766: {
        "test_paths": "tests/test_workflow_execution.py",
        "extra_args": "",
    },
}


def _get_test_config(pr_number: int) -> dict:
    return PR_TEST_CONFIG.get(pr_number, {
        "test_paths": "tests/",
        "extra_args": "",
    })


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
        repo_name = self.pr.repo
        tc = _get_test_config(self.pr.number)
        test_paths = tc["test_paths"]
        extra_args = tc["extra_args"]

        pytest_cmd = f"poetry run pytest --no-header -rA --tb=no -p no:cacheprovider -v {extra_args} {test_paths}".strip()
        pytest_cmd = re.sub(r"  +", " ", pytest_cmd)

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
                f"""ls
###ACTION_DELIMITER###
curl -sSL https://install.python-poetry.org | python3 -
###ACTION_DELIMITER###
apt-get update && apt-get install -y curl
###ACTION_DELIMITER###
curl -sSL https://install.python-poetry.org | python3 -
###ACTION_DELIMITER###
export PATH="/root/.local/bin:$PATH"
###ACTION_DELIMITER###
poetry install --with dev
###ACTION_DELIMITER###
apt-get update && apt-get install -y python3.11
###ACTION_DELIMITER###
poetry env use python3.11
###ACTION_DELIMITER###
poetry install --with dev
###ACTION_DELIMITER###
echo '{pytest_cmd}' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
export PATH="/root/.local/bin:$PATH"
cd /home/{repo_name}
{pytest_cmd}

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
export PATH="/root/.local/bin:$PATH"
cd /home/{repo_name}
# Apply test patch, filtering out binary diffs if needed
if ! git apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    git checkout -- . 2>/dev/null
    git clean -fd 2>/dev/null
    python3 -c "
import re, sys
patch = open('/home/test.patch').read()
parts = re.split(r'(?=^diff --git )', patch, flags=re.MULTILINE)
filtered = [p for p in parts if p.strip() and 'Binary files' not in p and 'GIT binary patch' not in p]
sys.stdout.write(''.join(filtered))
" > /tmp/text_only.patch
    git apply --whitespace=nowarn /tmp/text_only.patch
fi
{pytest_cmd}

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
export PATH="/root/.local/bin:$PATH"
cd /home/{repo_name}
# Apply test patch and fix patch separately, filtering out binary diffs if needed
for patchfile in /home/test.patch /home/fix.patch; do
    if ! git apply --whitespace=nowarn "$patchfile" 2>/dev/null; then
        python3 -c "
import re, sys
patch = open('$patchfile').read()
parts = re.split(r'(?=^diff --git )', patch, flags=re.MULTILINE)
filtered = [p for p in parts if p.strip() and 'Binary files' not in p and 'GIT binary patch' not in p]
sys.stdout.write(''.join(filtered))
" > /tmp/text_only.patch
        if [ -s /tmp/text_only.patch ]; then
            git apply --whitespace=nowarn /tmp/text_only.patch
        fi
    fi
done
{pytest_cmd}

""",
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
FROM python:3.11-slim

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git curl

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

# Install Poetry
ENV PATH="/root/.local/bin:$PATH"
RUN curl -sSL https://install.python-poetry.org | python3 -

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/keephq/keep.git /home/keep

WORKDIR /home/keep
RUN git reset --hard
RUN git checkout {pr.base.sha}

# Install project dependencies
RUN poetry install --with dev --no-interaction
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("keephq", "keep_5618_to_1766")
class KEEP_5618_TO_1766(Instance):
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
        import re

        # Regex patterns to match test results
        # Match test paths starting with tests/ OR keep/ (for provider-level tests like PR-4721)
        pattern = re.compile(
            r"(?:\[\s*\d+\]\s+)?((?:tests|keep)/.*?)\s+(PASSED|FAILED|SKIPPED)"
        )
        for line in log.split("\n"):
            line = line.strip()
            match = pattern.search(line)
            if match:
                # Extract test name and status from either group position
                test_name = match.group(1)
                status = match.group(2)
                test_name = test_name.strip()
                status = status.strip()
            else:
                continue
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)
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
