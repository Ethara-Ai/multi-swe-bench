# CRUCIBLE — the audit contract for `multi-swe-bench`

This file is the **single source of the contract**. Every wrapper (the
`.opencode`/`.claude` commands, the `audit-gate` skill, `audit/README.md`) defers
to this file and never re-states the axes, the severity scale, or the disposition
vocabulary. A wrapper that duplicates them is a drift bug. The run playbook lives
once in [`audit/README.md`](audit/README.md); there is no `AUDIT.md`, no `BUGS.md`
(folded into `REPORT.md`), and no `SCOPE.md` (the machine scope is
[`audit/scope.yaml`](audit/scope.yaml)).

The gate is the **last line of defence** before deliverables reach high-IQ
adversarial reviewers. Any hole that ships is fatal, and the producer who fills in
the findings is **not a trusted party**. The gate grounds every audit in real tool
output and is built to survive a motivated liar.

---

## What this project is (the scoped subject)

`multi-swe-bench` is the **dataset-creation** half of a multilingual
SWE-bench-style pipeline: it crawls GitHub PRs, builds per-instance Docker
environments, runs the gold test/fix patches in-container to capture verdicts, and
emits a **JSONL dataset** (one record per resolved PR — gold `fix_patch` /
`test_patch` + reward buckets) consumed downstream by a separate trajectory repo.
The auditable codebase is the pure-Python framework under `multi_swe_bench/**`; the
4832 generated scripts under `multi_swe_bench/harness/repos/**` are dataset/config
definitions (the **excluded registry surface** — still secret-scanned). The
authoritative, machine-readable scope — surfaces, required instruments, command/
config policy, coverage gaps, ambiguities — is [`audit/scope.yaml`](audit/scope.yaml),
pinned by its SHA-256 in [`audit/scope.approved`](audit/scope.approved).

---

## First principles (non-negotiable)

- **Provenance ≠ validity ≠ relevance.** The gate owns provenance, co-owns
  validity, and is mostly blind to relevance. A tool exiting `0` is a measurement
  under a configuration, not truth.
- **No invented evidence.** Every span resolves, every cited run completed, every
  CVSS recomputes. A fabricated span or a phantom/blocked run cited as proof is the
  first bug to catch.
- **Absence is a result.** Every absent surface is stated explicitly; silence
  never reads as safety.
- **Determinism over vibes.** Any atom specifiable as a total, bounded,
  recomputable relation is **Bucket D** (harness code) or a **D-COVERAGE-GAP** —
  never an LLM prompt. A decidable-but-unimplemented atom is a `D-COVERAGE-GAP`,
  never reclassified as judgment. Total policies; unknowns fail closed.
- **Fail closed everywhere.** A required instrument that cannot run, parse, or
  cover a present surface **caps the disposition at HOLD/BLOCK** — never a silent
  pass.
- **Executable drift ledger.** Every Bucket-D guarantee needs a passing
  conformance (negative-control) test. An unimplemented guarantee is non-operative
  and caps the disposition. A row in [`audit/DRIFT_LEDGER.md`](audit/DRIFT_LEDGER.md)
  is `Implemented` **only** with a passing test. Editing this file and re-running
  reconciles the harness through the ledger — never silently.

---

## Vocabulary (defined ONCE here)

**Severity — canonical 5:** `INFO` · `LOW` · `MEDIUM` · `HIGH` · `CRITICAL`.
The native→canonical map is a hash-bound **total** function: an unknown native
severity fails closed to ≥ `MEDIUM` (never `INFO`); a required-instrument finding
is never mapped below `MEDIUM`.

**CRITICAL floor (hash-bound, un-waivable to SHIP):** injection · unsafe
deserialization · SSRF · authz-bypass · secret exposure · memory corruption ·
CVSS ≥ 9 or KEV · container escape · dataset leakage · agent-writable reward ·
rollout miscount.

**Disposition — exactly three:** `SHIP` · `HOLD` · `BLOCK`. Severity is
independent of disposition; tallies sum to the finding count. A coverage gap or a
failed integrity pre-check **caps** the disposition (HOLD or BLOCK); it never
silently passes.

**Evidence classes:** `static_reproducible` · `dynamic_live` · `heuristic` ·
`domain_integrity`. DAST and deep-ML similarity are **veto-only** (may cap HOLD /
corroborate BLOCK, never support SHIP). The commodity hygiene tier (ruff, format,
type-check, pytest) is **necessary, never sufficient**.

---

## The phases

**Phase 0 — scope (READ-ONLY except `audit/scope.yaml`).** Inventory the tree,
detect deliverable surfaces (present/absent + paths), classify ecosystems/product
types, and **derive** the required-instrument set from the surfaces — the producer
does not choose it. A surface with no automated instrument becomes a declared
coverage gap. On an **UPDATE run**, re-derive scope from the current tree and diff
against the existing `scope.yaml`: new surfaces become newly-required instruments;
preserve the drift ledger, negative-control tests, custom `.crucible/semgrep` rules
and recorded waivers (append, never discard); bump `policy_version` only when the
required-instrument set changes. Silently dropping a previously-required instrument
is the **starvation bypass** and is forbidden.

**Phase 0.5 — scope sign-off gate (ENFORCED in code).** A human writes
`sha256(scope.yaml)` into `scope.approved`. Every harness command's first action
recomputes `sha256(scope.yaml)` and compares it (`scopelib.load_approved_scope`):
missing or mismatched → **hard exit, nothing written**. The approved digest binds
**both** the required-instrument set (R1 reads `required_instruments` from the
approved scope only) **and** the command/config policy (§1.10 compares live argv/
config digests against the approved policy). Re-scoping — or quietly widening an
ignore file — invalidates the digest and re-triggers sign-off. This defeats
required-set starvation and config starvation.

**Phase 1 — instrument (produces evidence, NOT a gate).** `audit run` runs every
applicable instrument, captures **real exit codes**, writes full stdout/stderr to
`audit/results/` (gzipped, head+tail capped), normalizes to `NormalizedIssue`s with
content-anchored multiset identity, runs the Bucket-D domain checks, and emits the
committed [`audit/evidence.yaml`](audit/evidence.yaml) + the §1.10 provenance
manifest. Per-tool run status is a machine enum: `ok` · `nonzero_exit` · `timeout`
· `tool_blocked` (nonzero ≠ error for many scanners).

**Phase 2 — review (the producer's only territory).** A model reads
[`REVIEW.md`](REVIEW.md) as instructions and `audit/evidence.yaml` as the **ONLY**
source of instrumented evidence, then writes `findings.yaml` + `REPORT.md` at the
project root. It may cite only verifier-emitted `issue_instance_id`s. Nothing the
model does changes the contract.

**Phase 3 — gate.** `audit verify` runs the six deterministic rules + the
provenance pre-checks against `findings.yaml`. It exits `0` **only** when all hold
— the only command whose success means the gate passed. Findings are **UNGATED
until `audit verify` exits 0**.

---

## Required instruments (derived from the surfaces — see `scope.yaml`)

- **Taint-aware SAST:** Semgrep with explicit packs `p/owasp-top-ten` +
  `p/r2c-security-audit` + `p/secrets` + `.crucible/semgrep` (Harbor-domain rules),
  `--metrics=off`, **never `--config auto`**; plus `bandit`.
- **Supply chain:** `pip-audit`, `osv-scanner` (CVSS ≥ 9 / KEV / reachable-RCE
  floor CRITICAL).
- **Secrets:** `gitleaks` over the working tree **and full history**
  (`--log-opts=--all`) — scans the excluded registry surface too.
- **Container:** `hadolint` + `trivy` fed the **generated** Dockerfile (no static
  Dockerfile in tree → declared coverage gap until the generator-emitter atom
  emits one).
- **Bespoke domain-integrity (Bucket D, §1.5 — no scanner ships these):**
  `reward_provenance_check`, `reward_bucket_consistency_check` (replaces runtime
  rollout integrity for this dataset repo), `report_claim_artifact_check`,
  `dataset_leakage_check`, `dockerfile_generation_check`.
- **Hygiene tier (necessary, never sufficient):** `ruff`, `ruff format`, `pytest`.

Each DB-backed scanner's snapshot version/digest is pinned in recon; an unpinned
required DB caps **HOLD**. Forbid `semgrep --config auto`.

---

## The six verifier rules (`audit/verifier.py`)

- **R1 recall** — per-instance verifier-owned ids; every ≥ `MEDIUM` issue from a
  parsed run must be acknowledged; an empty/all-clear report passes **only** if
  every required instrument ran clean and parsed; effective severity = `max` over
  acknowledged issues (not the producer's label); waiver discipline (reason-code
  enum + fingerprint-bound rationale + out-of-band approved waiver for
  HIGH/CRITICAL/security; boilerplate reused across unrelated fingerprints
  rejected). A CRITICAL-floor issue cannot be waived to SHIP.
- **R2 span resolution** — every `path:line` resolves against the Phase-1 source
  manifest (realpath inside the audit root, regular file, line in range; no `..`,
  no symlink escape).
- **R3 completed-run evidence** — only `ok`/`nonzero_exit` runs back a finding;
  `tool_blocked`/`timeout` back only a coverage gap. **R3-state:** SHIP requires a
  non-null git SHA + clean tree + pinned DBs.
- **R4 CVSS form-vs-truth** — parse the full v3.1 vector, recompute the base score
  offline with the pinned calculator (`audit/cvss.py`), reject any mismatched
  `cvss_base`; CWE format + registry membership are Bucket-D, appropriateness is
  judgment.
- **R6 vocabulary** — dispositions `SHIP`/`HOLD`/`BLOCK` only; severity
  independent of disposition; tallies sum to the finding count.

(R5 is reserved; the rule set is R1–R4 + R6 by design.)

---

## 1.10 Provenance gate (`audit/provenance.py`, wired into `verify`)

A content-addressed **artifact-closure manifest**
(`audit/provenance.manifest.yaml`) covers the evidence digest, every citable source
(path/realpath/sha256/line-count/type), the git SHA + clean bit, every run's
argv/cwd/status/exit-code + stdout-artifact digest, the policy version, and the
scope + approved-scope digests. Pre-checks run **before** any other rule:

- **context integrity** — `sha256(evidence.yaml)` matches the manifest; a post-run
  edit caps **BLOCK**.
- **scope integrity** — `sha256(scope.yaml)` still equals `scope.approved` and the
  manifest snapshot.
- **command/config integrity** — each run's live argv/cwd/status + stdout digest
  matches the manifest; argv swaps and artifact substitution cap **BLOCK**.
- **source integrity** — R2 resolves the manifest snapshot (closes TOCTOU); live
  source drift caps **SHIP**.

**Trusted-Evidence Axiom.** A signature proves possession of a key, not honest
collection — so `trusted` is **computed by the verifier, never copied**. SHIP
requires a detached signature over the manifest's canonical bytes that verifies
against a trust root supplied **from outside** the audited repo
(`AUDIT_TRUST_ROOT_KEY`, e.g. a CI/KMS secret the producer cannot mint) **and** a
clean recorded tree **and** no source drift. Absent the external signature the run
is self-attested and the disposition is capped at **HOLD**; a broken-integrity or
unparsable manifest caps **BLOCK**; a missing manifest caps **HOLD**. **On a
producer-controlled host (no external key) SHIP is mechanically unreachable — by
design.**

---

## House rules

- Root-canonical files: **`CRUCIBLE.md`** (this contract) + **`REVIEW.md`** (the
  Phase-2 prompt) + **`findings.yaml`** + **`REPORT.md`** (the reviewer artifacts).
  The committed evidence bundle is **`audit/evidence.yaml`**. All other harness
  code, research, and docs live under `audit/`; raw per-command transcripts live
  under `audit/results/` (gitignored).
- The evidence bundle, scope, approval, harness, front doors and `.crucible/semgrep`
  rules are **tracked**; only volatile machine-specific state (`.venv/`,
  `results/`, caches, `provenance.manifest.sig`, local tool settings) is ignored.
- Bucket-D atoms are harness code, never prompts. A decidable-but-unimplemented
  atom is a `D-COVERAGE-GAP`, never reclassified as judgment.

The loop: `uv run --project audit audit all -t 900` → read `audit/evidence.yaml` →
write `findings.yaml` + `REPORT.md` → `audit verify` until it exits 0.
