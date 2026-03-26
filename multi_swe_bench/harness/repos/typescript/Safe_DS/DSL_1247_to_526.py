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
        return "node:20-bookworm"

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
                """#!/bin/bash
set -e
cd /home/[[REPO_NAME]]
npm ci 2>/dev/null || npm install
npm run langium:generate 2>/dev/null || true
npm run build
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/[[REPO_NAME]]
rm -f /tmp/results.json
npx vitest --reporter=json --outputFile=/tmp/results.json --reporter=verbose --run 2>&1 || true
echo "###VITEST_JSON_START###"
cat /tmp/results.json 2>/dev/null || echo '{"testResults":[],"numTotalTests":0}'
echo ""
echo "###VITEST_JSON_END###"
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='package-lock.json' --exclude='**/package-lock.json' /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npm run langium:generate 2>/dev/null || true
npm run build 2>&1 || true
rm -f /tmp/results.json
npx vitest --reporter=json --outputFile=/tmp/results.json --reporter=verbose --run 2>&1 || true
echo "###VITEST_JSON_START###"
cat /tmp/results.json 2>/dev/null || echo '{"testResults":[],"numTotalTests":0}'
echo ""
echo "###VITEST_JSON_END###"
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='package-lock.json' --exclude='**/package-lock.json' /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npm run langium:generate 2>/dev/null || true
npm run build 2>&1 || true
rm -f /tmp/results.json
npx vitest --reporter=json --outputFile=/tmp/results.json --reporter=verbose --run 2>&1 || true
echo "###VITEST_JSON_START###"
cat /tmp/results.json 2>/dev/null || echo '{"testResults":[],"numTotalTests":0}'
echo ""
echo "###VITEST_JSON_END###"
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

# Choose an appropriate base image based on the project's requirements - replace node:20-bookworm with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM node:20-bookworm

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
RUN git clone https://github.com/Safe-DS/DSL.git /home/DSL

WORKDIR /home/DSL
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("Safe-DS", "DSL_1247_to_526")
class DSL_1247_TO_526(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        start_marker = "###VITEST_JSON_START###"
        end_marker = "###VITEST_JSON_END###"
        start_idx = log.find(start_marker)
        end_idx = log.find(end_marker)

        if start_idx != -1 and end_idx != -1:
            json_str = log[start_idx + len(start_marker):end_idx].strip()
            try:
                data = json.loads(json_str)
                for test_file in data.get("testResults", []):
                    file_path = test_file.get("name", "")
                    for assertion in test_file.get("assertionResults", []):
                        full_name = assertion.get("fullName", "").strip()
                        status = assertion.get("status", "")
                        if not full_name:
                            title = assertion.get("title", "unknown")
                            ancestors = assertion.get("ancestorTitles", [])
                            full_name = " ".join(
                                [a for a in ancestors if a] + [title]
                            ).strip()
                        if not full_name:
                            continue
                        if status == "passed":
                            passed_tests.add(full_name)
                        elif status == "failed":
                            failed_tests.add(full_name)
                        elif status in ("skipped", "pending", "todo", "disabled"):
                            skipped_tests.add(full_name)
            except json.JSONDecodeError:
                pass

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
