---
description: Run the CRUCIBLE audit gate, then review against instrumented evidence
agent: build
---

The contract is `@CRUCIBLE.md` — defer to it; this command does not restate it.

**Phase 1 — instrument (produces evidence, NOT a gate):**

`!uv run --project audit audit all -t 900`

**Phase 2 — review.** Read `@REVIEW.md` as your instructions and
`@audit/evidence.yaml` as the ONLY source of instrumented evidence. Then write
`findings.yaml` and `REPORT.md` at the project root (strip any
`_template`/`_example` keys; `REPORT.md` is the single human report and its
**Bug Tickets** section carries the JIRA-style tickets). Cite only
verifier-emitted `issue_instance_id`s; never invent spans, runs, or scores.

**Phase 3 — gate.** Loop until this exits 0:

`uv run --project audit audit verify --findings findings.yaml --context audit/evidence.yaml`

Findings are UNGATED until `audit verify` exits 0. Expect a `HOLD` cap on a
producer-controlled host (dirty tree + no external trust-root signature) — that
is by design, not a defect to fix.
