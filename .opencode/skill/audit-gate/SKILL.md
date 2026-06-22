---
name: audit-gate
description: >-
  Use when asked to audit, gate, or adversarially review this repo's deliverables
  (the multi-swe-bench dataset/grader) before hand-off — runs the CRUCIBLE
  instruments and gates findings against instrumented evidence.
---

The full contract is `CRUCIBLE.md` at the project root. **Defer to it — do not
restate the axes, severity scale, or disposition vocabulary here** (duplicating
the contract is a drift bug).

The two reviewer artifacts you produce are **`findings.yaml`** and **`REPORT.md`**
at the project root. The only source of instrumented evidence you may cite is
**`audit/evidence.yaml`**.

Run playbook: `audit/README.md`. The loop is `audit all` → write the two
artifacts from `audit/evidence.yaml` → `audit verify` until it exits 0.
