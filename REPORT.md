# Audit Report — `multi-swe-bench`

**Disposition: `HOLD`** · Phase-2 review of the instrumented evidence
(`audit/evidence.yaml`). This report cites only that evidence; every quantitative
claim traces back to it. The contract is [`CRUCIBLE.md`](CRUCIBLE.md).

| | |
|---|---|
| Git SHA | `23bd558266ce7add66308507812bb64f6fb0980b` |
| Working tree | **dirty** (gate harness + lockfile uncommitted) |
| Instruments run | 10 (9 produced evidence; `pytest` blocked) |
| Findings | 5 (4 MEDIUM, 1 LOW) — 0 CRITICAL, 0 HIGH |
| Coverage gaps | 2 (both HOLD) |
| Supply-chain vulns | **0** (pip-audit + osv-scanner clean on the pinned lockfile) |
| Secrets | **0** (gitleaks, working tree + full history) |

## Why HOLD (not SHIP, not BLOCK)

- **Not BLOCK:** no CRITICAL-floor issue fired — no injection/secret/RCE/dataset-leak
  was *confirmed*, supply-chain and secret scans are clean.
- **Not SHIP:** SHIP is **mechanically unreachable on this producer host** by the
  Trusted-Evidence Axiom — the run is self-attested (no external
  `AUDIT_TRUST_ROOT_KEY`), the tree is dirty/uncommitted, and two coverage gaps each
  cap HOLD. This is by design, not a defect.

## Instrument coverage

| Instrument | Status | Result |
|---|---|---|
| semgrep | ok | parsed, no findings surfaced |
| bandit | nonzero_exit | 7 MEDIUM (6× B404, 1× B105) |
| pip-audit | ok | clean (pinned `requirements.txt`) |
| osv-scanner | ok | clean |
| gitleaks | ok | clean (tree + `--all` history) |
| ruff | nonzero_exit | parsed |
| ruff-format | nonzero_exit | 15 LOW (format drift) |
| hadolint | nonzero_exit | 1 MEDIUM (DL3007) |
| trivy | ok | clean |
| **pytest** | **tool_blocked** | no project interpreter in the isolated harness (set `AUDIT_PROJECT_PYTHON`) |

## Findings

### MSB-CONTAINER-001 — Untrusted interpolation into generated Dockerfile lines · MEDIUM
The generator interpolates values into `RUN`/`ENV`/`ARG` lines at 10 sites:
`multi_swe_bench/utils/env_to_dockerfile.py:122,140,148` and
`multi_swe_bench/harness/image.py:116,134,227,361,380,382,444`. Instances are built
from crawled third-party PR metadata and parsed `env` output, so an attacker-
controlled `org`/`repo`/`ref`/env value could inject Docker directives or shell into
the build. **This is the trust boundary of the whole pipeline** (untrusted instance
metadata → generated build → reward). The deterministic check locates the sites;
**whether each value is actually attacker-reachable and unsanitised is a manual
judgement** the gate does not make — see ticket.

### MSB-CONTAINER-002 — Unpinned base image (`latest`) · MEDIUM
hadolint `DL3007` on the generator's emitted Dockerfile: the default base image uses
an unpinned `latest` tag → non-reproducible builds and silent base drift, which
undermines benchmark reproducibility.

### MSB-SUPPLY-003 — `subprocess` usage across harness utilities · MEDIUM
bandit `B404` in `build_lht_dataset.py:38`, `group_prs_by_tags.py:59`,
`docker_util.py:18`, `git_util.py:16`, `safe_subprocess.py:23`, `session_util.py:3`.
B404 is the *import*, expected for a docker/git orchestrator — not a vuln by itself.
The real risk is argv construction with untrusted input (overlaps MSB-CONTAINER-001);
the repo's `safe_subprocess.py` is the hardening surface to verify.

### MSB-FP-004 — bandit B105 "hardcoded password 'PASS'" · MEDIUM · **false positive**
`test_result.py:23` — `'PASS'` is a test-status enum value, not a credential.
Acknowledged (not waived) to satisfy recall; no remediation beyond an optional
`# nosec`.

### MSB-HYGIENE-005 — Source formatting drift · LOW
15 files fail `ruff format --check` (hygiene only). `ruff format` clears them.

## Coverage gaps (both cap HOLD)

- **`D-COVERAGE-GAP-repo-cache-history`** — tracked third-party bare git mirrors under
  `multi_swe_bench/collect/.repo_cache/**` (70 blobs, ~20 MB) inflate clones and embed
  foreign history. Secret-scanned, but bloat/provenance is flagged, not gated.
- **`D-COVERAGE-GAP-dockerfile-sample-no-interp`** — the per-instance generated-
  Dockerfile lint (`dockerfile_sample_check`) could not render because no interpreter
  with the project's deps was available. Set `AUDIT_PROJECT_PYTHON` (CI) to activate
  it; it then lints a per-language sample with RUN layers (residual: not exhaustive +
  no live image scan).

## Bug Tickets

> JIRA-style. Severity is the canonical scale; priority reflects security relevance.

---
**[MSB-CONTAINER-001] Confirm/raise: untrusted interpolation into generated Dockerfiles**
- **Type:** Bug / Security · **Severity:** MEDIUM · **Priority:** High
- **Component:** harness/container-generation
- **Locations:** `multi_swe_bench/utils/env_to_dockerfile.py:122,140,148`;
  `multi_swe_bench/harness/image.py:116,134,227,361,380,382,444`
- **Evidence:** `dockerfile_generation_check:interpolated-run-line` ×10 (see findings.yaml ids)
- **Steps:** for each site, trace the interpolated value to its source; determine if
  an attacker-controlled instance (`org`/`repo`/`ref`/`env`) can reach it unsanitised.
- **Fix:** validate/escape interpolated values; prefer `ARG` with allow-listed
  charset; never concatenate untrusted strings into `RUN`.
- **Acceptance:** each site either proven non-reachable (documented) or sanitised;
  add a negative test injecting a hostile `org`/`ref`.

---
**[MSB-CONTAINER-002] Pin generated base images to a digest/tag**
- **Type:** Bug · **Severity:** MEDIUM · **Priority:** Medium
- **Location:** generator default base image (hadolint `DL3007`)
- **Fix:** replace `latest` with a pinned tag or `@sha256:` digest.
- **Acceptance:** hadolint `DL3007` no longer fires on the emitted Dockerfile.

---
**[MSB-SUPPLY-003] Review argv construction in subprocess wrappers**
- **Type:** Task / Security · **Severity:** MEDIUM · **Priority:** Medium
- **Locations:** `build_lht_dataset.py:38`, `group_prs_by_tags.py:59`,
  `docker_util.py:18`, `git_util.py:16`, `safe_subprocess.py:23`, `session_util.py:3`
- **Fix:** confirm all call sites pass list-form argv (no `shell=True` with
  interpolation); route through `safe_subprocess`.
- **Acceptance:** documented review; no `shell=True` with untrusted input.

---
**[MSB-FP-004] Suppress bandit B105 false positive**
- **Type:** Chore · **Severity:** MEDIUM (FP) · **Priority:** Low
- **Location:** `multi_swe_bench/harness/test_result.py:23`
- **Fix:** `# nosec B105` with a comment that `'PASS'` is a test status.

---
**[MSB-HYGIENE-005] Apply ruff format**
- **Type:** Chore · **Severity:** LOW · **Priority:** Low
- **Fix:** `ruff format` (15 files). **Acceptance:** `ruff format --check` clean.

---

## Loop
`uv run --project audit audit verify --findings findings.yaml --context audit/evidence.yaml`
→ expected `HOLD`, exit 0 (the gate verified the findings are complete + consistent).
