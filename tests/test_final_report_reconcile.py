"""Unit tests for the MSB-ROLLOUT-004 fail-closed pass-rate recompute.

`FinalReport.from_reports` must recompute its counts from the raw per-instance
buckets instead of trusting list lengths, so a dropped, duplicated, or
miscounted instance cannot silently move the pass rate. The function only reads
``.id`` off each report/task, so lightweight stand-ins suffice here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from multi_swe_bench.harness.report import FinalReport


def _r(instance_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=instance_id)


def test_clean_set_reconciles():
    fr = FinalReport.from_reports(
        reports=[_r("a"), _r("b")],  # resolved
        invalid_reports=[_r("c")],  # unresolved
        failed_tasks=[_r("d")],  # error
        expected_ids={"a", "b", "c", "d"},
    )
    assert fr.resolved_instances == 2
    assert fr.unresolved_instances == 1
    assert fr.error_instances == 1
    assert fr.submitted_instances == 4
    assert fr.completed_instances + fr.incomplete_instances == 4


def test_dropped_instance_counts_as_error_not_resolved():
    # "e" is expected but produced no terminal report (silently dropped).
    fr = FinalReport.from_reports(
        reports=[_r("a")],
        invalid_reports=[],
        failed_tasks=[],
        expected_ids={"a", "e"},
    )
    assert fr.resolved_instances == 1  # the drop did NOT inflate resolved
    assert "e" in fr.error_ids  # it is folded into error
    assert fr.error_instances == 1
    assert fr.submitted_instances == 2  # denominator still counts it


def test_same_instance_in_two_buckets_raises():
    with pytest.raises(ValueError, match="more than one"):
        FinalReport.from_reports(
            reports=[_r("a")],
            invalid_reports=[_r("a")],  # also unresolved -> collision
            failed_tasks=[],
        )


def test_duplicate_within_bucket_raises():
    with pytest.raises(ValueError, match="duplicate instance id"):
        FinalReport.from_reports(
            reports=[_r("a"), _r("a")],
            invalid_reports=[],
            failed_tasks=[],
        )


def test_graded_instance_outside_expected_raises():
    with pytest.raises(ValueError, match="not in the expected set"):
        FinalReport.from_reports(
            reports=[_r("a"), _r("ghost")],  # "ghost" not in expected
            invalid_reports=[],
            failed_tasks=[],
            expected_ids={"a"},
        )


def test_backward_compatible_without_expected_ids():
    # Old call shape (no expected_ids): still reconciles, denominator = sum.
    fr = FinalReport.from_reports(
        reports=[_r("a")],
        invalid_reports=[_r("b")],
        failed_tasks=[_r("c")],
    )
    assert fr.submitted_instances == 3
    assert fr.resolved_instances == 1
