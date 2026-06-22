"""Provenance gate (§1.10): context edit, scope drift, missing manifest, self-attest."""

from __future__ import annotations

import yaml
from helpers import ack_finding, build_audit, run_verify, write_findings


def test_context_edited_after_run_blocks(tmp_path):
    audit_root, project_root, ev_path, issues = build_audit(tmp_path)
    fp = write_findings(audit_root, [ack_finding(issues[0])], disposition="HOLD")
    # tamper with evidence AFTER the manifest snapshot
    ev = yaml.safe_load(ev_path.read_text())
    ev["normalized_issues"] = []  # erase the issue post-hoc
    ev_path.write_text(yaml.safe_dump(ev, sort_keys=False))
    res = run_verify(audit_root, project_root, fp)
    assert res.computed_cap.value == "BLOCK"
    assert any("evidence.yaml edited" in n for n in res.notes)


def test_scope_drift_blocks(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    man_path = audit_root / "provenance.manifest.yaml"
    man = yaml.safe_load(man_path.read_text())
    man["scope_digest"] = "b" * 64  # drift from approved
    man_path.write_text(yaml.safe_dump(man, sort_keys=True))
    fp = write_findings(audit_root, [ack_finding(issues[0])], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert res.computed_cap.value == "BLOCK"


def test_missing_manifest_caps_hold(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    (audit_root / "provenance.manifest.yaml").unlink()
    fp = write_findings(audit_root, [ack_finding(issues[0])], disposition="SHIP")
    res = run_verify(audit_root, project_root, fp)
    assert res.computed_cap.value == "HOLD"
    assert not res.ok  # SHIP exceeds HOLD


def test_self_attested_caps_hold_not_ship(tmp_path):
    # clean tree, pinned dbs, but no AUDIT_TRUST_ROOT_KEY -> trusted False -> HOLD
    audit_root, project_root, _, issues = build_audit(tmp_path)
    fp = write_findings(audit_root, [ack_finding(issues[0])], disposition="SHIP")
    res = run_verify(audit_root, project_root, fp)
    assert res.computed_cap.value == "HOLD"
    assert not res.ok


def test_source_drift_caps_below_ship(tmp_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    # mutate the source after the snapshot
    src = project_root / "multi_swe_bench/harness/report.py"
    src.write_text("totally different\n", encoding="utf-8")
    fp = write_findings(audit_root, [ack_finding(issues[0], line=1)], disposition="SHIP")
    res = run_verify(audit_root, project_root, fp)
    assert res.computed_cap.value in ("HOLD", "BLOCK")


def test_argv_swap_in_evidence_blocks(tmp_path):
    # command/config integrity: a run's argv swapped after the manifest snapshot.
    audit_root, project_root, ev_path, issues = build_audit(tmp_path)
    ev = yaml.safe_load(ev_path.read_text())
    ev["runs"][0]["argv"] = ["bandit", "--config", "/tmp/evil"]
    ev_path.write_text(yaml.safe_dump(ev, sort_keys=False))
    # re-sync the evidence digest so context-integrity passes and we isolate argv.
    from models import sha256_bytes

    man_path = audit_root / "provenance.manifest.yaml"
    man = yaml.safe_load(man_path.read_text())
    man["evidence_sha256"] = sha256_bytes(ev_path.read_bytes())
    man_path.write_text(yaml.safe_dump(man, sort_keys=True))
    fp = write_findings(audit_root, [ack_finding(issues[0])], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert res.computed_cap.value == "BLOCK"
    assert any("argv/cwd/status drift" in n for n in res.notes)


def test_artifact_substitution_blocks(tmp_path):
    # command/config integrity: a stdout artifact swapped on disk vs its snapshot.
    audit_root, project_root, _, issues = build_audit(tmp_path)
    results = audit_root / "results"
    results.mkdir(exist_ok=True)
    (results / "CMD-001.stdout.txt.gz").write_bytes(b"substituted-bytes")
    man_path = audit_root / "provenance.manifest.yaml"
    man = yaml.safe_load(man_path.read_text())
    man["runs"][0]["stdout_artifact"] = "CMD-001.stdout.txt.gz"
    man["runs"][0]["stdout_artifact_sha256"] = "f" * 64  # not the on-disk digest
    man_path.write_text(yaml.safe_dump(man, sort_keys=True))
    fp = write_findings(audit_root, [ack_finding(issues[0])], disposition="HOLD")
    res = run_verify(audit_root, project_root, fp)
    assert res.computed_cap.value == "BLOCK"
    assert any("artifact substituted" in n for n in res.notes)
