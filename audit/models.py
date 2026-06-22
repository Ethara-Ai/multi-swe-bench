"""Canonical data model for the CRUCIBLE audit gate.

Every other module imports these types. They are deliberately plain dataclasses
with explicit, total semantics — the gate is a deterministic relation over these
structures, so their identity and ordering are load-bearing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# --------------------------------------------------------------------------
# Vocabularies (R6: dispositions and severities are independent, closed sets).
# --------------------------------------------------------------------------
class Severity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEV_RANK[self]

    @classmethod
    def max(cls, sevs: list[Severity]) -> Severity:
        """Total max over a (possibly empty) list; empty -> INFO."""
        best = cls.INFO
        for s in sevs:
            if s.rank > best.rank:
                best = s
        return best


_SEV_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class Disposition(str, Enum):
    SHIP = "SHIP"
    HOLD = "HOLD"
    BLOCK = "BLOCK"

    @property
    def rank(self) -> int:
        # Lower == worse. A "cap" lowers the disposition toward BLOCK.
        return _DISP_RANK[self]

    @classmethod
    def cap(cls, a: Disposition, b: Disposition) -> Disposition:
        """Return the more conservative (lower-ranked) of two dispositions."""
        return a if a.rank <= b.rank else b


_DISP_RANK = {
    Disposition.BLOCK: 0,
    Disposition.HOLD: 1,
    Disposition.SHIP: 2,
}


class RunStatus(str, Enum):
    OK = "ok"
    NONZERO_EXIT = "nonzero_exit"
    TIMEOUT = "timeout"
    TOOL_BLOCKED = "tool_blocked"  # binary missing / could not launch


class EvidenceClass(str, Enum):
    STATIC_REPRODUCIBLE = "static_reproducible"
    DYNAMIC_LIVE = "dynamic_live"
    HEURISTIC = "heuristic"
    DOMAIN_INTEGRITY = "domain_integrity"


class LocationType(str, Enum):
    SOURCE = "source"
    DEPENDENCY = "dependency"
    SECRET = "secret"
    CONFIG = "config"
    ARTIFACT = "artifact"


# --------------------------------------------------------------------------
# Tool capability contract.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    name: str
    category: str
    binary: str
    build_argv: tuple[str, ...]
    ecosystems: tuple[str, ...]
    timeout_sec: int
    required_when: tuple[str, ...]
    critical_capable: bool
    evidence_class: EvidenceClass
    parser_required: bool
    raw_artifact_required: bool
    disposition_cap_on_absent: Disposition  # HOLD or BLOCK
    forbidden_argv: tuple[str, ...] = ()
    # Optional stdin payload provider (project_root -> bytes | None). Used by
    # tools that lint a generated artifact fed on stdin (e.g. hadolint reads the
    # generator-emitted Dockerfile). Returning None means the input could not be
    # produced -> the runner records the tool as blocked (fail closed).
    stdin_provider: Callable[[Path], bytes | None] | None = None


@dataclass
class RunRecord:
    """The provenance of a single tool invocation."""

    run_id: str
    tool: str
    argv: list[str]
    cwd: str
    status: RunStatus
    exit_code: int | None
    started_epoch: float | None
    duration_sec: float | None
    stdout_artifact: str | None  # path under audit/results/artifacts/
    stdout_sha256: str | None
    stdout_excerpt: str
    parsed_ok: bool


@dataclass
class NormalizedIssue:
    tool: str
    rule_id: str
    severity: Severity
    location_type: LocationType
    path: str | None
    line: int | None
    package: str | None
    version: str | None
    vuln_id: str | None
    advisory_id: str | None
    tool_native_fingerprint: str | None
    message: str
    cluster_fingerprint: str
    issue_instance_id: str
    occurrence_ordinal: int = 0


@dataclass
class CoverageGap:
    gap_id: str
    surface: str
    reason: str
    disposition_cap: Disposition


@dataclass
class ToolReport:
    tool: str
    run: RunRecord
    issues: list[NormalizedIssue] = field(default_factory=list)


# --------------------------------------------------------------------------
# Producer-supplied finding (read from findings.yaml). The producer is the
# adversary: every field here is a CLAIM the verifier must check, never trust.
# --------------------------------------------------------------------------
@dataclass
class Finding:
    finding_id: str
    title: str
    severity: Severity
    disposition: Disposition
    issue_instance_ids: list[str]  # which verifier-emitted ids this acknowledges
    path: str | None
    line: int | None
    cwe: str | None
    cvss_vector: str | None
    cvss_base: float | None
    waiver_reason_code: str | None
    rationale: str
    raw: dict


# --------------------------------------------------------------------------
# Deterministic hashing helpers (the identity substrate).
# --------------------------------------------------------------------------
def sha256_hex(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(b"\x1f")  # unit separator so ("a","b") != ("ab",)
        h.update(str(p).encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Hash-bound CRITICAL floor: rule/issue subjects that can NEVER be waived to
# SHIP. Keyed by a stable token the normalizers attach to vuln classes.
CRITICAL_FLOOR_CLASSES = frozenset(
    {
        "injection",
        "command-injection",
        "sql-injection",
        "unsafe-deserialization",
        "ssrf",
        "path-traversal",
        "authz-bypass",
        "secret-exposure",
        "memory-corruption",
        "cve-critical",  # CVSS >= 9 / KEV
        "container-escape",
        "dataset-leakage",
        "agent-writable-reward",
        "rollout-miscount",
    }
)
