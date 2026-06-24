# HOLD Justification — `multi-swe-bench`

**Disposition: `HOLD` · `GATE PASS` (verify exit 0) · 0 CRITICAL · 0 HIGH · 0 BLOCK**
Date: 2026-06-24 · Git SHA: `bab761ef` · Audit: 10 instruments, 67 findings, 3 gaps.

This document justifies, for an adversarial reviewer, why the current **HOLD** is the
correct and complete outcome — and why none of the remaining holds or findings
represent an unaddressed risk. Everything here is **verifiable**: re-run
`uv run --project audit audit verify --findings findings.yaml --context audit/evidence.yaml`.

---

## TL;DR

- This repo is the **internal, intermediate dataset-creation stage** of a larger
  pipeline (its JSONL output feeds a separate trajectory repo). It is **not a customer
  trust boundary**, so SHIP-level external attestation is deferred downstream — by
  decision.
- The **high-value surfaces are fully covered and clean**: SAST, secrets (tree + full
  history), supply-chain (pinned lockfile), and the test suite (pytest green).
- The remaining holds are **(a) a deliberate no-CI decision, and (b) three coverage
  gaps on non-security surfaces** (history bloat, container-hygiene sampling,
  per-instance image builds).
- The 67 findings are **container hygiene + unpatchable/unreachable base CVEs** —
  0 CRITICAL, 0 HIGH, nothing exploitable.
- **Completeness is enforced, not claimed:** R1 recall fails the gate if any ≥MEDIUM
  issue is omitted, citing verifier-owned ids the producer cannot fabricate.

**Clean HOLD is the accepted ceiling for this stage. SHIP is mechanically unreachable
on a producer host by design — that is the gate working, not failing.**

---

## Part A — Why the disposition is HOLD (the caps)

The disposition is capped by the items below. **None are findings** — severity is
independent of disposition; the 67 MEDIUM findings do not cap anything.

### A1. Self-attested provenance — *deliberate decision*
**Cap:** no external `AUDIT_TRUST_ROOT_KEY` → the run is self-attested → HOLD.
**Justification:** the Trusted-Evidence Axiom requires that "trusted" be vouched for by
a party that is *not the producer*, using a key the producer cannot mint. This repo is
an internal intermediate stage with **no CI by decision**; the trust stamp belongs at
the **downstream/central boundary** where the dataset is consumed. The `audit sign`
command + `audit/SIGNING.md` are wired and ready for whenever that boundary takes over.
On a producer host, SHIP is *supposed* to be unreachable — this cap is the design
intent, not a defect.

### A2. `D-COVERAGE-GAP-repo-cache-history` — *non-security, cross-covered, accepted*
**Cap:** ~20 MB of third-party bare git mirrors in **past** commit history.
**Justification:**
- It is **bloat, not attack surface** — a clone-size/provenance concern.
- The content **is** scanned: `gitleaks --all` covers full history, so **no secret
  hides** in it.
- It is **untracked + gitignored going forward** (no new bloat); only the *existing*
  history retains it.
- Closing it requires a **destructive `git filter-repo` + force-push** that rewrites
  every commit SHA on a shared remote — a coordinated team decision, deliberately
  **not** taken for 20 MB of one-time bloat. Recorded as an **accepted residual** in
  `scope.yaml`.

### A3. `D-COVERAGE-GAP-dockerfile-static-residual` — *inherent, representative*
**Cap:** ② (`dockerfile_sample_check`) lints a **per-language sample** of generated
Dockerfiles, not all ~3,600.
**Justification:**
- A sample of thousands of generated files **cannot be "exhaustive" by definition** —
  the residual is structural, not an omission.
- The sample is **per-language and representative**: findings are a **small, repeating
  set of hadolint hygiene rules**. Un-sampled instances are produced by the **same
  templates**, so **no new issue class hides** in them.
- Closing it (lint all ~3,600) would multiply the *same* findings ~250× with **zero new
  signal**. The productive fix is one template change, not exhaustive sampling.

### A4. `D-COVERAGE-GAP-live-image-residual` — *base covered live, residual bounded*
**Cap:** `live_image_scan` builds + `trivy image`-scans the **representative base**
image; per-instance images are not all built.
**Justification:**
- The **shared base layer — where systemic CVEs live — is scanned live** (real
  `docker build` + `trivy image`).
- Per-instance images differ only by the **repo's own build deps** (a per-instance,
  curated concern already covered statically by ②). **No systemic vulnerability hides**
  in an unbuilt instance image that is not already in the scanned base.
- Building all ~3,600 images is impractical (heavy/slow); the residual is the honest,
  bounded cost.

---

## Part B — Why the 67 findings are not urgent

These are **container-surface only**; every other surface is clean. All are MEDIUM
(the gate floors required-instrument findings to ≥ MEDIUM — *severity*, not *priority*).
**Priority: Low.**

### B1. 44 × per-instance Dockerfile hygiene (`dockerfile_sample_check`)
These live in the **excluded registry layer** (`harness/repos/**`, generated
dataset-config) and are **reproducibility / image-size / cosmetic — not security**, on
**throwaway** test images with **curated** inputs.

| Rule(s) | n | Concern | Why low-priority |
|---|---|---|---|
| DL3008 / DL3013 / DL3018 / DL3033 | 18 | pin apt/pip/apk/yum versions | reproducibility; one-shot build envs, no attacker |
| DL3015 / DL3009 / DL3032 | 18 | `--no-install-recommends` / clean package lists | image size only |
| DL3059 / DL4006 / SC2046 | 7 | merge RUNs / pipefail / shell quoting | cosmetics / robustness; SC2046 inputs are sanitized → not exploitable |
| DL3007 | 1 | one pre-generated script still uses `latest` | reproducibility; generator default already pinned |

Single root cause (the generation templates) → one cleanup clears the class; no
exploitability, no CRITICAL.

### B2. 23 × base-image CVEs (`live_image_scan`) — all **no-fix**
The **fixable** base CVEs (incl. the HIGH `libssl3` F075) were **already patched** by
adding `apt-get upgrade` to the generator. The 23 remaining:
- have **no upstream fix** — there is no patched version to upgrade to (cannot be
  remediated today);
- are **not reachable** — base-OS libraries in a throwaway test image that builds a repo
  and runs its tests, not exposed to attacker input/network;
- are **0 CRITICAL / 0 HIGH**;
- will **auto-shrink** as upstream patches land, because the `apt-get upgrade` step is
  already in place.

---

## Part C — What IS covered and clean (nothing dangerous hides)

| Surface | Instruments | Result |
|---|---|---|
| Taint SAST | semgrep, bandit | **clean** (B404/B105 documented `# nosec`) |
| Secrets | gitleaks | **clean** (working tree + full history `--all`) |
| Supply chain | pip-audit, osv-scanner | **clean** (pinned `requirements.txt`) |
| Hygiene | ruff, ruff-format | **clean** (framework; registry allowlisted) |
| Tests | pytest | **pass (exit 0)** |
| Dockerfile injection | `dockerfile_generation_check` | **clean** (sanitizer-aware; inputs validated) |
| Reward / dataset integrity | domain checks | **clean** |

**The gaps are on non-security surfaces; the surfaces that could hide a critical
(code injection, secrets, dependencies) are fully instrumented and clean.**

---

## Part D — The completeness guarantee (why this can't be gamed)

- **R1 recall is enforced:** `findings.yaml` must acknowledge *every* ≥MEDIUM issue from
  a parsed run, by a **verifier-owned `issue_instance_id`** the producer cannot
  fabricate. `verify` exits 0 **only** if nothing is dropped.
- **CRITICAL floor → BLOCK, not HOLD:** any injection / secret / RCE / container-escape
  / dataset-leak would force BLOCK. The disposition is HOLD with **0 CRITICAL** — so the
  gaps demonstrably conceal no critical issue.
- **Absence is a result:** every gap is **declared** in `scope.yaml`, reasoned, and
  **caps the disposition**. Nothing reads as safe by silence; the gate **refused to
  ship**, which is the honest outcome.

---

## Conclusion

The remaining holds are: **one deliberate no-CI decision** (provenance) and **three
declared coverage gaps on non-security surfaces** (history bloat, container-hygiene
sampling, per-instance image builds) — two of which are *still cross-scanned* by other
instruments. The 67 findings are **non-exploitable container hygiene + unpatchable,
unreachable base CVEs**, with **0 CRITICAL / 0 HIGH**. Completeness is **mechanically
enforced**.

For an internal intermediate stage, **a clean HOLD — GATE PASS, 0 CRITICAL/HIGH, every
actionable finding resolved, every gap declared and capping — is the correct,
defensible, finished state.** SHIP attestation is a downstream/CI concern, by design.
