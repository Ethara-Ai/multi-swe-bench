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
        return "node:12"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
                """echo "=== Rotki Frontend Test Setup ==="
###ACTION_DELIMITER###
echo "Step 1: Checking directory structure..."
###ACTION_DELIMITER###
ls -la
###ACTION_DELIMITER###
echo "Step 2: Checking for frontend/app directory..."
###ACTION_DELIMITER###
if [ -d "frontend/app" ]; then echo "frontend/app exists"; ls -la frontend/app/; fi
###ACTION_DELIMITER###
echo "Step 3: Checking for electron-app directory..."
###ACTION_DELIMITER###
if [ -d "electron-app" ]; then echo "electron-app exists"; ls -la electron-app/; fi
###ACTION_DELIMITER###
echo "Step 4: Examining package.json scripts..."
###ACTION_DELIMITER###
if [ -f "frontend/app/package.json" ]; then echo "=== frontend/app/package.json scripts ==="; cat frontend/app/package.json | grep -A 20 '"scripts"'; fi
###ACTION_DELIMITER###
if [ -f "electron-app/package.json" ]; then echo "=== electron-app/package.json scripts ==="; cat electron-app/package.json | grep -A 20 '"scripts"'; fi
###ACTION_DELIMITER###
echo "Step 5: Installing dependencies (frontend/app)..."
###ACTION_DELIMITER###
if [ -d "frontend/app" ]; then cd frontend/app && npm install --ignore-scripts && cd ..; fi
###ACTION_DELIMITER###
echo "Step 6: Fallback install with legacy-peer-deps (frontend/app)..."
###ACTION_DELIMITER###
if [ -d "frontend/app" ]; then cd frontend/app && npm install --legacy-peer-deps --ignore-scripts && cd ..; fi
###ACTION_DELIMITER###
echo "Step 7: Installing dependencies (electron-app)..."
###ACTION_DELIMITER###
if [ -d "electron-app" ]; then cd electron-app && npm install --ignore-scripts; fi
###ACTION_DELIMITER###
echo "Step 8: Fallback install with legacy-peer-deps (electron-app)..."
###ACTION_DELIMITER###
if [ -d "electron-app" ]; then cd electron-app && npm install --legacy-peer-deps --ignore-scripts; fi
###ACTION_DELIMITER###
echo "Step 9: Verifying test script availability..."
###ACTION_DELIMITER###
if [ -d "frontend/app" ]; then cd frontend/app && npm run test:unit -- --version 2>/dev/null || echo "test:unit not available in frontend/app"; cd ..; fi
###ACTION_DELIMITER###
if [ -d "electron-app" ]; then cd electron-app && npm run test:unit -- --version 2>/dev/null || echo "test:unit not available in electron-app"; fi
###ACTION_DELIMITER###
echo "=== Setup Complete ===""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
(cd frontend/app && npm run test:unit -- --verbose) || (cd electron-app && npm run test:unit -- --verbose)

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
(cd frontend/app && npm run test:unit -- --verbose) || (cd electron-app && npm run test:unit -- --verbose)

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
(cd frontend/app && npm run test:unit -- --verbose) || (cd electron-app && npm run test:unit -- --verbose)

""".replace("[[REPO_NAME]]", repo_name),
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
FROM node:12

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/rotki/rotki.git /home/rotki

WORKDIR /home/rotki
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("rotki", "rotki_1613_to_902")
class ROTKI_1613_TO_902(Instance):
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

        stack = []
        # Regex to match test cases with status markers (✓, ✕, ○) and optional duration
        test_re = re.compile(r"^(\s*)(✓|✕|○)\s*(.*?)(\s*\(\d+ms\))?$")
        # Regex to match suite lines (indented, non-test lines)
        suite_re = re.compile(r"^(\s*)(.*?)\s*$")
        for line in log.split("\n"):
            line = line.rstrip("\r")
            # Check for test cases
            test_match = test_re.match(line)
            if test_match:
                indent, status, test_name, _ = test_match.groups()
                indent_level = len(indent)
                # Update stack to current suite hierarchy based on indentation
                while stack and stack[-1][1] >= indent_level:
                    stack.pop()
                # Build full test name from stack
                suite_path = " › ".join([s[0] for s in stack])
                full_name = f"{suite_path} › {test_name}" if suite_path else test_name
                # Classify test status
                if status == "✓":
                    passed_tests.add(full_name)
                elif status == "✕":
                    failed_tests.add(full_name)
                elif status == "○":
                    skipped_tests.add(full_name)
                continue
            # Check for suite lines (update stack)
            suite_match = suite_re.match(line)
            if suite_match:
                indent, suite_name = suite_match.groups()
                indent_level = len(indent)
                # Strip 'FAIL'/'PASS' prefix from suite names
                if suite_name.startswith(("FAIL", "PASS")):
                    suite_name = suite_name.split(" ", 1)[1].strip()
                # Skip lines with errors, expect, or other non-suite content
                if (
                    not suite_name
                    or "at " in suite_name
                    or "Error" in suite_name
                    or "expect" in suite_name
                ):
                    continue
                # Update stack: remove deeper levels, then add current suite
                while stack and stack[-1][1] >= indent_level:
                    stack.pop()
                stack.append((suite_name, indent_level))
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
