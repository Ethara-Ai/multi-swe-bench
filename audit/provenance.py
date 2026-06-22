"""Provenance gate (§1.10) — a required Bucket-D atom.

Emits a content-addressed artifact-closure manifest and verifies it BEFORE any
other rule. Trusted-Evidence Axiom: `trusted` is COMPUTED by the verifier, never
copied. SHIP requires a detached signature over the manifest's canonical bytes
verifying against a trust root supplied from OUTSIDE the repo (AUDIT_TRUST_ROOT_KEY)
AND a clean recorded tree AND no source drift. On a producer-controlled host with
no external key, SHIP is mechanically unreachable — by design.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from models import Disposition, sha256_bytes


@dataclass
class ManifestEntry:
    path: str
    realpath: str
    sha256: str
    line_count: int
    type: str


@dataclass
class ProvenanceVerdict:
    cap: Disposition
    trusted: bool
    reasons: list[str]
    source_snapshot: dict  # path -> {sha256, line_count} for R2 TOCTOU closure


def _hash_file(p: Path) -> tuple[str, int]:
    data = p.read_bytes()
    return sha256_bytes(data), data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def build_source_manifest(project_root: Path, source_paths: list[Path]) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for p in source_paths:
        if not p.is_file():
            continue
        digest, lines = _hash_file(p)
        try:
            rel = str(p.relative_to(project_root))
        except ValueError:
            rel = str(p)
        entries.append(
            ManifestEntry(
                path=rel,
                realpath=str(p.resolve()),
                sha256=digest,
                line_count=lines,
                type="source",
            )
        )
    return entries


def build_manifest(
    *,
    project_root: Path,
    audit_root: Path,
    evidence_path: Path,
    run_records: list,
    source_entries: list[ManifestEntry],
    recon: dict,
    policy_version: int,
    scope_digest: str,
    approved_digest: str,
    results_dir: Path,
) -> dict:
    runs = []
    for rec in run_records:
        artifact_digest = None
        if rec.stdout_artifact:
            ap = results_dir / rec.stdout_artifact
            if ap.is_file():
                artifact_digest = sha256_bytes(ap.read_bytes())
        runs.append(
            {
                "run_id": rec.run_id,
                "tool": rec.tool,
                "argv": rec.argv,
                "cwd": rec.cwd,
                "status": rec.status.value,
                "exit_code": rec.exit_code,
                "stdout_artifact": rec.stdout_artifact,
                "stdout_artifact_sha256": artifact_digest,
            }
        )
    manifest = {
        "schema_version": 1,
        "git_sha": recon.get("git_sha"),
        "git_dirty": recon.get("git_dirty"),
        "policy_version": policy_version,
        "scope_digest": scope_digest,
        "approved_scope_digest": approved_digest,
        "evidence_sha256": sha256_bytes(evidence_path.read_bytes())
        if evidence_path.is_file()
        else None,
        "sources": [e.__dict__ for e in source_entries],
        "runs": runs,
    }
    return manifest


def canonical_bytes(manifest: dict) -> bytes:
    return yaml.safe_dump(manifest, sort_keys=True).encode("utf-8")


def write_manifest(manifest: dict, audit_root: Path) -> Path:
    path = audit_root / "provenance.manifest.yaml"
    path.write_bytes(canonical_bytes(manifest))
    return path


# --------------------------------------------------------------------------
# Verification (Phase-3 pre-checks: run BEFORE any other rule).
# --------------------------------------------------------------------------
def _verify_signature(manifest_bytes: bytes, audit_root: Path) -> tuple[bool, str]:
    """Detached signature check against an EXTERNAL trust root.

    The trust root is supplied from outside the audited repo via
    AUDIT_TRUST_ROOT_KEY (e.g. a CI/KMS secret the producer cannot mint). A
    detached signature lives at audit/provenance.manifest.sig. We verify HMAC
    over the canonical bytes — possession of the external key is the gate.
    """
    key = os.environ.get("AUDIT_TRUST_ROOT_KEY")
    if not key:
        return False, "no AUDIT_TRUST_ROOT_KEY in environment (self-attested run)"
    sig_path = audit_root / "provenance.manifest.sig"
    if not sig_path.is_file():
        return False, "no detached signature (provenance.manifest.sig) present"
    expected = hmac_sign(manifest_bytes, key)
    actual = sig_path.read_text(encoding="utf-8").strip()
    if not _consteq(expected, actual):
        return False, "detached signature does not verify against AUDIT_TRUST_ROOT_KEY"
    return True, "signature verified against external trust root"


def hmac_sign(data: bytes, key: str) -> str:
    import hmac

    return hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()


def _consteq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


def _check_command_config_integrity(
    manifest: dict,
    evidence_path: Path,
    results_dir: Path,
    cap: Disposition,
    reasons: list[str],
) -> Disposition:
    """Live runs (evidence) vs manifest snapshot + on-disk artifact digests.

    Total + fail-closed: a run in evidence but absent from the manifest, an
    argv/cwd/status drift, or a stdout artifact whose on-disk bytes no longer hash
    to the snapshot digest all cap BLOCK (argv swap / artifact substitution). A
    missing artifact caps HOLD (cannot re-verify, but not proven tampered).
    """
    ev: dict = {}
    if evidence_path.is_file():
        try:
            ev = yaml.safe_load(evidence_path.read_bytes()) or {}
        except yaml.YAMLError:
            ev = {}
    man_runs = {r.get("run_id"): r for r in manifest.get("runs", [])}
    for er in ev.get("runs", []):
        rid = er.get("run_id")
        mr = man_runs.get(rid)
        if mr is None:
            cap = Disposition.cap(cap, Disposition.BLOCK)
            reasons.append(f"run {rid} in evidence but absent from manifest — capped BLOCK")
            continue
        if (
            er.get("argv") != mr.get("argv")
            or er.get("cwd") != mr.get("cwd")
            or er.get("status") != mr.get("status")
        ):
            cap = Disposition.cap(cap, Disposition.BLOCK)
            reasons.append(f"run {rid} argv/cwd/status drift vs manifest — capped BLOCK")
    for rid, mr in man_runs.items():
        art = mr.get("stdout_artifact")
        dig = mr.get("stdout_artifact_sha256")
        if not (art and dig):
            continue
        ap = results_dir / art
        if not ap.is_file():
            cap = Disposition.cap(cap, Disposition.HOLD)
            reasons.append(f"run {rid} stdout artifact missing on disk — capped HOLD")
        elif sha256_bytes(ap.read_bytes()) != dig:
            cap = Disposition.cap(cap, Disposition.BLOCK)
            reasons.append(f"run {rid} stdout artifact substituted (digest drift) — capped BLOCK")
    return cap


def verify_provenance(
    *,
    audit_root: Path,
    project_root: Path,
    evidence_path: Path,
    scope_digest: str,
    approved_digest: str,
) -> ProvenanceVerdict:
    reasons: list[str] = []
    cap = Disposition.SHIP
    manifest_path = audit_root / "provenance.manifest.yaml"

    if not manifest_path.is_file():
        return ProvenanceVerdict(
            Disposition.HOLD, False, ["provenance manifest missing — capped HOLD"], {}
        )

    raw = manifest_path.read_bytes()
    try:
        manifest = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        return ProvenanceVerdict(
            Disposition.BLOCK, False, [f"manifest unparseable: {e} — capped BLOCK"], {}
        )

    # context integrity: evidence digest must match the manifest snapshot
    if evidence_path.is_file():
        live_ev = sha256_bytes(evidence_path.read_bytes())
        if manifest.get("evidence_sha256") != live_ev:
            cap = Disposition.cap(cap, Disposition.BLOCK)
            reasons.append("evidence.yaml edited after run (digest mismatch) — capped BLOCK")
    else:
        cap = Disposition.cap(cap, Disposition.BLOCK)
        reasons.append("evidence.yaml missing — capped BLOCK")

    # scope integrity
    if manifest.get("scope_digest") != scope_digest or scope_digest != approved_digest:
        cap = Disposition.cap(cap, Disposition.BLOCK)
        reasons.append("scope digest drift vs approved — capped BLOCK")

    # command/config integrity: the live runs (as recorded in evidence) must match
    # the manifest snapshot run-for-run (argv/cwd/status), and each stdout artifact
    # on disk must still hash to the snapshot digest. An argv swap or an artifact
    # substitution is a motivated-liar bypass and caps BLOCK.
    cap = _check_command_config_integrity(
        manifest, evidence_path, audit_root / "results", cap, reasons
    )

    # source integrity (snapshot for R2 TOCTOU closure) + live drift detection
    snapshot: dict = {}
    drift = False
    for entry in manifest.get("sources", []):
        snapshot[entry["path"]] = {
            "sha256": entry["sha256"],
            "line_count": entry["line_count"],
            "realpath": entry["realpath"],
        }
        rp = Path(entry["realpath"])
        if rp.is_file():
            live = sha256_bytes(rp.read_bytes())
            if live != entry["sha256"]:
                drift = True
        else:
            drift = True
    if drift:
        cap = Disposition.cap(cap, Disposition.HOLD)
        reasons.append("source drift since manifest snapshot — SHIP capped")

    # recorded tree cleanliness — fail closed: clean ONLY if git_dirty is
    # explicitly recorded False (a missing/None value is treated as dirty).
    clean_tree = bool(manifest.get("git_sha")) and manifest.get("git_dirty") is False
    if not clean_tree:
        cap = Disposition.cap(cap, Disposition.HOLD)
        reasons.append("recorded tree dirty or git_sha missing — SHIP unreachable (R3-state)")

    # Trusted-Evidence Axiom: computed, never copied
    signed_ok, sig_reason = _verify_signature(raw, audit_root)
    trusted = signed_ok and clean_tree and not drift
    reasons.append(f"trust: {sig_reason}")
    if not trusted:
        cap = Disposition.cap(cap, Disposition.HOLD)
        reasons.append("self-attested (no external signature / unclean / drift) — capped HOLD")

    return ProvenanceVerdict(cap=cap, trusted=trusted, reasons=reasons, source_snapshot=snapshot)
