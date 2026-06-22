---
description: Run the CRUCIBLE audit gate and review against instrumented evidence
---

Defer to `CRUCIBLE.md` (the contract) — this wrapper does not restate it.

1. Instrument: `uv run --project audit audit all -t 900` (evidence only, not a gate).
2. Review: read `REVIEW.md` + `audit/evidence.yaml` (the ONLY evidence source);
   write `findings.yaml` + `REPORT.md` at the project root.
3. Gate (loop until exit 0):
   `uv run --project audit audit verify --findings findings.yaml --context audit/evidence.yaml`

Findings are UNGATED until `audit verify` exits 0.
