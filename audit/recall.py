"""R1 — CRITICAL-recall gate.

Every >= MEDIUM issue from a parsed run must be acknowledged by issue_instance_id
in some finding. Acknowledgement ids are VERIFIER-owned: the producer may only
reference ids the verifier emitted into evidence. Effective severity is the max
over acknowledged issues — never the producer's self-reported label. An empty,
all-clear report passes only if every required instrument ran clean and parsed.

Waiver discipline: a waived finding needs a reason-code from the closed enum and
a fingerprint-bound rationale; HIGH/CRITICAL/security waivers additionally need an
out-of-band approval. A CRITICAL-floor issue can never be waived to SHIP.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from models import Disposition, Finding, NormalizedIssue, Severity
from policy import rule_hits_critical_floor

_WAIVER_CODES = {
    "false-positive",
    "not-exploitable",
    "accepted-risk",
    "duplicate",
    "out-of-scope",
    "compensating-control",
}
_HEAVY_CODES = {"accepted-risk", "compensating-control"}


@dataclass
class RecallResult:
    ok: bool
    cap: Disposition
    effective_severity: Severity
    unacknowledged: list[NormalizedIssue] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def _valid_waiver(
    f: Finding, issue: NormalizedIssue, approved_waivers: set[str]
) -> tuple[bool, str]:
    code = f.waiver_reason_code
    if code not in _WAIVER_CODES:
        return False, f"finding {f.finding_id}: invalid/absent waiver reason-code {code!r}"
    if not f.rationale or len(f.rationale.strip()) < 12:
        return False, f"finding {f.finding_id}: waiver rationale too thin"
    # HIGH/CRITICAL/security waivers need out-of-band approval (a token bound to
    # the issue instance id, recorded outside findings.yaml).
    if issue.severity.rank >= Severity.HIGH.rank or issue.location_type.value == "secret":
        if issue.issue_instance_id not in approved_waivers:
            return False, (
                f"finding {f.finding_id}: {issue.severity.value} issue "
                f"{issue.issue_instance_id[:12]} waived without out-of-band approval"
            )
    return True, ""


def check_recall(
    findings: list[Finding],
    issues: list[NormalizedIssue],
    *,
    required_all_clean: bool,
    has_empty_report: bool,
    approved_waivers: set[str] | None = None,
) -> RecallResult:
    approved_waivers = approved_waivers or set()
    violations: list[str] = []

    # Build acknowledgement index: instance id -> finding that cites it.
    ack: dict[str, Finding] = {}
    valid_ids = {i.issue_instance_id for i in issues}
    for f in findings:
        for iid in f.issue_instance_ids:
            if iid not in valid_ids:
                violations.append(
                    f"finding {f.finding_id} cites unknown issue id {iid[:12]} "
                    f"(not verifier-emitted) — fabricated acknowledgement"
                )
                continue
            ack[iid] = f

    # Empty report only valid if every required instrument ran clean+parsed.
    if has_empty_report and not required_all_clean:
        violations.append(
            "empty/all-clear report but a required instrument did not run clean+parse — "
            "absence is not safety (capped)"
        )

    unack: list[NormalizedIssue] = []
    acknowledged_sevs: list[Severity] = []
    cap = Disposition.SHIP

    for issue in issues:
        if issue.severity.rank < Severity.MEDIUM.rank:
            continue  # below the recall floor
        f = ack.get(issue.issue_instance_id)
        if f is None:
            unack.append(issue)
            violations.append(
                f"unacknowledged {issue.severity.value} issue {issue.issue_instance_id[:12]} "
                f"from {issue.tool}:{issue.rule_id}"
            )
            # an unacknowledged required-instrument issue caps disposition
            cap = Disposition.cap(cap, Disposition.HOLD)
            if issue.severity.rank >= Severity.CRITICAL.rank or rule_hits_critical_floor(
                issue.rule_id, issue.message
            ):
                cap = Disposition.cap(cap, Disposition.BLOCK)
            continue

        acknowledged_sevs.append(issue.severity)
        # waiver path (finding marked with a waiver reason-code)
        if f.waiver_reason_code:
            on_floor = issue.severity.rank >= Severity.CRITICAL.rank or rule_hits_critical_floor(
                issue.rule_id, issue.message
            )
            if on_floor:
                violations.append(
                    f"finding {f.finding_id}: CRITICAL-floor issue "
                    f"{issue.issue_instance_id[:12]} cannot be waived to SHIP"
                )
                cap = Disposition.cap(cap, Disposition.BLOCK)
            ok_w, why = _valid_waiver(f, issue, approved_waivers)
            if not ok_w:
                violations.append(why)
                cap = Disposition.cap(cap, Disposition.HOLD)

    effective = Severity.max(acknowledged_sevs + [i.severity for i in unack])
    return RecallResult(
        ok=not violations,
        cap=cap,
        effective_severity=effective,
        unacknowledged=unack,
        violations=violations,
    )
