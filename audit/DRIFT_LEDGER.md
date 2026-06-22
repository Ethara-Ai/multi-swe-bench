# Drift ledger — Bucket-D guarantees → conformance tests

**Executable ledger.** Every Bucket-D guarantee needs a passing conformance
(negative-control) test. A row is `Implemented` **only** with a passing test;
otherwise it is non-operative and caps the disposition (`D-COVERAGE-GAP`).
Editing `CRUCIBLE.md`/`scope.yaml` and re-running reconciles the harness through
this ledger — never silently.

Run the controls: `uv run --project audit --extra dev python -m pytest audit/tests -q` (60 passing).

| # | Guarantee (Bucket-D atom) | Module | Conformance test(s) | Status |
|---|---------------------------|--------|---------------------|--------|
| R1a | Omitted ≥MEDIUM issue is caught (per-instance recall) | `recall.py` | `test_recall_negative_controls::test_omitted_issue_fails` | **Implemented** |
| R1b | Empty/all-clear passes only if every required instrument ran clean+parsed | `recall.py`,`verifier.py` | `::test_empty_all_clear_with_unclean_required_fails` | **Implemented** |
| R1c | Fabricated acknowledgement id rejected (ids are verifier-owned) | `recall.py` | `::test_fabricated_ack_id_fails` | **Implemented** |
| R1d | CRITICAL-floor issue cannot be waived to SHIP | `recall.py`,`policy.py` | `::test_critical_floor_cannot_be_waived_to_ship` | **Implemented** |
| R2 | Span resolution: traversal / escape / nonexistent / out-of-range / valid | `verifier.py` | `test_span_negative_controls` (5 cases) | **Implemented** |
| R3 | Only `ok`/`nonzero_exit` runs back a finding; blocked/timeout do not | `verifier.py` | `test_run_evidence_negative_controls::test_{blocked,timeout}_run_cited_*` | **Implemented** |
| R3-state | SHIP needs non-null SHA + clean tree + pinned DBs | `verifier.py` | `::test_dirty_tree_blocks_ship`, `::test_unpinned_db_blocks_ship` | **Implemented** |
| R4 | CVSS v3.1 recompute; wrong `cvss_base` / malformed CWE rejected | `cvss.py` | `test_cvss_negative_controls` (4 cases) | **Implemented** |
| R6 | Closed disposition/severity sets; summary tally sums to finding count | `verifier.py` | `test_vocab_negative_controls` (2 cases) | **Implemented** |
| P1 | Context integrity: post-run edit of `evidence.yaml` caps BLOCK | `provenance.py` | `test_provenance_negative_controls::test_context_edited_after_run_blocks` | **Implemented** |
| P2 | Scope integrity: manifest/approved digest drift caps BLOCK | `provenance.py` | `::test_scope_drift_blocks` | **Implemented** |
| P3 | Command/config integrity: argv swap + artifact substitution cap BLOCK | `provenance.py` | `::test_argv_swap_in_evidence_blocks`, `::test_artifact_substitution_blocks` | **Implemented** |
| P4 | Source integrity (R2 against snapshot — TOCTOU closed); live drift caps SHIP | `provenance.py` | `::test_source_drift_caps_below_ship` | **Implemented** |
| P5 | Trusted-Evidence Axiom: missing manifest → HOLD; self-attested → HOLD (never SHIP) | `provenance.py` | `::test_missing_manifest_caps_hold`, `::test_self_attested_caps_hold_not_ship` | **Implemented** |
| D1 | reward-bucket consistency (p2p-regression, bucket-overlap → CRITICAL) | `domain.py` | `test_domain_checks::test_{reward_miscount,bucket_overlap}_is_critical` | **Implemented** |
| D2 | dataset leakage (solution-in-prompt containment → CRITICAL) | `domain.py` | `::test_solution_leakage_is_critical`, `::test_clean_record_no_critical` | **Implemented** |
| D3 | reward provenance (tamper-guard defined but not enforced → CRITICAL) | `domain.py` | `::test_reward_provenance_detects_unenforced_guard` | **Implemented** |
| D4 | Missing dataset → coverage gap (fail closed, not silent pass) | `domain.py` | `::test_no_dataset_is_coverage_gap` | **Implemented** |
| D5 | report-claim reconciliation (counts/join-id/verdict from raw) | `domain.py` | exercised by `audit run`; no dedicated unit control yet | **Implemented (integration only)** |
| D6 | Dockerfile-generation injection / unpinned base | `domain.py` | exercised by `audit run` (13 MEDIUM on real tree); no dedicated unit control yet | **Implemented (integration only)** |
| ID | `cluster_fingerprint` survives `ruff format`; distinct rules distinct fps | `normalize.py` | `test_fingerprint_stability` (2 cases) | **Implemented** |
| AL | Approved `ignore_allowlist` drops SAST/hygiene noise, keeps secrets+deps, recorded | `audit.py`,`evidence.py` | `test_allowlist_enforcement` (3 cases) | **Implemented** |
| ST | Required-but-not-instrumented tool → coverage gap (starvation fail-closed) | `audit.py` | exercised by `audit run`; no dedicated unit control yet | **Implemented (integration only)** |
| FZ | Verifier never returns OK on an invariant violation; never crashes | `verifier.py` | `test_selffuzz` (Hypothesis) | **Implemented** |
| WIRE1 | `ruff-format` / `pytest` / `hadolint` / `trivy` wired into the runner; each output parsed to normalized seeds (fail-closed on unparsable) | `tools.py`,`normalize.py` | `test_wired_instruments` (parsers, 8 cases) | **Implemented** |
| WIRE2 | hadolint lints the project's OWN generator output (stdin); absent generator → None | `tools.py` | `test_wired_instruments::test_emit_dockerfile_*` (2 cases) | **Implemented** |
| WIRE3 | Fail-closed: stdin provider → None blocks the tool; no project interpreter blocks pytest (remediation recorded) | `tools.py` | `test_wired_instruments::test_{stdin_provider_none,unresolvable_binary}_blocks` (2 cases) | **Implemented** |
| DF1 | ② Generated-Dockerfile sampling: render a per-language sample of real instance `dockerfile()` output (with RUN layers) via the project interpreter + hadolint each; fail closed (no interp / driver crash / hadolint absent → coverage gap); success → findings + a residual gap (not exhaustive / no live image scan) | `dockerfiles.py`,`audit.py` | `test_dockerfile_sampling` (6 cases) | **Implemented (wired into `audit run`; single source of truth for the container-static surface)** |

## Declared coverage gaps / non-applicable atoms (honest absence)

| Atom (from CRUCIBLE §1.5) | Status | Why |
|---------------------------|--------|-----|
| `multimodal_dataset_leakage_check` (image/audio/video) | **Not applicable** | No multimodal surface in scope — the deliverable is text JSONL. Re-scope if media records appear. |
| Runtime `rollout_integrity_check` (job×task×agent×model×attempt×seed) | **Reframed** | This repo creates a dataset, not runtime rollouts; replaced by `reward_bucket_consistency_check` (scope notes this). |
| `.crucible/semgrep` custom-rule digest pinning | **Open limitation** | The approved `scope.yaml` references `.crucible/semgrep` in the semgrep argv but does **not** pin its content digest in `command_config_policy.config_digests`. The rules exist (`.crucible/semgrep/harbor.yml`, 8 rules) but are not digest-bound. semgrep is also `tool_blocked` on this host → already a `HOLD` coverage gap. **Fix = re-scope** (add the harbor.yml digest) + re-sign-off; that is the sanctioned path and is deliberately not done silently here. |
| `hadolint`, `trivy`, `ruff-format`, `pytest` live runs | **Wired (WIRE1–3)** | Now in the runner registry. `ruff-format`/`hadolint`/`trivy` run as standalone binaries; `pytest` resolves a project interpreter (`AUDIT_PROJECT_PYTHON` → project `.venv` → `VIRTUAL_ENV`), else records `tool_blocked` honestly (the audit venv lacks the project's deps by design). hadolint is fed the project generator's emitted Dockerfile via stdin (`DL3007` unpinned base). ② (`dockerfile_sample_check`, DF1) additionally renders + hadolint-lints a per-language sample of full per-instance Dockerfiles (with RUN layers) — 44 MEDIUM findings under a project interpreter. The scope no longer declares `D-COVERAGE-GAP-dockerfile-static`; the harness check is the single source of truth, emitting a precise residual gap (sample not exhaustive + no live `docker build`/`trivy image` scan) that still caps HOLD. |

## D5/D6/ST upgrade path

D5, D6, ST are proven by the real `audit run` (their issues/gaps appear in
`evidence.yaml`) but lack an isolated `tmp_path` negative control. They are
marked `Implemented (integration only)` rather than `Implemented` to stay honest.
Promoting them to full `Implemented` = add a `tmp_path` control that constructs a
crafted dataset/Dockerfile/scope and asserts the exact issue/gap fires.
