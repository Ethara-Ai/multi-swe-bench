import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


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

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """ls -la
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel poetry-core hatchling poetry
###ACTION_DELIMITER###
poetry install --all-extras 2>/dev/null || pip install -e ".[dev,test]" 2>/dev/null || pip install -e ".[test,graph,vector_stores,llms,extras]" 2>/dev/null || pip install -e ".[test]" 2>/dev/null || pip install -e . 2>/dev/null || true
###ACTION_DELIMITER###
pip install pytest pytest-mock pytest-asyncio pytest-env || true""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
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
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential curl libgeos-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install --upgrade pip setuptools wheel poetry-core hatchling poetry || true
RUN poetry install --all-extras 2>/dev/null || pip install -e ".[dev,test]" 2>/dev/null || pip install -e ".[test,graph,vector_stores,llms,extras]" 2>/dev/null || pip install -e ".[test]" 2>/dev/null || pip install -e . 2>/dev/null || true
RUN pip install pytest pytest-mock pytest-asyncio pytest-env || true

{copy_commands}
""".format(pr=self.pr, copy_commands=copy_commands)


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v --no-header -rA output.

    Regex patterns match:
      PASSED:  tests/llms/test_openai.py::TestOpenAI::test_foo PASSED
      FAILED:  FAILED tests/llms/test_openai.py::TestOpenAI::test_foo
      SKIPPED: tests/llms/test_openai.py::TestOpenAI::test_foo SKIPPED
      XFAIL:   tests/llms/test_openai.py::TestOpenAI::test_foo XFAIL
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    pattern_status_after = re.compile(
        r"^((?:tests|embedchain/tests)/.*)::(.*) (PASSED|SKIPPED|XFAIL)"
    )
    pattern_failed = re.compile(
        r"^FAILED ((?:tests|embedchain/tests)/.*)::(.*)"
    )

    for line in log.splitlines():
        line = ANSI_ESCAPE.sub("", line).strip()
        match_status_after = pattern_status_after.match(line)
        if match_status_after:
            test_path = match_status_after.group(1)
            test_name = match_status_after.group(2)
            status = match_status_after.group(3)
            full_test_name = f"{test_path}::{test_name}"
            if status == "PASSED":
                passed_tests.add(full_test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(full_test_name)
            continue
        match_failed = pattern_failed.match(line)
        if match_failed:
            test_path = match_failed.group(1)
            test_name = match_failed.group(2)
            full_test_name = f"{test_path}::{test_name}"
            failed_tests.add(full_test_name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("mem0ai", "mem0")
class MEM0(Instance):
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
        return parse_pytest_log(log)
