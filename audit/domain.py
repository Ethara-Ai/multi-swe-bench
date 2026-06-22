"""Bespoke domain-integrity checks (Bucket D, §1.5).

No off-the-shelf scanner ships these — they are the defects that actually kill a
SWE-bench dataset-creation pipeline. All are TOTAL, deterministic relations over
the JSONL deliverable and the grader source. Each returns (issues, gaps):
a missing input is a CoverageGap (caps disposition), never a silent pass.

Checks:
  - reward_bucket_consistency_check  -- per-record {run,test,fix} triples must
        match their bucket; buckets disjoint; resolved verdict recomputable.
  - dataset_leakage_check            -- gold fix_patch (solution) must not be
        reachable from agent-visible prompt fields; cross-record dup.
  - report_claim_artifact_check      -- recompute dataset-level stats from raw.
  - dockerfile_generation_check      -- injection / unpinned base in the
        Dockerfile generator source.
  - reward_provenance_check          -- the fix_patch_tampers_with_tests guard
        is actually enforced before scoring.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from models import (
    CoverageGap,
    Disposition,
    LocationType,
    NormalizedIssue,
    Severity,
    sha256_hex,
)

_TO_PASS_BUCKETS = ("p2p_tests", "f2p_tests", "s2p_tests", "n2p_tests")

# The required-instrument ids these Bucket-D checks satisfy. The runner consults
# this to know which scoped instruments are covered by a domain check (vs. a
# scanner), so anything required but neither run nor covered here surfaces as a
# coverage gap instead of silently passing.
DOMAIN_CHECK_IDS = (
    "reward_provenance_check",
    "reward_bucket_consistency_check",
    "report_claim_artifact_check",
    "dataset_leakage_check",
    "dockerfile_generation_check",
)


def _issue(
    *,
    check: str,
    rule_id: str,
    severity: Severity,
    message: str,
    path: str | None,
    line: int | None = None,
    subject: str = "",
    ordinal: int = 0,
) -> NormalizedIssue:
    cluster = sha256_hex(check, rule_id, subject, path or "", message[:80])
    iid = sha256_hex(cluster, ordinal, "domain", check, rule_id)
    return NormalizedIssue(
        tool=check,
        rule_id=rule_id,
        severity=severity,
        location_type=LocationType.ARTIFACT,
        path=path,
        line=line,
        package=None,
        version=None,
        vuln_id=None,
        advisory_id=None,
        tool_native_fingerprint=None,
        message=message,
        cluster_fingerprint=cluster,
        issue_instance_id=iid,
        occurrence_ordinal=ordinal,
    )


def _load_jsonl(path: Path) -> tuple[list[dict] | None, str]:
    try:
        records = []
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    records.append(json.loads(ln))
        return records, ""
    except FileNotFoundError:
        return None, f"dataset not found: {path}"
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return None, f"dataset unparseable: {e}"


# --------------------------------------------------------------------------
# 1. reward bucket consistency
# --------------------------------------------------------------------------
def reward_bucket_consistency_check(
    dataset_path: Path | None,
) -> tuple[list[NormalizedIssue], list[CoverageGap]]:
    if dataset_path is None:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-reward-bucket-no-dataset",
                "environment_dataset",
                "no dataset path supplied to reward_bucket_consistency_check",
                Disposition.HOLD,
            )
        ]
    records, err = _load_jsonl(dataset_path)
    if records is None:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-reward-bucket-unreadable",
                "environment_dataset",
                err,
                Disposition.HOLD,
            )
        ]

    issues: list[NormalizedIssue] = []
    for rec in records:
        rid = (
            rec.get("instance_id")
            or rec.get("uuid")
            or f"{rec.get('org')}/{rec.get('repo')}#{rec.get('number')}"
        )
        seen_tests: dict[str, str] = {}
        for bucket in _TO_PASS_BUCKETS:
            tests = rec.get(bucket) or {}
            if not isinstance(tests, dict):
                continue
            for tname, triple in tests.items():
                fixv = (triple or {}).get("fix")
                # every "to-pass" bucket requires fix == PASS
                if fixv != "PASS":
                    issues.append(
                        _issue(
                            check="reward_bucket_consistency_check",
                            rule_id="bucket-fix-not-pass",
                            severity=Severity.CRITICAL,  # rollout-miscount floor
                            message=(
                                f"{rid}: test {tname!r} filed under {bucket} but fix={fixv!r} "
                                f"(a to-pass bucket requires fix==PASS) — reward miscount"
                            ),
                            path=str(dataset_path),
                            subject=f"{rid}:{bucket}:{tname}",
                        )
                    )
                # disjointness across buckets
                if tname in seen_tests and seen_tests[tname] != bucket:
                    issues.append(
                        _issue(
                            check="reward_bucket_consistency_check",
                            rule_id="bucket-overlap",
                            severity=Severity.CRITICAL,
                            message=(
                                f"{rid}: test {tname!r} appears in both {seen_tests[tname]} "
                                f"and {bucket} — contradictory reward classification"
                            ),
                            path=str(dataset_path),
                            subject=f"{rid}:{tname}",
                        )
                    )
                seen_tests[tname] = bucket

            # bucket-specific transition correctness
            for tname, triple in tests.items():
                tv = (triple or {}).get("test")
                if bucket == "f2p_tests" and tv not in ("FAIL", "ERROR"):
                    issues.append(
                        _issue(
                            check="reward_bucket_consistency_check",
                            rule_id="f2p-not-failing-pre",
                            severity=Severity.HIGH,
                            message=f"{rid}: f2p test {tname!r} has test-stage={tv!r}, expected FAIL/ERROR",
                            path=str(dataset_path),
                            subject=f"{rid}:f2p:{tname}",
                        )
                    )

        # fixed_tests must reconcile with the transition buckets (f2p∪s2p∪n2p)
        fixed = set((rec.get("fixed_tests") or {}).keys())
        transitions = set()
        for b in ("f2p_tests", "s2p_tests", "n2p_tests"):
            transitions |= set((rec.get(b) or {}).keys())
        if fixed != transitions:
            missing = transitions - fixed
            extra = fixed - transitions
            issues.append(
                _issue(
                    check="reward_bucket_consistency_check",
                    rule_id="fixed-tests-mismatch",
                    severity=Severity.HIGH,
                    message=(
                        f"{rid}: fixed_tests does not reconcile with f2p∪s2p∪n2p "
                        f"(missing={sorted(missing)[:3]}, extra={sorted(extra)[:3]})"
                    ),
                    path=str(dataset_path),
                    subject=f"{rid}:fixed",
                )
            )

        # resolved-verdict integrity: a p2p test regressing at fix means the
        # instance is NOT actually resolved, yet it shipped in the dataset.
        for tname, triple in (rec.get("p2p_tests") or {}).items():
            if (triple or {}).get("fix") not in ("PASS",):
                issues.append(
                    _issue(
                        check="reward_bucket_consistency_check",
                        rule_id="p2p-regression",
                        severity=Severity.CRITICAL,
                        message=(
                            f"{rid}: p2p test {tname!r} regressed at fix "
                            f"(fix={(triple or {}).get('fix')!r}) — record should be unresolved"
                        ),
                        path=str(dataset_path),
                        subject=f"{rid}:p2p:{tname}",
                    )
                )
    return issues, []


# --------------------------------------------------------------------------
# 2. dataset leakage (gold solution reachable from the prompt)
# --------------------------------------------------------------------------
_CODE_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|[^\sA-Za-z0-9]")


def _shingles(text: str, k: int) -> set[str]:
    toks = _CODE_TOK.findall(text or "")
    if len(toks) < k:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i : i + k]) for i in range(len(toks) - k + 1)}


def _containment(needle: set[str], haystack: set[str]) -> float:
    if not needle:
        return 0.0
    return len(needle & haystack) / len(needle)


def _patch_added_lines(patch: str) -> str:
    """Only the added (+) source lines of a diff — the actual solution content."""
    out = []
    for ln in (patch or "").splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append(ln[1:])
    return "\n".join(out)


def dataset_leakage_check(
    dataset_path: Path | None,
) -> tuple[list[NormalizedIssue], list[CoverageGap]]:
    if dataset_path is None:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-leakage-no-dataset",
                "environment_dataset",
                "no dataset path supplied to dataset_leakage_check",
                Disposition.HOLD,
            )
        ]
    records, err = _load_jsonl(dataset_path)
    if records is None:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-leakage-unreadable",
                "environment_dataset",
                err,
                Disposition.HOLD,
            )
        ]

    issues: list[NormalizedIssue] = []
    seen_solutions: dict[str, str] = {}
    for rec in records:
        rid = (
            rec.get("instance_id")
            or rec.get("uuid")
            or f"{rec.get('org')}/{rec.get('repo')}#{rec.get('number')}"
        )
        visible = " \n ".join(
            str(rec.get(f, "") or "") for f in ("title", "body", "resolved_issues")
        )
        solution = _patch_added_lines(rec.get("fix_patch", ""))

        sol_sh = _shingles(solution, 9)
        vis_sh = _shingles(visible, 9)
        cont = _containment(sol_sh, vis_sh)
        if cont >= 0.80:
            issues.append(
                _issue(
                    check="dataset_leakage_check",
                    rule_id="solution-in-prompt",
                    severity=Severity.CRITICAL,  # dataset-leakage floor
                    message=(
                        f"{rid}: gold fix_patch is {cont:.0%} contained in agent-visible "
                        f"prompt fields (title/body/resolved_issues) — solution leakage"
                    ),
                    path=str(dataset_path),
                    subject=f"{rid}:leak",
                )
            )

        # exact cross-record duplicate of the solution (dup/contamination)
        sol_digest = sha256_hex("fix_patch", rec.get("fix_patch", ""))
        if rec.get("fix_patch") and sol_digest in seen_solutions:
            issues.append(
                _issue(
                    check="dataset_leakage_check",
                    rule_id="duplicate-instance",
                    severity=Severity.HIGH,
                    message=(
                        f"{rid}: identical fix_patch already present in record "
                        f"{seen_solutions[sol_digest]} — duplicated instance"
                    ),
                    path=str(dataset_path),
                    subject=f"{rid}:dup",
                )
            )
        else:
            seen_solutions[sol_digest] = rid
    return issues, []


# --------------------------------------------------------------------------
# 3. report claim reconciliation
# --------------------------------------------------------------------------
def report_claim_artifact_check(
    dataset_path: Path | None,
) -> tuple[list[NormalizedIssue], list[CoverageGap]]:
    """Recompute dataset-level counts from raw records.

    With no separate published summary file in scope, this asserts internal
    consistency: record count > 0, every record carries a join id, and the
    per-record resolved verdict (>=1 to-pass test, all fix==PASS) is coherent.
    A published REPORT.md/CSV, when present, would be diffed here.
    """
    if dataset_path is None:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-report-no-dataset",
                "client_deliverables",
                "no dataset path supplied to report_claim_artifact_check",
                Disposition.HOLD,
            )
        ]
    records, err = _load_jsonl(dataset_path)
    if records is None:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-report-unreadable",
                "client_deliverables",
                err,
                Disposition.HOLD,
            )
        ]

    issues: list[NormalizedIssue] = []
    if not records:
        issues.append(
            _issue(
                check="report_claim_artifact_check",
                rule_id="empty-dataset",
                severity=Severity.HIGH,
                message="dataset has zero records — a published nonzero count would not reconcile",
                path=str(dataset_path),
            )
        )
    for i, rec in enumerate(records):
        if not (rec.get("instance_id") or rec.get("uuid")):
            issues.append(
                _issue(
                    check="report_claim_artifact_check",
                    rule_id="missing-join-id",
                    severity=Severity.MEDIUM,
                    message=f"record #{i} lacks instance_id/uuid join key — untraceable claim",
                    path=str(dataset_path),
                    subject=f"rec{i}",
                    ordinal=i,
                )
            )
        to_pass = sum(len(rec.get(b) or {}) for b in _TO_PASS_BUCKETS)
        if to_pass == 0:
            issues.append(
                _issue(
                    check="report_claim_artifact_check",
                    rule_id="resolved-without-transition",
                    severity=Severity.HIGH,
                    message=(
                        f"record #{i} ({rec.get('instance_id')}) is in the resolved dataset but "
                        f"has 0 to-pass tests — verdict does not reconcile"
                    ),
                    path=str(dataset_path),
                    subject=f"rec{i}:notrans",
                    ordinal=i,
                )
            )
    return issues, []


# --------------------------------------------------------------------------
# 4. Dockerfile-generation injection / unpinned base
# --------------------------------------------------------------------------
# A RUN/ARG/ENV line that interpolates a Python `{value}` placeholder. A `${VAR}`
# is a DOCKER build-arg (resolved by docker from --build-arg, not Python string
# interpolation of PR metadata), so the negative lookbehind excludes it — only
# real source-side interpolation is flagged.
_INTERP_RUN_RE = re.compile(r"""(RUN|ARG|ENV)\b[^\n]*(?<![${])\{[^}{]*\}""")


def dockerfile_generation_check(
    project_root: Path,
) -> tuple[list[NormalizedIssue], list[CoverageGap]]:
    candidates = [
        project_root / "multi_swe_bench" / "utils" / "env_to_dockerfile.py",
        project_root / "multi_swe_bench" / "harness" / "image.py",
    ]
    present = [c for c in candidates if c.is_file()]
    if not present:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-dockerfile-generator-absent",
                "docker_containers",
                "no Dockerfile generator source found to inspect",
                Disposition.HOLD,
            )
        ]
    issues: list[NormalizedIssue] = []
    for src in present:
        try:
            text = src.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(src.relative_to(project_root))
        # Only a FROM that STARTS a (optionally f-prefixed) string literal is a
        # generated Dockerfile directive; "FROM" appearing mid-prose (e.g. inside
        # an error message) is not, and must not be flagged.
        for m in re.finditer(r"""(?:f?["'])FROM[ \t]+([A-Za-z0-9_./:@{}-]+)""", text):
            base = m.group(1)
            # A parameterized base (f-string placeholder) is validated by the
            # generator; only a literal base with neither tag nor digest is unpinned.
            if "{" in base:
                continue
            if ":" not in base and "@sha256" not in base:
                line = text[: m.start()].count("\n") + 1
                issues.append(
                    _issue(
                        check="dockerfile_generation_check",
                        rule_id="unpinned-base-image",
                        severity=Severity.MEDIUM,
                        message=f"{rel}: generated FROM uses a floating/unpinned base image: {base!r}",
                        path=rel,
                        line=line,
                        subject=f"{rel}:from:{line}",
                    )
                )
        for m in _INTERP_RUN_RE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            issues.append(
                _issue(
                    check="dockerfile_generation_check",
                    rule_id="interpolated-run-line",
                    severity=Severity.MEDIUM,
                    message=(
                        f"{rel}:{line}: RUN/ARG/ENV line interpolates a value — verify the source "
                        f"is trusted (org/repo/ref from PR metadata can inject shell)"
                    ),
                    path=rel,
                    line=line,
                    subject=f"{rel}:interp:{line}",
                )
            )
    return issues, []


# --------------------------------------------------------------------------
# 5. reward provenance: the tamper guard must be enforced before scoring
# --------------------------------------------------------------------------
def reward_provenance_check(
    project_root: Path,
) -> tuple[list[NormalizedIssue], list[CoverageGap]]:
    guard_src = project_root / "multi_swe_bench" / "harness" / "test_result.py"
    runner_src = project_root / "multi_swe_bench" / "harness" / "run_evaluation.py"
    if not guard_src.is_file() or not runner_src.is_file():
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-reward-provenance-source-absent",
                "rubrics_grading",
                "grader source (test_result.py / run_evaluation.py) not found",
                Disposition.HOLD,
            )
        ]
    issues: list[NormalizedIssue] = []
    guard_text = guard_src.read_text(encoding="utf-8")
    runner_text = runner_src.read_text(encoding="utf-8")

    has_guard = "fix_patch_tampers_with_tests" in guard_text
    enforced = "fix_patch_tampers_with_tests" in runner_text
    if not has_guard:
        issues.append(
            _issue(
                check="reward_provenance_check",
                rule_id="tamper-guard-missing",
                severity=Severity.CRITICAL,  # agent-writable-reward floor
                message="fix_patch_tampers_with_tests guard is not defined — agent can edit gold tests",
                path=str(guard_src.relative_to(project_root)),
                subject="guard-def",
            )
        )
    if has_guard and not enforced:
        issues.append(
            _issue(
                check="reward_provenance_check",
                rule_id="tamper-guard-not-enforced",
                severity=Severity.CRITICAL,
                message=(
                    "run_evaluation.py never calls fix_patch_tampers_with_tests — the reward-hack "
                    "guard is defined but not enforced before scoring"
                ),
                path=str(runner_src.relative_to(project_root)),
                subject="guard-enforce",
            )
        )
    return issues, []


def run_all_domain_checks(
    project_root: Path, dataset_path: Path | None
) -> tuple[list[NormalizedIssue], list[CoverageGap]]:
    all_issues: list[NormalizedIssue] = []
    all_gaps: list[CoverageGap] = []
    # Explicit calls (not a heterogeneous dispatch loop) so each check is invoked
    # with its correctly-typed argument and the whole suite stays type-checkable.
    for issues, gaps in (
        reward_bucket_consistency_check(dataset_path),
        dataset_leakage_check(dataset_path),
        report_claim_artifact_check(dataset_path),
        dockerfile_generation_check(project_root),
        reward_provenance_check(project_root),
    ):
        all_issues.extend(issues)
        all_gaps.extend(gaps)
    return all_issues, all_gaps
