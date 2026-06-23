# Audit Report — `multi-swe-bench` (re-audit @ HEAD `78d1ac6f`)

**Disposition: `HOLD`** · Phase-2 review of the *freshly regenerated*
`audit/evidence.yaml`. Cites only that evidence. Contract: [`CRUCIBLE.md`](CRUCIBLE.md).

| | |
|---|---|
| Git SHA | `78d1ac6fee1feb26fd9e6b4b8ba89f3b5be909f2` |
| Working tree | **dirty** (uncommitted: base pin + `ruff format` + `# nosec B404/B105` + sanitizer-aware check + org/repo sanitize + accepted-residual scope note) |
| Instruments | 10 — **all ran** (`pytest` clean via `AUDIT_PROJECT_PYTHON`; ② per-instance lint active) |
| Findings | **44** (44 MEDIUM, 0 HIGH/CRITICAL) — all one category (per-instance Dockerfile hygiene, in-scope) |
| Coverage gaps | 2 (both HOLD; `dockerfile-static-residual` now that ② ran) |
| Supply-chain / secrets | **clean** (pip-audit + osv on pinned lockfile; gitleaks tree+history) |

## Why HOLD
- **Not BLOCK** — no CRITICAL-floor issue; supply-chain + secrets clean.
- **Not SHIP** — self-attested run (no external `AUDIT_TRUST_ROOT_KEY`) + 2 coverage
  gaps. Mechanically unreachable on a producer host, by design.

## The 44 findings — per-instance Dockerfile hygiene (in-scope, low priority)
With `pytest` unblocked (project interpreter supplied), ② now renders + hadolint-lints
a per-language sample of the **generated per-instance Dockerfiles** and surfaces 44
**hygiene** items (all MEDIUM): `DL3008`/`DL3018`/`DL3033`/`DL3013` (pin apt/apk/yum/pip
versions) ×18, `DL3015`/`DL3009`/`DL3032` (`--no-install-recommends` / clean package
lists) ×18, `DL3059`/`DL4006`/`SC2046`/`DL3007` (merge RUNs / pipefail / quoting / one
stray `latest`) ×8. These are **best-practice, not security** — no untrusted-input
injection — on Dockerfiles generated from the `harness/repos/**` registry layer.
**Owner decision: kept in-scope** (acknowledged as known low-priority items). Real fix =
harden the generation templates/logic; deferred. All 0 CRITICAL/HIGH.

## Re-audit note (vs the previous run)
Two fixes applied since the clean HEAD re-audit: (1) the generator base image was
pinned (`ubuntu:latest` → `ubuntu:24.04`) → **`DL3007` resolved** (MEDIUM 18 → 17);
(2) `ruff format` applied to the 15 drifted files → **formatting findings resolved**
(LOW 15 → 0). Both are uncommitted (tree dirty; R3-state caps HOLD until committed).
The remaining 17 MEDIUM are unchanged (curated-input Dockerfile interpolation ×10,
`B404` informational ×6, `B105` false positive ×1).

## Findings

### MSB-CONTAINER-001 — Interpolation into generated Dockerfile lines · MEDIUM · *10 → 2 (sanitizer-aware)*
**Update:** the `dockerfile_generation_check` was made **sanitizer-aware** (it now skips
interpolation provably routed through `_safe_path_component` / `is_valid_env_name` /
`escape_env_value`, inline or transitively, and still flags any raw value — proven by a
new negative-control test). That cleared **8 of 10** false positives. The **2 remaining**:
`image.py:381` (a `re.compile()` pattern matching existing Dockerfiles — `repo` is
sanitized + `re.escape`'d; a check false-positive) and `image.py:448` (`ARG REPO_URL`
built from curated `org`/`repo` — low risk, not via the path sanitizer). Both acknowledged.

`dockerfile_generation_check` flags 10 interpolation sites (`env_to_dockerfile.py`
122/140/148; `image.py` 116/134/227/361/380/382/444) where a value is pasted into a
`RUN`/`WORKDIR`/`ENV` line. The check is a **heuristic for interpolation presence**;
it cannot see the threat model.

**Threat model (producer-supplied):** `org`/`repo` are **not attacker-controlled** —
they are **manually curated** by the maintainers (instance scripts under
`harness/repos/**`), so an injection would require a hostile entry passing manual
curation, not external input. The genuinely external input (PR `fix_patch`/`test_patch`
from arbitrary contributors) is a *different* surface — written to files via `COPY`,
not interpolated into `RUN` — so it is not this finding.

**Defense-in-depth (present in code):** `pr.repo` is validated by
`_safe_path_component` (raises on bad names; `image.py:19`, applied `:226`/`:359`);
ENV names via `is_valid_env_name`, values via `escape_env_value`
(`env_to_dockerfile.py:19,23`).

**Assessment:** realistic exploitability is **LOW** (curated inputs + defensive
sanitization) — effectively *defense-in-depth / informational*, not a live injection.
Kept at MEDIUM/HOLD only because recall can't drop an instrumented ≥MEDIUM issue and
the gate does not adjudicate exploitability; **priority Low**.

### MSB-CONTAINER-002 — Unpinned base image · **RESOLVED (instrumented) / partial (registry)**
Fixed the source of the instrumented finding: the generator default base was pinned
`ubuntu:latest` → `ubuntu:24.04` (`env_to_dockerfile.py:109,130`). Re-audit confirms
**0 hadolint findings** — `DL3007` cleared.

**Honest caveat:** the per-instance scripts under `harness/repos/**` (the
excluded/regenerated surface) independently hardcode their own base — many use
`return "ubuntu:latest"` + `FROM ubuntu:latest` (e.g. `pyccel_1896`, `aiohttp_*`).
Those production bases are **not** pinned by this fix and were not in the instrumented
finding (hadolint only linted the generator-utility Dockerfile; ②'s per-instance lint
was in the no-interp gap). Fully pinning production builds means regenerating those
registry templates (or running ② with `AUDIT_PROJECT_PYTHON` to surface them).

### MSB-SUPPLY-003 — `subprocess` import across utilities · **RESOLVED**
Suppressed with documented `# nosec B404` on all 6 import lines (verified: every call
site routes exec through `safe_subprocess` `safe_run`/`safe_popen`; the import is for
`PIPE`/exception types only; raw calls live in the B603-checked wrapper). Re-audit:
**0 `B404` findings**.

### MSB-FP-004 — bandit B105 "hardcoded password 'PASS'" · **RESOLVED**
`test_result.py:23` — `'PASS'` is the verdict-status enum, not a credential. Suppressed
with documented `# nosec B105`. Re-audit: **0 `B105` findings**.

### MSB-HYGIENE-005 — Formatting drift · **RESOLVED**
Fixed: `ruff format` applied to the 15 drifted non-registry files. Re-audit confirms
**0 ruff-format findings** (LOW 15 → 0).

## Coverage gaps (both cap HOLD) — **ACCEPTED RESIDUALS**
Both are recorded in `scope.yaml › accepted_residuals` as knowing owner decisions for
this **internal intermediate stage** (output feeds the downstream trajectory repo; not
a customer trust boundary). They still cap HOLD (fail-closed preserved), but they are
**not open action items** — clean HOLD is the accepted ceiling here.
- **`D-COVERAGE-GAP-repo-cache-history`** — third-party bare git mirrors in history
  (~20 MB). Untracked + gitignored (no new bloat); existing history bloat **accepted**
  (full purge = opt-in history rewrite).
- **`D-COVERAGE-GAP-dockerfile-sample-no-interp`** — ②'s per-instance Dockerfile lint
  needs `AUDIT_PROJECT_PYTHON`; the static-sample residual (no live image scan) is
  **accepted**.

*Also accepted:* self-attested provenance (no external `AUDIT_TRUST_ROOT_KEY`) caps
HOLD by design — no CI signing on this repo; external attestation is a downstream
concern. **SHIP is intentionally unreachable here; clean HOLD is the bar.**

## Bug Tickets

**[MSB-CONTAINER-001]** *Confirm each interpolation site routes through a sanitizer* —
Severity MEDIUM, **Priority Low** (inputs are curated, not attacker-controlled;
sanitizers are defense-in-depth). The escaping (`is_valid_env_name`/`escape_env_value`/
`_safe_path_component`) is present; optionally add a negative test injecting a hostile
`repo`/env and assert no Docker directive escapes. **Acceptance:** documented that each
of the 10 sites is either curated-input or sanitized.

**[MSB-CONTAINER-002]** *Pin generated base image* — **DONE**: `ubuntu:latest` →
`ubuntu:24.04` (`env_to_dockerfile.py:109,130`); `DL3007` cleared in re-audit.

**[MSB-SUPPLY-003]** *Audit subprocess call sites* — **DONE**: all 6 route through
`safe_subprocess` (list-form, no shell); `# nosec B404` added; 0 `B404` on re-audit.

**[MSB-FP-004]** *Suppress B105 FP* — **DONE**: `# nosec B105` at `test_result.py:23`;
0 `B105` on re-audit.

**[MSB-HYGIENE-005]** *Apply ruff format* — **DONE**: `ruff format` applied to 15
files; 0 ruff-format findings on re-audit.

## Loop
`uv run --project audit audit verify --findings findings.yaml --context audit/evidence.yaml`
→ **HOLD, exit 0** (gate verified the findings are complete + consistent at HEAD).
