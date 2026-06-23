# Audit Report — `multi-swe-bench` (re-audit @ HEAD `78d1ac6f`)

**Disposition: `HOLD`** · Phase-2 review of the *freshly regenerated*
`audit/evidence.yaml`. Cites only that evidence. Contract: [`CRUCIBLE.md`](CRUCIBLE.md).

| | |
|---|---|
| Git SHA | `78d1ac6fee1feb26fd9e6b4b8ba89f3b5be909f2` |
| Working tree | **dirty** (uncommitted base-image pin) |
| Instruments | 10 (9 produced evidence; `pytest` blocked — no project interpreter) |
| Findings | 32 (17 MEDIUM, 15 LOW) — 0 CRITICAL, 0 HIGH |
| Coverage gaps | 2 (both HOLD) |
| Supply-chain / secrets | **clean** (pip-audit + osv on pinned lockfile; gitleaks tree+history) |

## Why HOLD
- **Not BLOCK** — no CRITICAL-floor issue; supply-chain + secrets clean.
- **Not SHIP** — self-attested run (no external `AUDIT_TRUST_ROOT_KEY`) + 2 coverage
  gaps. Mechanically unreachable on a producer host, by design.

## Re-audit note (vs the previous run)
One fix applied since the clean HEAD re-audit: the generator base image was pinned
(`ubuntu:latest` → `ubuntu:24.04`), which **resolved `DL3007`** — MEDIUM count
18 → **17**. The pin is uncommitted, so the tree is dirty (R3-state caps HOLD; commit
it to clean that). The remaining 17 ≥MEDIUM are unchanged from the prior run (the rest
of the audited source is byte-identical).

## Findings

### MSB-CONTAINER-001 — Interpolation into generated Dockerfile lines · MEDIUM · *low realistic risk*
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

### MSB-SUPPLY-003 — `subprocess` import across utilities · MEDIUM
bandit `B404` in 6 files (import presence only). Exec risk is centralised in
`safe_subprocess.py` (list-form argv, no shell). Informational.

### MSB-FP-004 — bandit B105 "hardcoded password 'PASS'" · MEDIUM · **false positive**
`test_result.py:23` — `'PASS'` is the verdict literal, not a credential.

### MSB-HYGIENE-005 — Formatting drift · LOW
15 files fail `ruff format --check`. Run `ruff format`.

## Coverage gaps (both cap HOLD)
- **`D-COVERAGE-GAP-repo-cache-history`** — third-party bare git mirrors in history
  (~20 MB). The working tree is now untracked from them; *existing history* bloat
  persists until an opt-in history rewrite.
- **`D-COVERAGE-GAP-dockerfile-sample-no-interp`** — ②'s per-instance Dockerfile lint
  could not render (no project interpreter; set `AUDIT_PROJECT_PYTHON` in CI). The
  residual (no live image scan) is inherent.

## Bug Tickets

**[MSB-CONTAINER-001]** *Confirm each interpolation site routes through a sanitizer* —
Severity MEDIUM, **Priority Low** (inputs are curated, not attacker-controlled;
sanitizers are defense-in-depth). The escaping (`is_valid_env_name`/`escape_env_value`/
`_safe_path_component`) is present; optionally add a negative test injecting a hostile
`repo`/env and assert no Docker directive escapes. **Acceptance:** documented that each
of the 10 sites is either curated-input or sanitized.

**[MSB-CONTAINER-002]** *Pin generated base image* — **DONE**: `ubuntu:latest` →
`ubuntu:24.04` (`env_to_dockerfile.py:109,130`); `DL3007` cleared in re-audit.

**[MSB-SUPPLY-003]** *Audit subprocess call sites* — MEDIUM/Low. Confirm all route
through `safe_subprocess` (no `shell=True` + untrusted input).

**[MSB-FP-004]** *Suppress B105 FP* — Low. `# nosec B105` at `test_result.py:23`.

**[MSB-HYGIENE-005]** *Apply ruff format* — Low. `ruff format` (15 files).

## Loop
`uv run --project audit audit verify --findings findings.yaml --context audit/evidence.yaml`
→ **HOLD, exit 0** (gate verified the findings are complete + consistent at HEAD).
