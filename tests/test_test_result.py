"""Unit tests for multi_swe_bench.harness.test_result (MSB-SELFTEST-007).

Covers the patch-file extraction (`get_modified_files`, the basis of the
MSB-REWARD-003 guard), the status mapping (`mapping_to_testresult`), and the
`TestResult` self-consistency invariants enforced in ``__post_init__``.
"""

from __future__ import annotations

import pytest

from multi_swe_bench.harness.test_result import (
    Test,
    TestResult,
    TestStatus,
    get_modified_files,
    mapping_to_testresult,
)

_DIFF_TWO_FILES = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
    "diff --git a/src/b.py b/src/b.py\n"
    "--- a/src/b.py\n"
    "+++ b/src/b.py\n"
    "@@ -1 +1 @@\n"
    "-y = 1\n"
    "+y = 2\n"
)

# a brand-new file: source side is /dev/null
_DIFF_NEW_FILE = (
    "diff --git a/src/new.py b/src/new.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/new.py\n"
    "@@ -0,0 +1 @@\n"
    "+z = 3\n"
)


# --- get_modified_files -----------------------------------------------------
def test_get_modified_files_lists_each_modified_path():
    assert get_modified_files(_DIFF_TWO_FILES) == ["src/a.py", "src/b.py"]


def test_get_modified_files_skips_newly_added_files():
    # source_file is /dev/null for an added file -> excluded (nothing to revert)
    assert get_modified_files(_DIFF_NEW_FILE) == []


def test_get_modified_files_empty_patch_is_empty():
    assert get_modified_files("") == []


# --- mapping_to_testresult --------------------------------------------------
def test_mapping_buckets_statuses_correctly():
    tr = mapping_to_testresult(
        {
            "p": TestStatus.PASSED.value,
            "x": TestStatus.XFAIL.value,  # xfail counts as passed
            "f": TestStatus.FAILED.value,
            "e": TestStatus.ERROR.value,  # error counts as failed
            "s": TestStatus.SKIPPED.value,
        }
    )
    assert tr.passed_tests == {"p", "x"}
    assert tr.failed_tests == {"f", "e"}
    assert tr.skipped_tests == {"s"}
    assert (tr.passed_count, tr.failed_count, tr.skipped_count) == (2, 2, 1)
    assert tr.all_count == 5


def test_mapping_ignores_unknown_status():
    tr = mapping_to_testresult({"weird": "NOT_A_STATUS"})
    assert tr.all_count == 0


def test_mapping_resolves_short_status_in_internal_table():
    tr = mapping_to_testresult({"p": TestStatus.PASSED.value})
    assert tr._tests["p"] == TestStatus.PASS


# --- TestResult.__post_init__ invariants ------------------------------------
def test_count_must_match_set_size():
    with pytest.raises(ValueError, match="passed_count"):
        TestResult(
            passed_count=5,  # lies about the set size
            failed_count=0,
            skipped_count=0,
            passed_tests={"only_one"},
            failed_tests=set(),
            skipped_tests=set(),
        )


def test_overlapping_pass_and_fail_is_rejected():
    with pytest.raises(ValueError, match="common items"):
        TestResult(
            passed_count=1,
            failed_count=1,
            skipped_count=0,
            passed_tests={"t"},
            failed_tests={"t"},  # same test passed AND failed
            skipped_tests=set(),
        )


def test_non_set_tests_is_rejected():
    with pytest.raises(ValueError, match="passed_tests"):
        TestResult(
            passed_count=1,
            failed_count=0,
            skipped_count=0,
            passed_tests=["t"],  # list, not set
            failed_tests=set(),
            skipped_tests=set(),
        )


def test_test_dataclass_holds_three_stage_statuses():
    t = Test(run=TestStatus.PASS, test=TestStatus.FAIL, fix=TestStatus.PASS)
    assert (t.run, t.test, t.fix) == (
        TestStatus.PASS,
        TestStatus.FAIL,
        TestStatus.PASS,
    )
