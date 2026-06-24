# Audit Report — `multi-swe-bench` (complete re-audit)

**Disposition: `HOLD`** · `GATE PASS` (all six rules + provenance pre-checks hold).
Complete audit of the whole repo, all instruments active. Cites only
`audit/evidence.yaml`. Contract: [`CRUCIBLE.md`](CRUCIBLE.md).

| | |
|---|---|
| Git SHA | `bab761ef` |
| Instruments | **10 — all ran** (incl. `pytest`, ②, live image scan) |
| Findings | **67** (67 MEDIUM, **0 HIGH**) — **0 CRITICAL, 0 BLOCK** |
| Coverage gaps | 3 (all HOLD) |
| Supply chain / secrets / SAST / tests | **all clean** |

## Instrument coverage (all 10 ran)
| Surface | Instruments | Result |
|---|---|---|
| Taint SAST | semgrep, bandit | **clean** — bandit B404/B105 are documented `# nosec` (subprocess centralized in `safe_subprocess`; `'PASS'` is a verdict enum) |
| Supply chain | pip-audit, osv-scanner | **clean** — audited against the pinned `requirements.txt` |
| Secrets | gitleaks | **clean** — working tree + full history (`--all`) |
| Hygiene | ruff, ruff-format | **clean** (framework; the 4832-script registry layer is allowlisted) |
| Tests | **pytest** | **✅ pass (exit 0)** — grader self-tests green |
| Containers | hadolint, trivy, ② sample, live image | 94 findings (below) |

## Findings — 94 (all on the container surface)
Everything else is clean; the entire report is container hygiene + base-image CVEs.

### Per-instance Dockerfile hygiene — 44 · MEDIUM · in-scope, low priority
`dockerfile_sample_check` renders + hadolint-lints a per-language sample of the
**generated** instance Dockerfiles. Findings are version-pinning / package-cleanup /
shell-quoting best-practices (`DL3008/3009/3015/3018/3032/3033/4006/SC2046`) — **not
security**; inputs are curated + sanitized. Owner decision: kept in-scope. Fix =
harden the generation templates.

### Base-image CVEs — 23 · all MEDIUM · live scan
`live_image_scan` does `docker build` of the representative Dockerfile +
`trivy image` of the built image. The generator now **applies security patches at
build** (`apt-get upgrade`), which cleared the **HIGH** `libssl3` CVE (F075,
CVE-2026-45447) **and 27 other fixable CVEs (50 → 23)**. The remaining 23 are CVEs
with **no upstream fix** and not reachable in the build context. **0 CRITICAL/HIGH.**

## Resolved this pass (no longer firing)
- `DL3007` unpinned base → pinned `ubuntu:24.04`.
- 15 formatting drifts → `ruff format`.
- bandit `B404` ×6 + `B105` → documented `# nosec`.
- Dockerfile-interpolation ×10 → `dockerfile_generation_check` made **sanitizer-aware**
  (skips provably-sanitized sites, still flags raw) + `org`/`repo` genuinely sanitized.
- **HIGH base-image CVE (F075, `libssl3`) + 27 fixable CVEs → patched** by adding
  `apt-get upgrade` (security patches) to the generated base; live-scan 50 → 23, 0 HIGH.

## Coverage gaps — 3 (all cap HOLD)
- `repo-cache-history` — ~20 MB of third-party git mirrors in **past** history
  (accepted residual; only a force-push history rewrite clears it).
- `dockerfile-static-residual` — ② samples per-instance Dockerfiles, not exhaustive.
- `live-image-residual` — base image scanned live; per-instance images not all built.

## Why HOLD (not SHIP, not BLOCK)
- **Not BLOCK** — 0 CRITICAL; SAST, supply-chain, secrets, tests all clean.
- **Not SHIP** — self-attested provenance (no external `AUDIT_TRUST_ROOT_KEY` — by
  decision, no CI on this internal stage) + the 3 coverage gaps + a dirty tree (the
  audit's own regenerated artifacts are uncommitted). SHIP is mechanically unreachable
  on a producer host by design; clean HOLD is the ceiling here.

## Bug Tickets
**[MSB-CONTAINER-HYGIENE]** *Harden generated Dockerfile templates* — MEDIUM / Low.
Pin apt/apk/yum/pip versions, add `--no-install-recommends`, clean package lists,
quote shell expansions in the `harness/repos/**` generation. **Acceptance:** ② sample
hadolint-clean.

**[MSB-BASE-IMAGE]** *Patch/minimize the base image* — **DONE** (HIGH cleared):
`apt-get upgrade` added to `env_to_dockerfile.generate_dockerfile*` (the real
`build_dataset` path + the live-scan base) → F075 + 27 fixable CVEs patched, 0 HIGH.
Residual 23 MEDIUM are no-fix CVEs (reduce via a slim/distroless base). **Open
decision:** applying the same `apt-get upgrade` to the per-instance `image.py` build
trades base-CVE patching for build reproducibility — left to the owner (not changed).

**[MSB-REPO-CACHE]** *(optional, destructive)* purge `.repo_cache` from history via
`git filter-repo` + coordinated force-push. **Acceptance:** `repo-cache-history` clears.

## Loop
`uv run --project audit audit verify --findings findings.yaml --context audit/evidence.yaml`
→ **HOLD, exit 0**.
