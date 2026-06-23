# Trusted signing — making the run externally attested (lifts provenance cap #1)

The audit caps at **HOLD** on a producer-controlled host because the run is
*self-attested* (Trusted-Evidence Axiom). To make the verifier compute
`trusted = true` you need a **detached signature over the provenance manifest, made
with a key the producer cannot read**, plus a clean tree and no source drift.

`trusted = signature_verifies AND clean_tree AND no_source_drift`

This repo ships the `audit sign` step. What it does **not** ship — by design — is the
key: that must come from a trusted environment outside the producer's control.

## The flow (run → sign → verify)

```bash
# 1. instrument (any environment)
uv run --project audit audit run -t 900

# 2. SIGN — ONLY in the trusted environment that holds the secret
export AUDIT_TRUST_ROOT_KEY="$AUDIT_TRUST_ROOT_KEY"     # from CI secret / KMS / vault
uv run --project audit audit sign                       # writes audit/provenance.manifest.sig

# 3. verify — with the same key in env
uv run --project audit audit verify \
    --findings findings.yaml --context audit/evidence.yaml
# -> note: provenance: trust: signature verified against external trust root
```

`audit sign` HMACs the exact bytes `verify` reads (`audit/provenance.manifest.yaml`)
and writes the detached hex signature to `audit/provenance.manifest.sig` (gitignored —
it is a per-run artifact, ship it to an append-only store if you want an immutable
trail).

## The one rule that makes it meaningful

**The signer must not be the producer.** HMAC is symmetric — whoever holds
`AUDIT_TRUST_ROOT_KEY` can both sign and verify, so the key's secrecy *is* the gate.
Signing on a laptop where the engineer/agent can read the key proves nothing. Put the
key + the `sign` step in a place the producer cannot reach:

- a **central / downstream pipeline** (e.g. the trajectory repo that ingests this
  dataset) — the natural trust boundary for an intermediate stage like this one;
- a **shared org CI runner** (not a per-repo workflow);
- a **human approver** with a vault key, signing out-of-band.

Generate the key once and store it as a secret: `openssl rand -hex 32`.

## Generic CI snippet (adapt to your runner — not necessarily GitHub Actions)

```yaml
# runs in a TRUSTED runner; AUDIT_TRUST_ROOT_KEY is a protected secret the
# producer cannot read. Asymmetric KMS signing is stronger (see below).
steps:
  - checkout            # clean tree, real git SHA
  - run: uv run --project audit audit run -t 900
  - run: uv run --project audit audit sign          # env: AUDIT_TRUST_ROOT_KEY
    env: { AUDIT_TRUST_ROOT_KEY: ${SECRET_FROM_VAULT} }
  - run: uv run --project audit audit verify --findings findings.yaml --context audit/evidence.yaml
    env: { AUDIT_TRUST_ROOT_KEY: ${SECRET_FROM_VAULT} }
```

## What signing alone does NOT do

`audit sign` clears **only** the signature factor. `trusted` also needs a **clean,
committed tree** (no dirty bit) and **no source drift**. And the overall disposition
reaches **SHIP** only if there are **no capping coverage gaps** either — so on this
repo you would additionally have to *close* (not just accept) `repo-cache-history`
(history rewrite) and `dockerfile-static-residual` (live `docker build` + `trivy image`
scan), and confirm pinned scanner DBs. Drop any one and it stays HOLD.

## Stronger variant (optional)

HMAC's trust root is a shared secret. For true key-custody separation, swap
`_verify_signature` / `hmac_sign` for **asymmetric** signing (e.g. ed25519): a private
key signs inside KMS, and the verifier holds only the **public** trust root. That is a
small change to `provenance.py` + a conformance test; ask if you want it.
