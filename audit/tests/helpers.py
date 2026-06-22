"""Fixture builders for the negative-control suites.

Each builder constructs an internally-consistent (audit_root, project_root)
pair: a real source file, an evidence.yaml, and a provenance manifest whose
digests all reconcile. Individual tests then mutate ONE thing to prove the
corresponding bypass is caught.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from models import sha256_bytes, sha256_hex

DIGEST = "a" * 64  # a fixed, self-consistent scope/approved digest for tests

SOURCE_FILE = "multi_swe_bench/harness/report.py"
SOURCE_TEXT = "line1\nline2\nline3\nline4\nline5\n"


def _issue(iid_seed: str, severity: str, *, path=SOURCE_FILE, line=2, rule="r"):
    cluster = sha256_hex("c", iid_seed)
    return {
        "tool": "bandit",
        "rule_id": rule,
        "severity": severity,
        "location_type": "source",
        "path": path,
        "line": line,
        "package": None,
        "version": None,
        "vuln_id": None,
        "advisory_id": None,
        "tool_native_fingerprint": None,
        "message": f"issue {iid_seed}",
        "cluster_fingerprint": cluster,
        "issue_instance_id": sha256_hex(cluster, 0, "digest", "bandit", rule),
        "occurrence_ordinal": 0,
    }


def build_audit(
    tmp_path: Path,
    *,
    issues=None,
    coverage_gaps=None,
    required_clean=True,
    git_dirty=False,
    pin_dbs=True,
):
    project_root = tmp_path / "proj"
    audit_root = tmp_path / "proj" / "audit"
    (project_root / "multi_swe_bench" / "harness").mkdir(parents=True, exist_ok=True)
    audit_root.mkdir(parents=True, exist_ok=True)
    src = project_root / SOURCE_FILE
    src.write_text(SOURCE_TEXT, encoding="utf-8")

    issues = issues if issues is not None else [_issue("x1", "MEDIUM")]
    coverage_gaps = coverage_gaps or []

    runs = [
        {
            "run_id": "CMD-001",
            "tool": "bandit",
            "argv": ["bandit"],
            "cwd": str(project_root),
            "status": "nonzero_exit",
            "exit_code": 1,
            "duration_sec": 0.1,
            "stdout_artifact": None,
            "stdout_sha256": None,
            "stdout_excerpt": "",
            "parsed_ok": True,
        }
    ]
    pins = [
        {
            "name": "bandit",
            "present": True,
            "version": "1.0",
            "db_version": "1.0" if pin_dbs else None,
            "db_digest": None,
        }
    ]
    recon = {
        "git_sha": "deadbeef" if not git_dirty else "deadbeef",
        "git_dirty": git_dirty,
        "tool_pins": pins,
    }
    evidence = {
        "schema_version": 1,
        "recon": recon,
        "required_instruments": ["bandit"] if required_clean else ["bandit", "gitleaks"],
        "runs": runs,
        "normalized_issues": issues,
        "coverage_gaps": coverage_gaps,
        "not_run": [],
    }
    ev_path = audit_root / "evidence.yaml"
    ev_path.write_text(yaml.safe_dump(evidence, sort_keys=False), encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "git_sha": recon["git_sha"],
        "git_dirty": git_dirty,
        "policy_version": 1,
        "scope_digest": DIGEST,
        "approved_scope_digest": DIGEST,
        "evidence_sha256": sha256_bytes(ev_path.read_bytes()),
        "sources": [
            {
                "path": SOURCE_FILE,
                "realpath": str(src.resolve()),
                "sha256": sha256_bytes(src.read_bytes()),
                "line_count": SOURCE_TEXT.count("\n") + 1,
                "type": "source",
            }
        ],
        "runs": [
            {
                "run_id": "CMD-001",
                "tool": "bandit",
                "argv": ["bandit"],
                "cwd": str(project_root),
                "status": "nonzero_exit",
                "exit_code": 1,
                "stdout_artifact": None,
                "stdout_artifact_sha256": None,
            }
        ],
    }
    (audit_root / "provenance.manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=True), encoding="utf-8"
    )
    return audit_root, project_root, ev_path, issues


def write_findings(audit_root: Path, findings: list[dict], *, disposition=None, summary=None):
    doc = {"findings": findings}
    if disposition:
        doc["disposition"] = disposition
    if summary:
        doc["summary"] = summary
    p = audit_root / "findings.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


def ack_finding(issue: dict, fid="F1", **over):
    f = {
        "id": fid,
        "title": "ack",
        "severity": issue["severity"],
        "disposition": "HOLD",
        "issue_instance_ids": [issue["issue_instance_id"]],
        "path": issue["path"],
        "line": issue["line"],
        "rationale": "acknowledged with detail",
    }
    f.update(over)
    return f


def run_verify(audit_root, project_root, findings_path):
    from verifier import verify_findings

    return verify_findings(
        findings_path,
        audit_root / "evidence.yaml",
        audit_root=audit_root,
        project_root=project_root,
        scope_digest=DIGEST,
        approved_digest=DIGEST,
    )
