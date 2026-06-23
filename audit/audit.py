"""CRUCIBLE audit orchestrator — one Typer app, no bash wrapper.

Commands:
  provision  uv sync --extra scanners (+ note native scanners). Hard-fail.
  run        recon + scanners + domain checks + normalization -> evidence.yaml
             + provenance manifest + per-command stdout artifacts. NOT a gate.
  verify     the six rules against findings.yaml; exits 0 ONLY when all hold.
             This is the ONLY command whose success means the gate passed.
  all        provision (unless --no-install) -> run -> optional --verify; prints
             the Phase-2 hand-off. Findings are UNGATED until `audit verify` exits 0.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import typer

import dockerfiles as dockerfiles_mod
import domain
import evidence as evidence_mod
import imagescan as imagescan_mod
import normalize
import provenance as prov_mod
import recon as recon_mod
from models import CoverageGap, Disposition, ToolReport
from scopelib import ScopeNotApproved, audit_dir, load_approved_scope
from tools import build_registry, run_tool
from verifier import verify_findings

app = typer.Typer(add_completion=False, help="CRUCIBLE adversarial audit gate")

AUDIT_ROOT = audit_dir()
PROJECT_ROOT = AUDIT_ROOT.parent
RESULTS_DIR = AUDIT_ROOT / "results"


def _clock() -> float:
    return time.time()


def _scope_or_die():
    try:
        return load_approved_scope(AUDIT_ROOT)
    except ScopeNotApproved as e:
        typer.secho(f"SCOPE NOT APPROVED:\n{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e


def _dataset_path(scope) -> Path | None:
    dd = scope.raw.get("deliverable_dataset", {})
    p = dd.get("example_path")
    if p and Path(p).is_file():
        return Path(p)
    return None


def _source_paths_for_manifest(scope) -> list[Path]:
    """The citable source closure: changed files + grader sources under audit focus."""
    paths: set[Path] = set()
    focus = scope.raw.get("audit_focus", {})
    for rel in focus.get("changed_tracked", []):
        paths.add(PROJECT_ROOT / rel)
    # always include grader sources the domain checks read
    for rel in (
        "multi_swe_bench/harness/report.py",
        "multi_swe_bench/harness/test_result.py",
        "multi_swe_bench/harness/run_evaluation.py",
        "multi_swe_bench/utils/env_to_dockerfile.py",
        "multi_swe_bench/harness/image.py",
    ):
        paths.add(PROJECT_ROOT / rel)
    return sorted(p for p in paths if p.is_file())


def _apply_ignore_allowlist(scope, issues: list) -> tuple[list, dict]:
    """Drop SAST/hygiene issues under sanctioned-excluded paths; keep secrets+deps.

    Total + fail-closed: only the exact paths in the APPROVED ignore_allowlist are
    excluded, and only for source/config location types. Returns (kept, summary)
    where summary records the dropped count per allowlist path (recorded, never
    silent). Secret and dependency findings are never dropped.
    """
    raw_allow = scope.command_config_policy.get("ignore_allowlist", []) or []
    prefixes = [
        str(e.get("path", "")).replace("\\", "/").rstrip("*").rstrip("/")
        for e in raw_allow
        if e.get("path")
    ]
    summary: dict = {p: 0 for p in prefixes}
    kept: list = []
    for i in issues:
        if i.location_type.value in ("secret", "dependency"):
            kept.append(i)
            continue
        p = (i.path or "").replace("\\", "/")
        hit = next((pre for pre in prefixes if pre and pre in p), None)
        if hit is not None:
            summary[hit] += 1
        else:
            kept.append(i)
    summary = {k: v for k, v in summary.items() if v}
    return kept, summary


@app.command()
def provision(no_install: bool = typer.Option(False, "--no-install")):
    """Install the scanner tier. Hard-fail on error."""
    _scope_or_die()
    if no_install:
        typer.echo("provision: --no-install set, skipping")
        return
    cmd = ["uv", "sync", "--extra", "scanners"]
    typer.echo(f"provision: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, cwd=str(AUDIT_ROOT))
    except OSError as e:
        typer.secho(f"provision failed to launch uv: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e
    if r.returncode != 0:
        typer.secho("provision failed", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(
        "provision: native scanners (osv-scanner, gitleaks, hadolint, trivy) "
        "must be installed out-of-band."
    )


@app.command()
def run(
    budget: int = typer.Option(
        0, "-t", "--timeout", help="global wall-clock budget (sec); 0 = unbounded"
    ),
):
    """Run instruments + domain checks; emit evidence.yaml + provenance manifest."""
    scope = _scope_or_die()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "artifacts").mkdir(parents=True, exist_ok=True)
    run_started = _clock()

    surfaces = scope.raw.get("surfaces", {})
    product_types = scope.raw.get("product_types", [])
    recon = recon_mod.collect_recon(
        PROJECT_ROOT, surfaces, product_types, timestamp_epoch=_clock()
    ).to_dict()

    # source manifest first (for R2 TOCTOU + provenance)
    source_paths = _source_paths_for_manifest(scope)
    source_entries = prov_mod.build_source_manifest(PROJECT_ROOT, source_paths)
    src_digest = recon.get("git_sha") or "nogit"

    # run scanner instruments
    registry = build_registry(PROJECT_ROOT)
    required_ids = set(scope.required_instrument_ids)
    reports: list[ToolReport] = []
    not_run: list[dict] = []
    ordinal = 1
    for tool in registry:
        # Global wall-clock budget: once exhausted, remaining tools are recorded
        # as not-run (fail closed -> coverage gap), never silently skipped.
        if budget and (_clock() - run_started) > budget:
            not_run.append({"tool": tool.name, "status": "budget_exhausted"})
            continue
        rec = run_tool(tool, PROJECT_ROOT, RESULTS_DIR / "artifacts", ordinal, _clock)
        ordinal += 1
        report = ToolReport(tool=tool.name, run=rec)
        normalize.normalize_report(
            report,
            from_required=tool.critical_capable,
            source_manifest_digest=src_digest,
            results_dir=RESULTS_DIR,
        )
        reports.append(report)
        if rec.status.value in ("tool_blocked", "timeout"):
            not_run.append({"tool": tool.name, "status": rec.status.value})

    all_issues = [i for r in reports for i in r.issues]
    all_runs = [r.run for r in reports]

    # coverage gaps: declared in scope + a gap per absent required critical tool
    gaps: list[CoverageGap] = []
    for g in scope.coverage_gaps:
        gaps.append(
            CoverageGap(
                g.get("id", "?"),
                g.get("surface", "?"),
                g.get("reason", ""),
                Disposition(g.get("disposition_cap", "HOLD")),
            )
        )
    present_tools = {r.tool for r in reports if r.run.status.value in ("ok", "nonzero_exit")}
    for tool in registry:
        if tool.name in required_ids and tool.critical_capable and tool.name not in present_tools:
            gaps.append(
                CoverageGap(
                    f"D-COVERAGE-GAP-absent-{tool.name}",
                    tool.category,
                    f"required critical-capable instrument {tool.name} did not run",
                    tool.disposition_cap_on_absent,
                )
            )
    # A required critical tool that RAN but whose artifact could not be parsed is
    # not coverage — parsing fails closed (CRUCIBLE §1.4). Emit a gap so the
    # unparsable run caps the disposition instead of contributing zero issues.
    for r in reports:
        tool = next((t for t in registry if t.name == r.tool), None)
        if (
            tool is not None
            and tool.name in required_ids
            and tool.critical_capable
            and r.run.status.value in ("ok", "nonzero_exit")
            and r.run.parsed_ok is False
        ):
            gaps.append(
                CoverageGap(
                    f"D-COVERAGE-GAP-unparsed-{tool.name}",
                    tool.category,
                    f"required instrument {tool.name} ran ({r.run.status.value}) but its "
                    f"artifact did not parse — fail closed",
                    tool.disposition_cap_on_absent,
                )
            )

    # domain-integrity checks
    dpath = _dataset_path(scope)
    dom_issues, dom_gaps = domain.run_all_domain_checks(PROJECT_ROOT, dpath)
    all_issues.extend(dom_issues)
    gaps.extend(dom_gaps)

    # ② Generated-Dockerfile sampling: render + hadolint a per-language sample of
    # real instance Dockerfiles (with RUN layers) via the project interpreter. The
    # SINGLE source of truth for the container-static surface — emits findings plus
    # a residual gap on success, or a fail-closed gap when it cannot render/lint.
    df_issues, df_gaps = dockerfiles_mod.dockerfile_sample_check(PROJECT_ROOT)
    all_issues.extend(df_issues)
    gaps.extend(df_gaps)

    # Live container image scan: docker-build the representative Dockerfile + trivy
    # image-scan the built image (real OS/package CVEs). Narrows the static residual;
    # fail-closed to a coverage gap if docker/trivy/build/scan is unavailable.
    img_issues, img_gaps = imagescan_mod.live_image_scan(PROJECT_ROOT)
    all_issues.extend(img_issues)
    gaps.extend(img_gaps)

    # Fail-closed against required-set STARVATION: any instrument named in the
    # approved scope that the runner neither executes (registry) nor evaluates as
    # a domain check is silently uncovered. Emit a coverage gap so the absence
    # caps the disposition instead of reading as a clean pass.
    accounted = present_tools | {t.name for t in registry} | set(domain.DOMAIN_CHECK_IDS)
    for rid in sorted(required_ids - accounted):
        gaps.append(
            CoverageGap(
                f"D-COVERAGE-GAP-not-instrumented-{rid}",
                "required_instrument",
                f"required instrument {rid!r} is declared in the approved scope but "
                f"is not wired into the runner — uncovered surface, fail closed",
                Disposition.HOLD,
            )
        )

    # Enforce the APPROVED ignore_allowlist (part of the signed command/config
    # policy). It sanctions dropping SAST/hygiene noise on the generated registry
    # and the third-party .repo_cache mirrors — but secrets and dependency
    # findings are KEPT (the allowlist explicitly preserves gitleaks coverage).
    # Argv is unchanged, so the approved-argv digest still holds; the exclusion is
    # applied here and RECORDED in evidence (never silent).
    all_issues, allowlist_excluded = _apply_ignore_allowlist(scope, all_issues)

    # emit evidence
    ev = evidence_mod.build_evidence(
        recon=recon,
        runs=all_runs,
        issues=all_issues,
        coverage_gaps=gaps,
        required_instruments=sorted(required_ids),
        not_run=not_run,
        allowlist_excluded=allowlist_excluded,
    )
    ev_path = evidence_mod.write_evidence(ev, AUDIT_ROOT)

    # provenance manifest (after evidence so it can hash it)
    manifest = prov_mod.build_manifest(
        project_root=PROJECT_ROOT,
        audit_root=AUDIT_ROOT,
        evidence_path=ev_path,
        run_records=all_runs,
        source_entries=source_entries,
        recon=recon,
        policy_version=scope.policy_version,
        scope_digest=scope.scope_digest,
        approved_digest=scope.approved_digest,
        results_dir=RESULTS_DIR,
    )
    prov_mod.write_manifest(manifest, AUDIT_ROOT)

    typer.echo(f"run: wrote {ev_path}")
    typer.echo(
        f"run: {len(all_issues)} normalized issues, {len(gaps)} coverage gaps, "
        f"{len(all_runs)} tool runs"
    )
    typer.secho(
        "run produced EVIDENCE only — findings are UNGATED until `audit verify` exits 0.",
        fg=typer.colors.YELLOW,
    )


@app.command()
def verify(
    findings: Path = typer.Option(..., "--findings"),
    context: Path = typer.Option(..., "--context"),
):
    """The gate. Exits 0 only when all six rules hold."""
    scope = _scope_or_die()
    if not findings.is_file():
        typer.secho(f"findings not found: {findings}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    result = verify_findings(
        findings,
        context,
        audit_root=AUDIT_ROOT,
        project_root=PROJECT_ROOT,
        scope_digest=scope.scope_digest,
        approved_digest=scope.approved_digest,
    )
    for n in result.notes:
        typer.echo(f"  note: {n}")
    for v in result.violations:
        typer.secho(f"  VIOLATION: {v}", fg=typer.colors.RED)
    typer.echo(
        f"computed cap = {result.computed_cap.value}  "
        f"effective severity = {result.effective_severity.value}"
    )
    if result.ok:
        typer.secho("GATE PASS — all rules hold.", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"GATE FAIL — {len(result.violations)} violation(s).", fg=typer.colors.RED, err=True
        )
    raise typer.Exit(code=result.exit_code())


@app.command()
def sign():
    """Detached-sign the provenance manifest with the EXTERNAL trust-root key.

    Run this ONLY in a trusted environment (CI / KMS / approver) that holds
    AUDIT_TRUST_ROOT_KEY — a secret the producer cannot mint. It HMACs the exact
    bytes `verify` reads (audit/provenance.manifest.yaml) and writes the detached
    signature to audit/provenance.manifest.sig. Possession of the external key is
    the gate: signing on a producer-controlled host where the key is readable is
    meaningless (and the run would still be self-attested elsewhere).
    """
    key = os.environ.get("AUDIT_TRUST_ROOT_KEY")
    if not key:
        typer.secho(
            "AUDIT_TRUST_ROOT_KEY not set — cannot sign (the trust root must come "
            "from outside the repo, e.g. a CI/KMS secret).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    manifest_path = AUDIT_ROOT / "provenance.manifest.yaml"
    if not manifest_path.is_file():
        typer.secho(
            f"no manifest to sign at {manifest_path} — run `audit run` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    sig = prov_mod.hmac_sign(manifest_path.read_bytes(), key)
    sig_path = AUDIT_ROOT / "provenance.manifest.sig"
    sig_path.write_text(sig, encoding="utf-8")
    typer.secho(
        f"signed: wrote {sig_path} (verify with the same key in env). "
        "SHIP also needs a clean tree, no source drift, and no capping coverage gaps.",
        fg=typer.colors.GREEN,
    )


@app.command()
def all(
    no_install: bool = typer.Option(False, "--no-install"),
    do_verify: bool = typer.Option(False, "--verify"),
    findings: Path = typer.Option(Path("../findings.yaml"), "--findings"),
    timeout: int = typer.Option(900, "-t", "--timeout"),
):
    """provision -> run -> optional verify. Never writes findings."""
    if not no_install:
        try:
            provision(no_install=False)
        except typer.Exit as e:
            if e.exit_code not in (0,):
                typer.secho(
                    "provision failed; continuing to run with available tools "
                    "(absent tools become coverage gaps).",
                    fg=typer.colors.YELLOW,
                )
    run(budget=timeout)
    if do_verify:
        ctx = AUDIT_ROOT / "evidence.yaml"
        fpath = findings if findings.is_absolute() else (AUDIT_ROOT / findings)
        if fpath.is_file():
            verify(findings=fpath, context=ctx)
    typer.echo("")
    typer.secho("PHASE 2 HAND-OFF:", bold=True)
    typer.echo("  Read REVIEW.md + audit/evidence.yaml (the ONLY evidence source).")
    typer.echo("  Write findings.yaml + REPORT.md at the project root.")
    typer.echo(
        "  Loop: uv run --project audit audit verify "
        "--findings findings.yaml --context audit/evidence.yaml  (until exit 0)"
    )
    typer.secho("  Findings are UNGATED until `audit verify` exits 0.", fg=typer.colors.YELLOW)


def main():
    app()


if __name__ == "__main__":
    sys.exit(app())
