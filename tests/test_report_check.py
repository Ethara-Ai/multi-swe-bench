"""Unit tests for the grader scoring core: ``Report.check()`` (MSB-SELFTEST-007).

``Report.check()`` is what turns three test-stage results (run / test-patch /
fix-patch) into the valid/resolved verdict and the p2p/f2p/s2p/n2p buckets. It
is the single most consequential piece of untested logic in the grader, so we
exercise every stage:

    1. empty fix result          -> invalid ("no test results were captured")
    2. a PASS->FAIL regression   -> invalid ("the test passed; however ...")
    3. nothing transitions to PASS -> invalid ("no test cases transitioned")
    4. anomalous (NONE/SKIP test, FAIL fix, PASS run) -> invalid
    then: PASS/PASS->p2p, FAIL/PASS->f2p, SKIP/PASS->s2p, NONE/PASS->n2p, valid.

A test absent from a stage is treated as ``TestStatus.NONE`` for that stage
(see ``Report.__post_init__``). ``mapping_to_testresult`` keys off the verified
status *values* ("PASSED"/"FAILED"/"SKIPPED"), which we alias as P/F/S below.
"""

from __future__ import annotations

from multi_swe_bench.harness.report import Report
from multi_swe_bench.harness.test_result import TestStatus, mapping_to_testresult

P = TestStatus.PASSED.value  # "PASSED"
F = TestStatus.FAILED.value  # "FAILED"
S = TestStatus.SKIPPED.value  # "SKIPPED"


def _report(run: dict, test: dict, fix: dict) -> Report:
    return Report(
        org="o",
        repo="r",
        number=1,
        run_result=mapping_to_testresult(run),
        test_patch_result=mapping_to_testresult(test),
        fix_patch_result=mapping_to_testresult(fix),
    )


# --- Stage 1: no fix-patch test results captured ---------------------------
def test_empty_fix_result_is_invalid():
    r = _report(run={"t": P}, test={"t": F}, fix={})
    assert r.valid is False
    assert "no test results were captured" in r.error_msg


# --- Stage 2: a previously-passing test now fails (regression) --------------
def test_new_failure_regression_is_invalid():
    r = _report(run={"t": P}, test={"t": P}, fix={"t": F})
    assert r.valid is False
    assert "the test passed" in r.error_msg and "the test failed" in r.error_msg


# --- Stage 3: nothing went failing->passing --------------------------------
def test_nothing_fixed_is_invalid():
    r = _report(run={"t": F}, test={"t": F}, fix={"t": F})
    assert r.valid is False
    assert "no test cases transitioned from failed to passed" in r.error_msg


# --- Happy path: a genuine failed->passed fix is valid ----------------------
def test_genuine_f2p_fix_is_valid():
    r = _report(run={"t": F}, test={"t": F}, fix={"t": P})
    assert r.valid is True
    assert r.error_msg == ""
    assert "t" in r.f2p_tests
    assert "t" in r.fixed_tests


# --- Stage 4: anomalous pattern (alongside a real fix to pass stage 3) ------
def test_anomalous_pattern_is_invalid():
    r = _report(
        run={"good": F, "anom": P},
        test={"good": F},  # 'anom' absent -> NONE in test stage
        fix={"good": P, "anom": F},
    )
    assert r.valid is False
    assert "anomalous pattern" in r.error_msg


# --- Categorization: p2p + f2p ---------------------------------------------
def test_p2p_and_f2p_categorized():
    r = _report(
        run={"a": F, "b": P},
        test={"a": F, "b": P},
        fix={"a": P, "b": P},
    )
    assert r.valid is True
    assert "a" in r.f2p_tests  # FAIL -> PASS
    assert "b" in r.p2p_tests  # PASS -> PASS
    assert "b" not in r.f2p_tests


# --- Categorization: s2p (SKIP->PASS) + n2p (NONE->PASS) --------------------
def test_s2p_and_n2p_categorized():
    r = _report(
        run={"sk": S},
        test={"sk": S},  # 'nw' absent in test -> NONE
        fix={"sk": P, "nw": P},
    )
    assert r.valid is True
    assert "sk" in r.s2p_tests  # SKIP -> PASS
    assert "nw" in r.n2p_tests  # NONE -> PASS


# --- check() is cached after __post_init__, recomputed only with force ------
def test_check_is_cached_until_forced():
    r = _report(run={"t": F}, test={"t": F}, fix={"t": P})
    assert r.valid is True
    # tamper the cached verdict: a plain check() returns it unchanged ...
    r.valid = False
    r.error_msg = "stale"
    assert r.check() == (False, "stale")
    # ... force=True recomputes from the underlying per-test results.
    valid, msg = r.check(force=True)
    assert valid is True
    assert msg == ""
