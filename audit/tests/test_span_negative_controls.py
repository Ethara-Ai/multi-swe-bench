"""R2 span resolution: four fabricated-span sub-cases."""

from __future__ import annotations

from helpers import ack_finding, build_audit, run_verify, write_findings


def _verify_with_span(tmp_path, path, line):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    f = ack_finding(issues[0])
    f["path"] = path
    f["line"] = line
    fp = write_findings(audit_root, [f], disposition="HOLD")
    return run_verify(audit_root, project_root, fp)


def test_path_traversal_rejected(tmp_path):
    res = _verify_with_span(tmp_path, "../../etc/passwd", 1)
    assert not res.ok
    assert any("R2" in v for v in res.violations)


def test_escape_outside_root_rejected(tmp_path):
    res = _verify_with_span(tmp_path, "/etc/passwd", 1)
    assert not res.ok
    assert any("R2" in v for v in res.violations)


def test_nonexistent_file_rejected(tmp_path):
    res = _verify_with_span(tmp_path, "multi_swe_bench/harness/ghost.py", 1)
    assert not res.ok
    assert any("R2" in v for v in res.violations)


def test_line_out_of_range_rejected(tmp_path):
    # source file has 5 lines
    res = _verify_with_span(tmp_path, "multi_swe_bench/harness/report.py", 9999)
    assert not res.ok
    assert any("out of range" in v for v in res.violations)


def test_valid_span_ok(tmp_path):
    res = _verify_with_span(tmp_path, "multi_swe_bench/harness/report.py", 3)
    assert res.ok, res.violations
