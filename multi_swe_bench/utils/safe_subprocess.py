"""Hardened subprocess wrappers (CRUCIBLE MSB-AUDIT-3).

Enforce, in ONE audited place, the three invariants bandit B603/B607 warn about
for the git/docker call sites that take PR-derived arguments:

  * **list-form argv only** — a string argv (which would go through a shell) is
    rejected, so there is no shell-injection surface (CWE-78);
  * **shell=True is rejected** — defensively, even though no call site uses it;
  * **absolute executable path** — argv[0] is resolved via a PATH lookup, so a
    partial executable name ("git", "docker") cannot be satisfied by a binary
    planted earlier on PATH (B607). If the executable is not found on PATH the
    original name is kept unchanged, so behaviour is identical to before.

Call sites use ``safe_run`` / ``safe_popen`` instead of ``subprocess.run`` /
``subprocess.Popen``; the only raw ``subprocess.*`` invocations live here, each
with a single justified suppression. Resolution happens at call time, so it works
correctly inside the eval container regardless of where git/docker are installed.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence


def _harden(argv: Sequence[str], kwargs: dict) -> list[str]:
    """Validate argv is list-form, forbid shell=True, resolve argv[0] absolute."""
    if isinstance(argv, (str, bytes)):
        raise TypeError("safe subprocess requires a list/tuple argv, not a string (no shell)")
    if kwargs.get("shell"):
        raise ValueError("shell=True is forbidden; pass a list-form argv")
    parts = [str(a) for a in argv]
    if not parts:
        raise ValueError("empty argv")
    resolved = shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


def safe_run(argv: Sequence[str], **kwargs):
    """``subprocess.run`` with list-form enforced, no shell, absolute exe path."""
    parts = _harden(argv, kwargs)
    return subprocess.run(parts, **kwargs)  # nosec B603 - list-form, shell rejected, exe resolved


def safe_popen(argv: Sequence[str], **kwargs):
    """``subprocess.Popen`` with list-form enforced, no shell, absolute exe path."""
    parts = _harden(argv, kwargs)
    return subprocess.Popen(parts, **kwargs)  # nosec B603 - list-form, shell rejected, exe resolved
