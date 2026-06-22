"""R3 completed-run evidence + R3-state SHIP gate."""

from __future__ import annotations

import yaml
from helpers import ack_finding, build_audit, run_verify, write_findings


def _add_run(audit_root, run_id, status):
    ev_path = audit_root / "evidence.yaml"
    ev = yaml.safe_load(ev_path.read_text())
    ev["runs"].append(
        {
            "run_id": run_id,
            "tool": "semgrep",
            "status": status,
            "exit_code": None,
            "parsed_ok": False,
        }
    )
    ev_path.write_text(yaml.safe_dump(ev, sort_keys=False))
    # keep provenance evidence digest in sync so we isolate the R3 check
    from models import sha256_bytes

    man_path = audit_root / "provenance.manifest.yaml"
    man = yaml.safe_load(man_path.read_text())
    man["evidence_sha256"] = sha256_bytes(ev_path.read_bytes())
    man_path.write_text(yaml.safe_dump(man, sort_keys=True))


def test_blocked_run_cited_as_evidence_fails(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    _add_run(audit_root, "CMD-009", "tool_blocked")
    f = ack_finding(issues[0])
    f["cited_runs"] = ["CMD-009"]
    fp = write_findings(audit_root, [f], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert any("R3" in v and "tool_blocked" in v for v in res.violations)


def test_timeout_run_cited_as_evidence_fails(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    _add_run(audit_root, "CMD-010", "timeout")
    f = ack_finding(issues[0])
    f["cited_runs"] = ["CMD-010"]
    fp = write_findings(audit_root, [f], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert any("R3" in v and "timeout" in v for v in res.violations)


def test_dirty_tree_blocks_ship(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path, git_dirty=True)
    fp = write_findings(audit_root, [ack_finding(issues[0])], disposition="SHIP")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
    assert res.computed_cap.value in ("HOLD", "BLOCK")


def test_unpinned_db_blocks_ship(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path, pin_dbs=False)
    fp = write_findings(audit_root, [ack_finding(issues[0])], disposition="SHIP")
    res = run_verify(audit_root, project_root, fp)
    assert not res.ok
