"""R4 CVSS form-vs-truth + CWE well-formedness."""

from __future__ import annotations

from helpers import ack_finding, build_audit, run_verify, write_findings

import cvss


def test_known_vector_recomputes():
    # CVSS:3.1 AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H  ->  9.8
    assert cvss.base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    # scope-changed example -> 10.0
    assert cvss.base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H") == 10.0


def test_wrong_cvss_base_fails(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    f = ack_finding(issues[0])
    f["cvss_vector"] = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    f["cvss_base"] = 5.0  # lie; truth is 9.8
    fp = write_findings(audit_root, [f], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert any("R4" in v and "9.8" in v for v in res.violations)


def test_correct_cvss_base_ok(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    f = ack_finding(issues[0])
    f["cvss_vector"] = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    f["cvss_base"] = 9.8
    f["cwe"] = "CWE-89"
    fp = write_findings(audit_root, [f], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert res.ok, res.violations


def test_malformed_cwe_fails(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    f = ack_finding(issues[0])
    f["cwe"] = "89"  # missing CWE- prefix
    fp = write_findings(audit_root, [f], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert any("CWE" in v for v in res.violations)
