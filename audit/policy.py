"""Policy: the hash-bound total severity map, required-instrument resolution,
and the CRITICAL floor.

Every function here is a TOTAL relation: an unknown input fails closed (never to
INFO, never to SHIP). The required-instrument set is read from the APPROVED scope
only — the producer cannot choose it.
"""

from __future__ import annotations

from models import Severity

# --------------------------------------------------------------------------
# Total severity map. Native severities map to the canonical 5. An UNKNOWN
# native severity fails closed to MEDIUM (never INFO). A finding from a
# required (critical-capable) instrument is never mapped below MEDIUM.
# --------------------------------------------------------------------------
_NATIVE_SEVERITY = {
    # generic
    "info": Severity.INFO,
    "informational": Severity.INFO,
    "note": Severity.LOW,
    "low": Severity.LOW,
    "minor": Severity.LOW,
    "medium": Severity.MEDIUM,
    "moderate": Severity.MEDIUM,
    "warning": Severity.MEDIUM,
    "high": Severity.HIGH,
    "important": Severity.HIGH,
    "error": Severity.HIGH,
    "critical": Severity.CRITICAL,
    "blocker": Severity.CRITICAL,
    # sarif levels
    "none": Severity.LOW,
    # bandit
    "undefined": Severity.MEDIUM,
}


def map_severity(native: str | None, *, from_required_instrument: bool) -> Severity:
    """Total severity map. Unknown -> MEDIUM (fail closed)."""
    key = (native or "").strip().lower()
    sev = _NATIVE_SEVERITY.get(key, Severity.MEDIUM)
    if from_required_instrument and sev.rank < Severity.MEDIUM.rank:
        return Severity.MEDIUM
    return sev


# --------------------------------------------------------------------------
# Surface-token resolution: which required_when tokens are "live" for this repo.
# --------------------------------------------------------------------------
def applicable_surface_tokens(scope_raw: dict) -> set[str]:
    tokens: set[str] = {"always"}
    for lang_block in scope_raw.get("ecosystems", []):
        tokens.add(lang_block.get("language", ""))
    surfaces = scope_raw.get("surfaces", {})
    for name, block in surfaces.items():
        if isinstance(block, dict) and block.get("present"):
            tokens.add(name)
    # docker token if container surface present
    if surfaces.get("docker_containers", {}).get("present"):
        tokens.add("docker")
    tokens.discard("")
    return tokens


def required_instrument_ids(scope) -> list[str]:
    """The set the recall gate (R1) enforces — from the APPROVED scope only."""
    return scope.required_instrument_ids


# --------------------------------------------------------------------------
# CRITICAL floor membership: does a rule_id / vuln class fall under the
# un-waivable floor? Normalizers tag a `floor_class` token; this is the gate.
# --------------------------------------------------------------------------
_FLOOR_RULE_SUBSTRINGS = (
    "sql-injection",
    "command-injection",
    "os-command",
    "code-injection",
    "deserial",
    "ssrf",
    "path-traversal",
    "hardcoded",
    "secret",
    "use-after-free",
    "buffer-overflow",
)


def rule_hits_critical_floor(rule_id: str | None, message: str | None) -> bool:
    hay = f"{rule_id or ''} {message or ''}".lower()
    return any(s in hay for s in _FLOOR_RULE_SUBSTRINGS)
