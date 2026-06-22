"""R6 vocabulary + tally: closed disposition/severity sets; summary must sum."""

from __future__ import annotations

from helpers import ack_finding, build_audit, run_verify, write_findings


def test_invalid_disposition_token_fails(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    f = ack_finding(issues[0])
    f["disposition"] = "MAYBE"  # not in {SHIP,HOLD,BLOCK}
    fp = write_findings(audit_root, [f])
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert any("R6" in v and "invalid disposition" in v for v in res.violations)


def test_tally_mismatch_fails(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    fp = write_findings(
        audit_root,
        [ack_finding(issues[0])],
        disposition="HOLD",
        summary={"finding_count": 99},  # != actual 1
    )
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert any("finding_count" in v for v in res.violations)
