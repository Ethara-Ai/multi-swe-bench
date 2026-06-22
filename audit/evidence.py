"""Emit the single committed evidence bundle (audit/evidence.yaml).

This is the ONLY evidence source a downstream reviewer/LLM may cite. It bundles
recon + per-tool run records + normalized issues + coverage gaps + the
required-instrument set, so the verifier reads exactly what the run produced.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from models import CoverageGap, NormalizedIssue, RunRecord


def _issue_dict(i: NormalizedIssue) -> dict:
    return {
        "tool": i.tool,
        "rule_id": i.rule_id,
        "severity": i.severity.value,
        "location_type": i.location_type.value,
        "path": i.path,
        "line": i.line,
        "package": i.package,
        "version": i.version,
        "vuln_id": i.vuln_id,
        "advisory_id": i.advisory_id,
        "tool_native_fingerprint": i.tool_native_fingerprint,
        "message": i.message,
        "cluster_fingerprint": i.cluster_fingerprint,
        "issue_instance_id": i.issue_instance_id,
        "occurrence_ordinal": i.occurrence_ordinal,
    }


def _run_dict(r: RunRecord) -> dict:
    return {
        "run_id": r.run_id,
        "tool": r.tool,
        "argv": r.argv,
        "cwd": r.cwd,
        "status": r.status.value,
        "exit_code": r.exit_code,
        "duration_sec": r.duration_sec,
        "stdout_artifact": r.stdout_artifact,
        "stdout_sha256": r.stdout_sha256,
        "stdout_excerpt": r.stdout_excerpt,
        "parsed_ok": r.parsed_ok,
    }


def build_evidence(
    *,
    recon: dict,
    runs: list[RunRecord],
    issues: list[NormalizedIssue],
    coverage_gaps: list[CoverageGap],
    required_instruments: list[str],
    not_run: list[dict],
    allowlist_excluded: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "recon": recon,
        "required_instruments": list(required_instruments),
        # Sanctioned exclusions applied to the issue set (approved ignore_allowlist).
        # Recorded, never silent: a downstream reviewer sees exactly what was dropped.
        "allowlist_excluded": allowlist_excluded or {},
        "runs": [_run_dict(r) for r in runs],
        "normalized_issues": [_issue_dict(i) for i in issues],
        "coverage_gaps": [
            {
                "gap_id": g.gap_id,
                "surface": g.surface,
                "reason": g.reason,
                "disposition_cap": g.disposition_cap.value,
            }
            for g in coverage_gaps
        ],
        "not_run": not_run,
    }


def write_evidence(evidence: dict, audit_root: Path) -> Path:
    path = audit_root / "evidence.yaml"
    path.write_text(yaml.safe_dump(evidence, sort_keys=False), encoding="utf-8")
    return path
