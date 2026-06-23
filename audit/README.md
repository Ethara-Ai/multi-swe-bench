# `audit/` — the CRUCIBLE audit gate for `multi-swe-bench`

Self-contained `uv` project (Python 3.12+) that instruments **this** repository,
emits a committed evidence bundle, and gates producer findings against six
deterministic rules. The **contract** is [`../CRUCIBLE.md`](../CRUCIBLE.md); this
file is the **run playbook** and does not restate the contract.

## Quick start

```bash
# one-shot: provision -> run -> hand-off (per-tool + global wall-clock budget)
uv run --project audit audit all -t 900

# or step by step
uv run --project audit audit provision          # uv sync --extra scanners (+ native scanners out-of-band)
uv run --project audit audit run -t 900         # recon + scanners + domain checks -> evidence.yaml
uv run --project audit audit verify \           # the gate: exits 0 ONLY when all rules hold
    --findings ../findings.yaml --context evidence.yaml
```

`provision`, `run`, and `all` produce **evidence only** — findings are **UNGATED
until `audit verify` exits 0**.

## Commands

| Command | Does | Gate? |
|---------|------|-------|
| `provision` | `uv sync --extra scanners`; notes native scanners (`osv-scanner`, `gitleaks`, `hadolint`, `trivy`) installed out-of-band. Hard-fail. | no |
| `run` | recon + scanner instruments + Bucket-D domain checks + normalization → `evidence.yaml` + `provenance.manifest.yaml` + `results/artifacts/CMD-NNN.stdout.txt.gz`. Enforces the approved `ignore_allowlist`; honours `-t` global budget. | no |
| `verify` | the six rules + provenance pre-checks against `findings.yaml`. Exits `0` **only** when all hold. | **yes — this is the gate** |
| `sign` | detached-signs `provenance.manifest.yaml` with the external `AUDIT_TRUST_ROOT_KEY` → `provenance.manifest.sig`. Run **only** in a trusted env (CI/KMS/approver). Lifts the self-attested provenance cap. See [`SIGNING.md`](SIGNING.md). | no (enables SHIP) |
| `all` | `provision` (unless `--no-install`) → `run` → optional `--verify` → prints Phase-2 hand-off. Never writes findings. | no |

## Module map (flat layout — modules import by bare name)

| Module | Role |
|--------|------|
| `audit.py` | Typer orchestrator (the four commands). |
| `models.py` | Canonical dataclasses, severity/disposition enums, hashing, the CRITICAL floor. |
| `recon.py` | Git/OS/runtime identity + scanner DB pins. |
| `tools.py` | Instrument registry (capability contracts) + the runner (real exit codes, gzipped artifacts, status enum). |
| `policy.py` | Total severity map, surface-token resolution, CRITICAL-floor membership. |
| `normalize.py` | Raw output → `NormalizedIssue` with content-anchored multiset identity (per-tool parsers; fail-closed). |
| `domain.py` | The Bucket-D bespoke checks (reward-bucket, dataset-leakage, report-claim, Dockerfile-gen, reward-provenance). |
| `cvss.py` | Offline CVSS v3.1 base-score calculator (R4). |
| `recall.py` | R1 recall + waiver discipline. |
| `verifier.py` | The six rules + disposition assembly. |
| `provenance.py` | §1.10 content-addressed manifest + the Trusted-Evidence Axiom. |
| `evidence.py` | Emits the committed `evidence.yaml`. |
| `scopelib.py` | Scope loading + the Phase-0.5 sign-off sentinel. |

Every module is listed in `pyproject.toml`'s wheel `include` and is importable
by `pytest` via `conftest.py` (puts `audit/` on `sys.path`).

## Scope sign-off gate (Phase 0.5)

The first action of every command is `load_approved_scope`: recompute
`sha256(scope.yaml)` and compare to `scope.approved`. **Missing or mismatched →
hard exit, nothing written.** Re-scoping after approval re-triggers sign-off.
To approve a (re-)scoped run:

```bash
shasum -a 256 audit/scope.yaml | cut -d' ' -f1 > audit/scope.approved
```

## What is committed vs. ignored

- **Committed** (reviewable in a diff): the harness, `scope.yaml`,
  `scope.approved`, `evidence.yaml`, `provenance.manifest.yaml`.
- **Gitignored** (volatile/machine-specific): `results/` (raw per-command
  transcripts), `.venv/`, `__pycache__/`, `provenance.manifest.sig`.

Reproducibility comes from the **pinned inputs** (git SHA, scanner versions,
pinned DB digests, `scope.yaml` + `scope.approved`) plus the §1.10 signed
artifact-closure manifest — never from stale raw artifacts.

## Tests

```bash
uv run --project audit --extra dev python -m pytest audit/tests -q  # 60 negative-controls + self-fuzz
ruff check . && ruff format --check .
```

See [`DRIFT_LEDGER.md`](DRIFT_LEDGER.md) for the Bucket-D guarantee → conformance
test mapping. A guarantee is `Implemented` **only** with a passing test.
