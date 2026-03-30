import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest verbose output for tortoise-orm tests.

    Handles both test directories: tortoise/tests/ and tests/
    Patterns:
      PASSED:  tests/test_foo.py::TestBar::test_baz PASSED
      FAILED:  FAILED tests/test_foo.py::TestBar::test_baz
      SKIPPED: tests/test_foo.py::TestBar::test_baz SKIPPED
      XFAIL:   tests/test_foo.py::TestBar::test_baz XFAIL
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    # Pattern for PASSED/SKIPPED/XFAIL at end of line
    pattern_status_after = re.compile(
        r"^((?:tests|tortoise/tests)/.*)::(.*) (PASSED|SKIPPED|XFAIL)"
    )
    # Pattern for FAILED at start of line
    pattern_failed = re.compile(r"^FAILED ((?:tests|tortoise/tests)/.*)::(.*)")

    for line in log.splitlines():
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
