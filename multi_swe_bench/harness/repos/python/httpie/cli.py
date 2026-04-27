import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


TEST_CMD = "python -m pytest --no-header -rA --tb=no -p no:cacheprovider"


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
        return "python:3.7-bookworm"

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
                f"""ls -F
###ACTION_DELIMITER###
pip install 'urllib3<2' 'pyOpenSSL<24.0.0'
pip install -e ".[dev]" || (pip install -e . && ([ -f requirements-dev.txt ] && pip install -r requirements-dev.txt || true))
python -m pytest --version || pip install pytest pytest-httpbin
###ACTION_DELIMITER###
{TEST_CMD}
###ACTION_DELIMITER###
echo '{TEST_CMD}' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
{TEST_CMD}

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement /home/test.patch || {{
    echo "Warning: standard apply failed, trying with --reject" >&2
    git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement --reject /home/test.patch || true
}}
{TEST_CMD}

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement /home/test.patch /home/fix.patch || {{
    echo "Warning: combined apply failed, trying separately" >&2
    git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement /home/test.patch || git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement --reject /home/test.patch || true
    git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement /home/fix.patch || git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement --reject /home/fix.patch || true
}}
{TEST_CMD}

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
FROM python:3.7-bookworm

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive
ENV GITHUB_ACTIONS=true

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
RUN git clone https://github.com/httpie/cli.git /home/cli

WORKDIR /home/cli
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install --upgrade pip
RUN pip install 'urllib3<2' 'pyOpenSSL<24.0.0'
RUN pip install -e ".[dev]" || (pip install -e . && ([ -f requirements-dev.txt ] && pip install -r requirements-dev.txt || true))
RUN python -m pytest --version || pip install pytest pytest-httpbin
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("httpie", "cli")
class CLI(Instance):
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
        log = re.sub(r'\x1b\[[0-9;]*m', '', log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        test_results = {}
        pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED(?: \[[\d]+\])?)\s+([^:\s]+)"
        )
        for line in log.splitlines():
            match = pattern.search(line)
            if match:
                status, test_file = match.group(1), match.group(2)
                test_file = test_file.strip()
                if "FAIL" in status or "ERROR" in status:
                    test_results[test_file] = "failed"
                elif "SKIP" in status:
                    if test_results.get(test_file) != "failed":
                        test_results[test_file] = "skipped"
                elif "PASS" in status:
                    if test_results.get(test_file) not in ["failed", "skipped"]:
                        test_results[test_file] = "passed"
        for test_file, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_file)
            elif status == "failed":
                failed_tests.add(test_file)
            elif status == "skipped":
                skipped_tests.add(test_file)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
