"""Offline CVSS v3.1 base-score calculator (R4).

The producer's claimed cvss_base is a form-vs-truth check: parse the full v3.1
vector and recompute the base score with this pinned calculator. A mismatch
(beyond float rounding) is rejected. CWE format + registry membership are
deterministic (Bucket D); CWE *appropriateness* is Bucket N.
"""

from __future__ import annotations

import math
import re

_METRICS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "PR": {"N": 0.85, "L": 0.62, "H": 0.27},  # adjusted when Scope changed below
    "UI": {"N": 0.85, "R": 0.62},
    "S": {"U": "U", "C": "C"},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_PR_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.50}

_VECTOR_RE = re.compile(r"^CVSS:3\.1/(.+)$")
_CWE_RE = re.compile(r"^CWE-\d+$")


class CvssError(ValueError):
    pass


def parse_vector(vector: str) -> dict[str, str]:
    m = _VECTOR_RE.match((vector or "").strip())
    if not m:
        raise CvssError(f"not a CVSS:3.1 vector: {vector!r}")
    parts = {}
    for chunk in m.group(1).split("/"):
        if not chunk:
            continue
        if ":" not in chunk:
            raise CvssError(f"malformed metric: {chunk!r}")
        k, v = chunk.split(":", 1)
        parts[k] = v
    for required in ("AV", "AC", "PR", "UI", "S", "C", "I", "A"):
        if required not in parts:
            raise CvssError(f"missing base metric {required} in {vector!r}")
    return parts


def _roundup(x: float) -> float:
    # CVSS spec Roundup: round to one decimal, ceiling at 1 dp.
    return math.ceil(x * 10) / 10.0


def base_score(vector: str) -> float:
    p = parse_vector(vector)
    scope_changed = p["S"] == "C"

    try:
        av = _METRICS["AV"][p["AV"]]
        ac = _METRICS["AC"][p["AC"]]
        pr = (_PR_CHANGED if scope_changed else _METRICS["PR"])[p["PR"]]
        ui = _METRICS["UI"][p["UI"]]
        c = _METRICS["C"][p["C"]]
        i = _METRICS["I"][p["I"]]
        a = _METRICS["A"][p["A"]]
    except KeyError as e:
        raise CvssError(f"invalid metric value: {e}") from e

    iss = 1 - (1 - c) * (1 - i) * (1 - a)
    if scope_changed:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    exploitability = 8.22 * av * ac * pr * ui

    if impact <= 0:
        return 0.0
    if scope_changed:
        return _roundup(min(1.08 * (impact + exploitability), 10.0))
    return _roundup(min(impact + exploitability, 10.0))


def cwe_is_well_formed(cwe: str | None) -> bool:
    return bool(cwe and _CWE_RE.match(cwe.strip()))


def matches_claim(vector: str, claimed_base: float | None, tol: float = 0.05) -> bool:
    if claimed_base is None:
        return False
    try:
        return abs(base_score(vector) - float(claimed_base)) <= tol
    except CvssError:
        return False
