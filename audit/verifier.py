"""The six deterministic verifier rules + disposition assembly.

R1 recall · R2 span resolution · R3 completed-run evidence · R4 CVSS form-vs-truth
· R6 vocabulary. Plus the Phase-3 provenance pre-checks (run BEFORE any rule).

`audit verify` exits 0 ONLY when every rule holds AND the producer's claimed
disposition is no more optimistic than the verifier-computed cap. The producer is
the adversary; nothing they assert is trusted without recomputation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

import cvss
from models import (
    CoverageGap,
    Disposition,
    Finding,
    LocationType,
    NormalizedIssue,
    RunStatus,
    Severity,
)
from provenance import verify_provenance
from recall import check_recall

_VALID_DISPOSITIONS = {d.value for d in Disposition}
_VALID_SEVERITIES = {s.value for s in Severity}


@dataclass
class VerifyResult:
    ok: bool
    computed_cap: Disposition
    producer_disposition: Disposition | None
    effective_severity: Severity
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def exit_code(self) -> int:
        return 0 if self.ok else 1


# --------------------------------------------------------------------------
# Loading producer findings + verifier evidence.
# --------------------------------------------------------------------------
def load_findings(path: Path) -> tuple[list[Finding], dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Finding] = []
    for item in raw.get("findings", []):
        # strip template/example keys defensively
        item = {k: v for k, v in item.items() if not k.endswith(("_template", "_example"))}
        out.append(
            Finding(
                finding_id=str(item.get("id", item.get("finding_id", "?"))),
                title=str(item.get("title", "")),
                severity=_coerce_sev(item.get("severity")),
                disposition=_coerce_disp(item.get("disposition")),
                issue_instance_ids=list(item.get("issue_instance_ids", []) or []),
                path=item.get("path"),
                line=item.get("line"),
                cwe=item.get("cwe"),
                cvss_vector=item.get("cvss_vector"),
                cvss_base=item.get("cvss_base"),
                waiver_reason_code=item.get("waiver_reason_code"),
                rationale=str(item.get("rationale", "")),
                raw=item,
            )
        )
    return out, raw


def _coerce_sev(v) -> Severity:
    try:
        return Severity(str(v).upper())
    except ValueError:
        return Severity.MEDIUM  # fail closed


def _coerce_disp(v) -> Disposition:
    try:
        return Disposition(str(v).upper())
    except ValueError:
        return Disposition.BLOCK  # fail closed


def load_evidence(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def evidence_to_issues(evidence: dict) -> list[NormalizedIssue]:
    out: list[NormalizedIssue] = []
    for ni in evidence.get("normalized_issues", []):
        out.append(
            NormalizedIssue(
                tool=ni.get("tool", ""),
                rule_id=ni.get("rule_id", ""),
                severity=_coerce_sev(ni.get("severity")),
                location_type=LocationType(ni.get("location_type", "source")),
                path=ni.get("path"),
                line=ni.get("line"),
                package=ni.get("package"),
                version=ni.get("version"),
                vuln_id=ni.get("vuln_id"),
                advisory_id=ni.get("advisory_id"),
                tool_native_fingerprint=ni.get("tool_native_fingerprint"),
                message=ni.get("message", ""),
                cluster_fingerprint=ni.get("cluster_fingerprint", ""),
                issue_instance_id=ni.get("issue_instance_id", ""),
                occurrence_ordinal=ni.get("occurrence_ordinal", 0),
            )
        )
    return out


# --------------------------------------------------------------------------
# Individual rules.
# --------------------------------------------------------------------------
def _r2_span(findings: list[Finding], source_snapshot: dict, project_root: Path) -> list[str]:
    """Every path:line resolves against the manifest snapshot; no escape."""
    v = []
    for f in findings:
        if f.path is None and f.line is None:
            continue
        if f.path is None:
            v.append(f"R2 {f.finding_id}: line without path")
            continue
        if ".." in Path(f.path).parts:
            v.append(f"R2 {f.finding_id}: path traversal in {f.path!r}")
            continue
        snap = source_snapshot.get(f.path)
        # resolve realpath inside the project root
        abs_p = (project_root / f.path).resolve()
        try:
            abs_p.relative_to(project_root.resolve())
        except ValueError:
            v.append(f"R2 {f.finding_id}: {f.path!r} escapes audit root")
            continue
        if snap is None:
            if not abs_p.is_file():
                v.append(f"R2 {f.finding_id}: {f.path!r} is not a regular file in scope")
                continue
            line_count = abs_p.read_text(encoding="utf-8", errors="replace").count("\n") + 1
        else:
            line_count = snap["line_count"]
            if Path(snap["realpath"]).resolve() != abs_p:
                v.append(f"R2 {f.finding_id}: {f.path!r} realpath drift vs manifest")
                continue
        if f.line is not None and not (1 <= int(f.line) <= max(line_count, 1)):
            v.append(
                f"R2 {f.finding_id}: line {f.line} out of range (1..{line_count}) for {f.path}"
            )
    return v


def _r3_runs(findings: list[Finding], evidence: dict) -> list[str]:
    """Only ok/nonzero_exit runs back a finding; blocked/timeout back only gaps."""
    v = []
    run_status = {r["run_id"]: r.get("status") for r in evidence.get("runs", [])}
    backable = {RunStatus.OK.value, RunStatus.NONZERO_EXIT.value}
    for f in findings:
        cited = f.raw.get("cited_runs", []) or []
        for rid in cited:
            st = run_status.get(rid)
            if st is None:
                v.append(f"R3 {f.finding_id}: cites unknown run {rid}")
            elif st not in backable:
                v.append(
                    f"R3 {f.finding_id}: cites a {st} run {rid} as evidence "
                    f"(only completed runs may back a finding)"
                )
    return v


def _r3_state_ship_allowed(evidence: dict) -> tuple[bool, str]:
    recon = evidence.get("recon", {})
    if not recon.get("git_sha"):
        return False, "R3-state: null git SHA — SHIP requires a real checkout"
    # Fail closed: SHIP requires the tree to be explicitly recorded clean
    # (git_dirty is False); a missing/None value is treated as dirty.
    if recon.get("git_dirty") is not False:
        return False, "R3-state: dirty working tree — SHIP requires a clean tree"
    pins = recon.get("tool_pins", [])
    required_present = [p for p in pins if p.get("present")]
    if any(p.get("db_version") is None for p in required_present):
        return False, "R3-state: a present DB-backed scanner is unpinned"
    return True, ""


def _r4_cvss(findings: list[Finding]) -> list[str]:
    v = []
    for f in findings:
        if f.cvss_vector:
            if not cvss.matches_claim(f.cvss_vector, f.cvss_base):
                try:
                    recomputed = cvss.base_score(f.cvss_vector)
                    v.append(
                        f"R4 {f.finding_id}: cvss_base {f.cvss_base} != recomputed {recomputed} "
                        f"for {f.cvss_vector}"
                    )
                except cvss.CvssError as e:
                    v.append(f"R4 {f.finding_id}: invalid CVSS vector ({e})")
        if f.cwe is not None and not cvss.cwe_is_well_formed(f.cwe):
            v.append(f"R4 {f.finding_id}: malformed CWE {f.cwe!r} (expected CWE-<n>)")
    return v


def _r6_vocab(findings: list[Finding], raw_findings: dict) -> list[str]:
    v = []
    for f in findings:
        if (
            f.raw.get("disposition")
            and str(f.raw["disposition"]).upper() not in _VALID_DISPOSITIONS
        ):
            v.append(f"R6 {f.finding_id}: invalid disposition {f.raw.get('disposition')!r}")
        if f.raw.get("severity") and str(f.raw["severity"]).upper() not in _VALID_SEVERITIES:
            v.append(f"R6 {f.finding_id}: invalid severity {f.raw.get('severity')!r}")
    # tally consistency if the producer published a summary
    summary = raw_findings.get("summary", {})
    if summary:
        claimed = summary.get("finding_count")
        if claimed is not None and int(claimed) != len(findings):
            v.append(f"R6: summary.finding_count {claimed} != actual {len(findings)}")
    return v


# --------------------------------------------------------------------------
# Top-level verify.
# --------------------------------------------------------------------------
def verify_findings(
    findings_path: Path,
    evidence_path: Path,
    *,
    audit_root: Path,
    project_root: Path,
    scope_digest: str,
    approved_digest: str,
    coverage_gaps: list[CoverageGap] | None = None,
    approved_waivers: set[str] | None = None,
) -> VerifyResult:
    violations: list[str] = []
    notes: list[str] = []

    findings, raw_findings = load_findings(findings_path)
    evidence = load_evidence(evidence_path)
    issues = evidence_to_issues(evidence)

    # ---- Phase 3 provenance pre-checks (BEFORE any rule) ----
    prov = verify_provenance(
        audit_root=audit_root,
        project_root=project_root,
        evidence_path=evidence_path,
        scope_digest=scope_digest,
        approved_digest=approved_digest,
    )
    cap = prov.cap
    notes.extend(f"provenance: {r}" for r in prov.reasons)

    # ---- R6 vocabulary ----
    violations += _r6_vocab(findings, raw_findings)

    # ---- R2 span resolution (against manifest snapshot — TOCTOU closed) ----
    violations += _r2_span(findings, prov.source_snapshot, project_root)

    # ---- R3 completed-run evidence ----
    violations += _r3_runs(findings, evidence)

    # ---- R4 CVSS ----
    violations += _r4_cvss(findings)

    # ---- coverage gaps cap the disposition ----
    gaps = list(coverage_gaps or [])
    for g in evidence.get("coverage_gaps", []):
        gaps.append(
            CoverageGap(
                g.get("gap_id", "?"),
                g.get("surface", "?"),
                g.get("reason", ""),
                _coerce_disp(g.get("disposition_cap")),
            )
        )
    for g in gaps:
        cap = Disposition.cap(cap, g.disposition_cap)
        notes.append(f"coverage-gap {g.gap_id}: caps {g.disposition_cap.value}")

    # ---- required-instrument cleanliness for empty-report rule ----
    required_ids = set(evidence.get("required_instruments", []))
    run_by_tool: dict[str, dict] = {}
    for r in evidence.get("runs", []):
        run_by_tool[r["tool"]] = r
    required_all_clean = True
    for rid in required_ids:
        r = run_by_tool.get(rid)
        if r is None or r.get("status") not in (RunStatus.OK.value, RunStatus.NONZERO_EXIT.value):
            required_all_clean = False
        elif r.get("parsed_ok") is False:
            required_all_clean = False

    # ---- R1 recall ----
    has_empty = len(findings) == 0
    recall = check_recall(
        findings,
        issues,
        required_all_clean=required_all_clean,
        has_empty_report=has_empty,
        approved_waivers=approved_waivers,
    )
    violations += recall.violations
    cap = Disposition.cap(cap, recall.cap)

    # ---- R3-state: SHIP gate ----
    ship_ok, ship_reason = _r3_state_ship_allowed(evidence)
    if not ship_ok:
        cap = Disposition.cap(cap, Disposition.HOLD)
        notes.append(ship_reason)

    # ---- disposition consistency: producer must not exceed the cap ----
    producer_disp = None
    declared = raw_findings.get("disposition") or (raw_findings.get("summary", {}) or {}).get(
        "disposition"
    )
    if declared:
        producer_disp = _coerce_disp(declared)
        if producer_disp.rank > cap.rank:
            violations.append(
                f"declared disposition {producer_disp.value} exceeds verifier cap {cap.value}"
            )

    ok = not violations
    return VerifyResult(
        ok=ok,
        computed_cap=cap,
        producer_disposition=producer_disp,
        effective_severity=recall.effective_severity,
        violations=violations,
        notes=notes,
    )
