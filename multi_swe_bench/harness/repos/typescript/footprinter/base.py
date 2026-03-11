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
        return "node:20"

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
                """ls
###ACTION_DELIMITER###
yarn install || npm install || pnpm install
###ACTION_DELIMITER###
echo 'yarn test || npm test || pnpm test' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
bun install
bun test
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='bun.lockb' /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
bun install
bun test
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='bun.lockb' /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
bun install
bun test
""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY " + file.name + " /home/\n"

        dockerfile_content = """

FROM node:20

RUN apt-get update && apt-get install -y git curl

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

# Install bun
RUN curl -fsSL https://bun.sh/install | bash

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/tscircuit/footprinter.git /home/footprinter

WORKDIR /home/footprinter
RUN git reset --hard
RUN git checkout {pr_sha}
"""
        dockerfile_content += """
""" + copy_commands
        return dockerfile_content.format(org="tscircuit", repo="footprinter", pr_sha=self.pr.base.sha)


class TscircuitFootprinterInstance(Instance):
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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        re_pass_color = re.compile(r"^\s*✓\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")
        re_fail_color = re.compile(r"^\s*✗\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")
        re_skip_color = re.compile(r"^\s*»\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")
        re_todo_color = re.compile(r"^\s*✎\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")

        re_pass_plain = re.compile(r"^\s*\(pass\)\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")
        re_fail_plain = re.compile(r"^\s*\(fail\)\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")
        re_skip_plain = re.compile(r"^\s*\(skip\)\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")
        re_todo_plain = re.compile(r"^\s*\(todo\)\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")

        for line in log.splitlines():
            line = ansi_escape.sub("", line).strip()
            for pattern, target in [
                (re_pass_color, passed_tests),
                (re_pass_plain, passed_tests),
                (re_fail_color, failed_tests),
                (re_fail_plain, failed_tests),
                (re_skip_color, skipped_tests),
                (re_skip_plain, skipped_tests),
                (re_todo_color, skipped_tests),
                (re_todo_plain, skipped_tests),
            ]:
                m = pattern.match(line)
                if m:
                    target.add(m.group(1))
                    break

        passed_count = len(passed_tests)
        failed_count = len(failed_tests)
        skipped_count = len(skipped_tests)

        return TestResult(
            passed_count=passed_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
