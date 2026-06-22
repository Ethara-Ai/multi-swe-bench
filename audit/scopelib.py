"""Scope loading + the Phase-0.5 sign-off sentinel.

The approved scope is the only source of the required-instrument set and the
command/config policy. Everything downstream reads it through here so the
hash-binding is enforced in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from models import sha256_bytes


class ScopeNotApproved(RuntimeError):
    """Raised when scope.approved is missing or does not match scope.yaml."""


@dataclass
class ApprovedScope:
    raw: dict
    scope_path: Path
    scope_digest: str
    approved_digest: str

    @property
    def required_instrument_ids(self) -> list[str]:
        return [i["id"] for i in self.raw.get("required_instruments", [])]

    @property
    def required_instruments(self) -> list[dict]:
        return list(self.raw.get("required_instruments", []))

    @property
    def policy_version(self) -> int:
        return int(self.raw.get("policy_version", 0))

    @property
    def command_config_policy(self) -> dict:
        return self.raw.get("command_config_policy", {}) or {}

    @property
    def coverage_gaps(self) -> list[dict]:
        return list(self.raw.get("coverage_gaps", []))

    def approved_argv(self, instrument_id: str) -> list[str] | None:
        for i in self.required_instruments:
            if i["id"] == instrument_id:
                return i.get("approved_argv")
        return None


def audit_dir(start: Path | None = None) -> Path:
    """Locate the audit/ directory (this file's parent)."""
    return Path(__file__).resolve().parent


def load_approved_scope(audit_root: Path | None = None) -> ApprovedScope:
    """Load scope.yaml and enforce the sign-off gate. Raises ScopeNotApproved.

    This is the scaffolder/runner's first gate: recompute sha256(scope.yaml) and
    compare to scope.approved. Missing file or mismatch -> hard fail.
    """
    root = audit_root or audit_dir()
    scope_path = root / "scope.yaml"
    approved_path = root / "scope.approved"

    if not scope_path.is_file():
        raise ScopeNotApproved(f"scope.yaml not found at {scope_path}")
    if not approved_path.is_file():
        raise ScopeNotApproved(f"scope.approved not found at {approved_path}; scope is unsigned")

    scope_bytes = scope_path.read_bytes()
    scope_digest = sha256_bytes(scope_bytes)
    approved_digest = approved_path.read_text(encoding="utf-8").strip()

    if scope_digest != approved_digest:
        raise ScopeNotApproved(
            "scope.approved digest does not match sha256(scope.yaml):\n"
            f"  approved = {approved_digest}\n"
            f"  actual   = {scope_digest}\n"
            "Re-scoping after approval re-triggers sign-off (by design)."
        )

    raw = yaml.safe_load(scope_bytes) or {}
    return ApprovedScope(
        raw=raw,
        scope_path=scope_path,
        scope_digest=scope_digest,
        approved_digest=approved_digest,
    )
