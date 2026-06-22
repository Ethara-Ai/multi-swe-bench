"""R1 recall bypasses: omitted issue, empty all-SHIP, fabricated id, floor waiver."""

from __future__ import annotations

from helpers import ack_finding, build_audit, run_verify, write_findings


def test_omitted_issue_fails(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    # producer omits the MEDIUM issue entirely
    fp = write_findings(audit_root, [], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert any("unacknowledged" in v for v in res.violations)


def test_acknowledged_issue_passes(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    fp = write_findings(audit_root, [ack_finding(issues[0])], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert res.ok, res.violations


def test_empty_all_clear_with_unclean_required_fails(tmp_path):
    # required set includes gitleaks which never ran clean -> empty report invalid
    audit_root, project_root, _, _ = build_audit(tmp_path, issues=[], required_clean=False)
    fp = write_findings(audit_root, [], disposition="SHIP")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok


def test_fabricated_ack_id_fails(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    bogus = ack_finding(issues[0])
    bogus["issue_instance_ids"] = ["f" * 64]  # not verifier-emitted
    fp = write_findings(audit_root, [bogus], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert any("fabricated" in v or "unknown issue" in v for v in res.violations)


def test_critical_floor_cannot_be_waived_to_ship(tmp_path):
    from helpers import _issue

    crit = _issue("c1", "CRITICAL", rule="sql-injection")
    audit_root, project_root, _, issues = build_audit(tmp_path, issues=[crit])
    waived = ack_finding(crit, waiver_reason_code="not-exploitable")
    fp = write_findings(audit_root, [waived], disposition="SHIP")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert res.computed_cap.value == "BLOCK"
