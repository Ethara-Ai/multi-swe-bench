"""Regression tests for the rust-ndarray/ndarray findings audit (2026-07-21).

Two independent guards are pinned here:

1. report.py cheating guard — a production-only Rust fix (inline #[cfg(test)]
   tests, zero gold-test overlap) must stay valid=True. Pins commit 89dc5e4
   so the Java/Rust naming false positive cannot silently return.
2. ndarray.parse_log — a test name reported both `ok` and `FAILED` across cargo
   binary targets must not raise ValueError (fail-closed dedup).
"""

from multi_swe_bench.harness.report import Report
from multi_swe_bench.harness.test_result import TestResult


# --------------------------------------------------------------------------- #
# 1. report.py cheating guard: production-only Rust fix stays valid (Fix A)
# --------------------------------------------------------------------------- #

_NAMES = ["insert_axis", "max_stride_axis", "merge_axes", "remove_axis", "slice_mut"]


def _tr(passed):
    s = set(passed)
    return TestResult(len(s), 0, 0, s, set(), set())


def _rust_fix_patch():
    """Fix touches only production src/*.rs (+Cargo.toml); each hunk mentions a
    credited test name, exactly the name/file collision that used to false-fire."""
    files = {
        "insert_axis": "src/impl_methods.rs",
        "max_stride_axis": "src/dimension/mod.rs",
        "merge_axes": "src/impl_methods.rs",
        "remove_axis": "src/dimension/mod.rs",
        "slice_mut": "src/slice.rs",
    }
    parts, seen = [], set()
    for name, f in files.items():
        if f not in seen:
            seen.add(f)
            parts.append(f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n")
        parts.append(
            f"@@ -10,2 +10,3 @@\n     // ctx\n"
            f"+    pub fn {name}(self, a: Axis) -> Self {{ /* fix */ }}\n"
        )
    parts.append(
        "diff --git a/Cargo.toml b/Cargo.toml\n--- a/Cargo.toml\n+++ b/Cargo.toml\n"
        "@@ -1 +1,2 @@\n v=1\n+e=2\n"
    )
    return "".join(parts)


_RUST_TEST_PATCH = (
    "diff --git a/src/impl_special_element_types.rs b/src/impl_special_element_types.rs\n"
    "--- a/src/impl_special_element_types.rs\n"
    "+++ b/src/impl_special_element_types.rs\n"
    "@@ -1 +1,2 @@\n use crate::ArrayParts;\n+// scaffolding\n"
)


def test_rust_production_only_fix_not_flagged():
    r = Report(
        org="rust-ndarray", repo="ndarray", number=1532,
        run_result=_tr(_NAMES), test_patch_result=_tr([]), fix_patch_result=_tr(_NAMES),
        test_patch=_RUST_TEST_PATCH, fix_patch=_rust_fix_patch(),
    )
    assert r.valid is True, r.error_msg
    assert r.guard_fix_patch_touched_tests == {}
    assert r._fix_patch_test_added == {}          # production src/*.rs filtered out
    assert set(r.fixed_tests) == set(_NAMES)


# --------------------------------------------------------------------------- #
# 2. ndarray.parse_log: cross-classified name must not crash (Fix B)
# --------------------------------------------------------------------------- #

def _parse(log):
    from multi_swe_bench.harness.repos.rust.rust_ndarray.ndarray import Ndarray
    return Ndarray.parse_log(None, log)           # self unused


def test_ndarray_parse_log_dedups_cross_classified_name():
    # Same name in two cargo binaries: passes in one, fails in the other.
    tr = _parse("test insert_axis ... ok\ntest insert_axis ... FAILED\n")
    assert "insert_axis" in tr.failed_tests       # fail-closed: failed wins
    assert "insert_axis" not in tr.passed_tests
    assert tr.passed_tests & tr.failed_tests == set()


def test_ndarray_parse_log_normal_path_unchanged():
    """Baseline behaviour must be untouched: distinct names route to their
    bucket and counts equal set sizes (no accidental dedup on clean input)."""
    tr = _parse(
        "test a ... ok\n"
        "test b ... ok\n"
        "test c ... FAILED\n"
        "test d ... ignored\n"
    )
    assert tr.passed_tests == {"a", "b"}
    assert tr.failed_tests == {"c"}
    assert tr.skipped_tests == {"d"}
    assert (tr.passed_count, tr.failed_count, tr.skipped_count) == (2, 1, 1)


def test_ndarray_parse_log_ok_and_ignored_are_disjoint():
    # Same name ok in one binary, ignored in another (no failure).
    tr = _parse("test t ... ok\ntest t ... ignored\n")
    assert tr.passed_tests & tr.skipped_tests == set()
    assert "t" in tr.skipped_tests and "t" not in tr.passed_tests


def test_ndarray_parse_log_name_in_all_three_buckets():
    # Worst case: ok + FAILED + ignored for one name -> failed wins, all disjoint.
    tr = _parse("test x ... ok\ntest x ... FAILED\ntest x ... ignored\n")
    assert tr.failed_tests == {"x"}
    assert "x" not in tr.passed_tests and "x" not in tr.skipped_tests
    assert tr.passed_tests & tr.failed_tests == set()
    assert tr.passed_tests & tr.skipped_tests == set()
    assert tr.failed_tests & tr.skipped_tests == set()


def test_ndarray_parse_log_empty_is_safe():
    tr = _parse("")
    assert (tr.passed_count, tr.failed_count, tr.skipped_count) == (0, 0, 0)
