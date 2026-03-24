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
npm install --force
###ACTION_DELIMITER###
echo 'npm run test:ci' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
npm run test:ci

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
npm run test:ci

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
npm run test:ci

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# TanStack/table Era 1 (npm era): PRs 4061-5071, v8.0.13-v8.9.10
# Uses npm install --force, npm run test:ci (jest/vitest)

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


@Instance.register("TanStack", "table_5071_to_4061")
class TABLE_5071_TO_4061(Instance):
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
        # Parse jest/vitest test output for TanStack/table npm era.
        # Handles both jest output (early v8) and vitest output (later v8).
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # --- Jest output parsing ---
        # Individual test results: "PASS react-table packages/react-table/__tests__/..." or "FAIL ..."
        jest_individual = re.findall(
            r"^(PASS|FAIL)\s+\S+\s+(.+)$", log, re.MULTILINE
        )
        for status, test_path in jest_individual:
            test_name = test_path.strip()
            if status == "PASS":
                passed_tests.add(test_name)
            else:
                failed_tests.add(test_name)

        # --- Vitest output parsing (only if jest didn't match) ---
        # Skip vitest parsing if jest already found results to avoid double-counting
        # (vitest FAIL regex also matches jest "FAIL project path" lines)
        if not jest_individual:
            # Passed: "✓ packages/react-table/__tests__/features/Visibility.test.tsx (5 tests) 3ms"
            # Also handles project prefix: "✓ |@tanstack/table-core| tests/file.test.ts (N tests)"
            vitest_passed = re.findall(
                r"^\s*[✓✔]\s+(?:\|[^|]+\|\s+)?(.+?)\s+\(\d+\s+tests?\)", log, re.MULTILINE
            )
            for test_path in vitest_passed:
                passed_tests.add(test_path.strip())

            # Failed vitest: "FAIL packages/react-table/__tests__/features/RowSelection.test.tsx"
            # or "× packages/..." or "✗ packages/..."
            vitest_failed = re.findall(
                r"^\s*(?:FAIL|[×✗])\s+(?:\|[^|]+\|\s+)?(.+?)(?:\s+\(\d+\s+tests?\))?$",
                log,
                re.MULTILINE,
            )
            for test_path in vitest_failed:
                test_name = test_path.strip()
                if test_name:
                    failed_tests.add(test_name)

        # --- Vitest summary parsing (fallback if no individual matches) ---
        # "Test Files  3 failed | 2 passed (5)"
        # "Test Suites: 2 passed, 2 total" (jest summary)
        if not passed_tests and not failed_tests:
            # Try vitest summary: "Test Files  N passed | M failed (total)"
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

            # Try jest summary: "Test Suites:  X failed, Y passed, Z total"
            jest_summary = re.search(
                r"Test Suites:\s+(?:(\d+)\s+failed,\s+)?(\d+)\s+passed,\s+(\d+)\s+total",
                log,
            )
            if jest_summary and not passed_tests and not failed_tests:
                failed_count = int(jest_summary.group(1) or 0)
                passed_count = int(jest_summary.group(2))
                for i in range(passed_count):
                    passed_tests.add(f"test_suite_{i + 1}")
                for i in range(failed_count):
                    failed_tests.add(f"failed_test_suite_{i + 1}")

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
