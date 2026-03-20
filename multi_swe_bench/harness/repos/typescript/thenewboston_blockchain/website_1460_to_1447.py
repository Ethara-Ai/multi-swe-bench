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
        return "node:18-bullseye"

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
cd /home/Website
rm -f src/components/GoToTop/GoToTop.test.tsx
node -e "const p=require('./package.json');p.resolutions=p.resolutions||{};p.resolutions['@babel/plugin-syntax-import-attributes']='7.22.5';p.resolutions['@babel/core']='7.22.5';require('fs').writeFileSync('./package.json',JSON.stringify(p,null,2));"
yarn install --ignore-scripts --network-timeout 300000""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
CI=true yarn test
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo \"Error: git apply failed\" >&2
    exit 1  
fi
CI=true yarn test
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo \"Error: git apply failed\" >&2
    exit 1  
fi
CI=true yarn test
""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY " + file.name + " /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

FROM node:18-bullseye

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/thenewboston-blockchain/Website.git /home/Website

WORKDIR /home/Website
RUN git reset --hard
RUN git checkout {pr_sha}
"""
        dockerfile_content += """
""" + copy_commands + "\nRUN bash /home/prepare.sh\n"
        return dockerfile_content.format(pr_sha=self.pr.base.sha)


@Instance.register("thenewboston-blockchain", "website_1460_to_1447")
class WEBSITE_1460_TO_1447(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_test = re.compile(r"^\s*✓\s+(.+?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?$")
        re_fail_test = re.compile(r"^\s*✕\s+(.+?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?$")
        re_skip_test = re.compile(r"^\s*○\s+(.+?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?$")
        re_fail_jest_error = re.compile(r"^\s*●\s+.+?\s+›\s+(.+?)$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass_test.match(line)
            if pass_match:
                test_name = pass_match.group(1)
                passed_tests.add(test_name)
                continue

            fail_match = re_fail_test.match(line)
            if fail_match:
                test_name = fail_match.group(1)
                failed_tests.add(test_name)
                continue

            skip_match = re_skip_test.match(line)
            if skip_match:
                test_name = skip_match.group(1)
                skipped_tests.add(test_name)
                continue

            jest_error_match = re_fail_jest_error.match(line)
            if jest_error_match:
                test_name = jest_error_match.group(1)
                failed_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
