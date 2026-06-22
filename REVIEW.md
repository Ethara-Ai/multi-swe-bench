# REVIEW — Phase-2 instructions for the reviewing model

You are the adversarial reviewer for `multi-swe-bench`. The **contract** is
[`CRUCIBLE.md`](CRUCIBLE.md) — read it; this file does not restate the axes, the
severity scale, or the disposition vocabulary. Your job is **Phase 2 only**:
turn instrumented evidence into two artifacts. Nothing you do changes the contract.

## Inputs (the ONLY evidence you may cite)

- [`audit/evidence.yaml`](audit/evidence.yaml) — the committed evidence bundle:
  recon, per-tool reports with **real exit codes**, the run log,
  `normalized_issues[]` (each with a verifier-emitted `issue_instance_id`),
  `coverage_gaps[]`, and not-run/blocked records. **This is the only source of
  instrumented evidence.** Do not run tools yourself, infer findings the
  instruments did not produce, or cite anything outside this bundle.

## Outputs (write both at the project root)

1. **`findings.yaml`** — one entry per finding. Each entry:
   - cites a verifier-emitted `issue_instance_id` (never an invented one);
   - resolves every `path:line` span against the real tree (R2);
   - carries a canonical severity (`INFO`..`CRITICAL`) and, for any CVSS claim, a
     full v3.1 vector with a `cvss_base` that recomputes (R4);
   - acknowledges **every** ≥ `MEDIUM` issue from a parsed run (R1) — omission is
     the first bug the gate catches;
   - records each `coverage_gap` honestly (a gap caps the disposition; it is never
     a silent pass);
   - uses a waiver only with a reason-code + fingerprint-bound rationale (and an
     out-of-band approval for HIGH/CRITICAL/security). A CRITICAL-floor issue
     cannot be waived to SHIP.
   - Strip any `_template`/`_example` scaffolding keys before submitting.

2. **`REPORT.md`** — the single human report. Its **Bug Tickets** section carries
   the JIRA-style tickets (there is no separate `BUGS.md`). Every quantitative
   claim must trace back to a raw artifact in `audit/evidence.yaml`.

## The disposition you will get here

`SHIP` is **mechanically unreachable on a producer-controlled host** — there is no
external `AUDIT_TRUST_ROOT_KEY`, so the run is self-attested and capped at `HOLD`
by the Trusted-Evidence Axiom; required scanners that are `tool_blocked` on this
host add HOLD coverage gaps. Expect **HOLD** when your findings are honest and
complete. That is by design, not a defect to engineer around.

## The loop

```bash
uv run --project audit audit verify --findings findings.yaml --context audit/evidence.yaml
```

Run it until it exits `0`. A non-zero exit names the violated rule (R1 recall, R2
span, R3 completed-run, R4 CVSS, R6 vocabulary, or a provenance pre-check) — fix
the artifact, never the evidence. Findings are **UNGATED until `audit verify`
exits 0**.
