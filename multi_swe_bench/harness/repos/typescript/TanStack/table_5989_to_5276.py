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
                """ls -la
###ACTION_DELIMITER###
npm install -g pnpm@9.3.0
###ACTION_DELIMITER###
pnpm install --no-frozen-lockfile
###ACTION_DELIMITER###
echo 'pnpm run test:ci' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
pnpm run test:ci

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
pnpm run test:ci

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
pnpm run test:ci

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# TanStack/table Era 2 (pnpm era): PRs 5276-5989, v8.11.6-v8.21.3
# Uses pnpm install --no-frozen-lockfile, pnpm run test:ci (vitest via nx)

FROM node:20

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
RUN git clone https://github.com/TanStack/table.git /home/table

WORKDIR /home/table
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("TanStack", "table_5989_to_5276")
class TABLE_5989_TO_5276(Instance):
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
        # Parse vitest/nx test output for TanStack/table pnpm era.
        # NX runs vitest for multiple projects via "pnpm run test:ci" →
        # "nx run-many --targets=test:format,test:lib,test:types,build"
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # --- NX project-level parsing ---
        # "NX   Successfully ran target test:lib for 3 projects"
        # or "NX   Running target test:lib for 3 projects"
        # Extract individual project results from nx run output

        # Per-project vitest results:
        # "> nx run @tanstack/table-core:\"test:lib\""
        # Followed by vitest output with passed/failed files

        # Individual vitest test file results:
        # "✓ __tests__/Pinning.test.ts (7 tests) 9ms"
        # "✓ |@tanstack/table-core| tests/RowPinning.test.ts (7 tests) 9ms"
        vitest_passed = re.findall(
            r"^\s*[✓✔]\s+(?:\|([^|]+)\|\s+)?(.+?)\s+\(\d+\s+tests?\)",
            log,
            re.MULTILINE,
        )
        for project_prefix, test_path in vitest_passed:
            if project_prefix:
                test_name = f"{project_prefix.strip()}::{test_path.strip()}"
            else:
                test_name = test_path.strip()
            passed_tests.add(test_name)

        # Failed vitest tests:
        # "FAIL packages/react-table/__tests__/..." or "× ..." or "✗ ..."
        vitest_failed = re.findall(
            r"^\s*(?:FAIL|[×✗])\s+(?:\|([^|]+)\|\s+)?(.+?)(?:\s+\(\d+\s+tests?\))?$",
            log,
            re.MULTILINE,
        )
        for project_prefix, test_path in vitest_failed:
            test_name_raw = test_path.strip()
            if test_name_raw:
                if project_prefix:
                    test_name = f"{project_prefix.strip()}::{test_name_raw}"
                else:
                    test_name = test_name_raw
                failed_tests.add(test_name)

        # --- NX target-level fallback ---
        # If no individual test files found, try NX project-level success/failure
        if not passed_tests and not failed_tests:
            # "NX   Successfully ran target test:lib for N projects"
            nx_success = re.findall(
                r"NX\s+Successfully ran target[s]?\s+(.+?)\s+for\s+(\d+)\s+project",
                log,
            )
            for target, count in nx_success:
                for i in range(int(count)):
                    passed_tests.add(f"nx::{target.strip()}::project_{i + 1}")

            # "NX   Ran target test:lib for N projects (M failed)"
            nx_failed = re.findall(
                r"NX\s+Ran target[s]?\s+(.+?)\s+for\s+(\d+)\s+project.*?(\d+)\s+failed",
                log,
            )
            for target, total, fail_count in nx_failed:
                for i in range(int(fail_count)):
                    failed_tests.add(f"nx::{target.strip()}::failed_project_{i + 1}")
                passed_count = int(total) - int(fail_count)
                for i in range(passed_count):
                    passed_tests.add(f"nx::{target.strip()}::project_{i + 1}")

        # --- Vitest summary fallback ---
        # "Test Files  3 passed | 1 failed (4)"
        if not passed_tests and not failed_tests:
            vitest_summary = re.search(
                r"Test Files\s+(?:(\d+)\s+failed\s+\|)?\s*(\d+)\s+passed",
                log,
            )
            if vitest_summary:
                failed_count = int(vitest_summary.group(1) or 0)
                passed_count = int(vitest_summary.group(2))
                for i in range(passed_count):
                    passed_tests.add(f"test_file_{i + 1}")
                for i in range(failed_count):
                    failed_tests.add(f"failed_test_file_{i + 1}")

        # Dedup: if a test appears in both passed and failed, it failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
