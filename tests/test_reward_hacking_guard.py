"""Unit tests for the MSB-REWARD-003 reward-hacking guard.

The guard (`multi_swe_bench.harness.test_result.fix_patch_tampers_with_tests`)
rejects an agent fix patch that modifies any gold test file, so the agent cannot
game the score by editing the very tests that determine pass/fail. Enforced at
the grader in `run_evaluation.run_instance` (refuses to run such a patch).
"""

from __future__ import annotations

from multi_swe_bench.harness.test_result import fix_patch_tampers_with_tests

GOLD_TEST = (
    "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
    "--- a/tests/test_foo.py\n"
    "+++ b/tests/test_foo.py\n"
    "@@ -1 +1,2 @@\n"
    " import foo\n"
    "+def test_new(): assert foo.f() == 2\n"
)

# agent edits the gold test itself (reward hacking)
CHEAT_FIX = (
    "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
    "--- a/tests/test_foo.py\n"
    "+++ b/tests/test_foo.py\n"
    "@@ -1 +1,2 @@\n"
    " import foo\n"
    "+def test_new(): assert True\n"
)

# honest fix: source only
LEGIT_FIX = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1 +1 @@\n"
    "-def f(): return 1\n"
    "+def f(): return 2\n"
)

# fix that touches source AND sneaks an edit into the gold test
MIXED_FIX = LEGIT_FIX + CHEAT_FIX


def test_fix_editing_gold_test_is_rejected():
    assert fix_patch_tampers_with_tests(CHEAT_FIX, GOLD_TEST) == ["tests/test_foo.py"]


def test_legit_source_only_fix_is_allowed():
    assert fix_patch_tampers_with_tests(LEGIT_FIX, GOLD_TEST) == []


def test_mixed_fix_touching_a_gold_test_is_rejected():
    # an otherwise-legit fix that also edits a gold test is still caught
    assert fix_patch_tampers_with_tests(MIXED_FIX, GOLD_TEST) == ["tests/test_foo.py"]


def test_empty_fix_patch_is_allowed():
    assert fix_patch_tampers_with_tests("", GOLD_TEST) == []


def test_malformed_fix_patch_does_not_crash():
    # garbage in -> [] (it will fail to apply anyway), never an exception
    assert fix_patch_tampers_with_tests("not a unified diff", GOLD_TEST) == []
