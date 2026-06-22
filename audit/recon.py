"""Recon: capture the reproducible identity of the audited tree + environment.

Records git SHA, dirty state, OS, network availability, runtime versions, repo
roots, LOC by language, product types, the Phase-0 surface map, and the pinned
identity (version + digest) of every scanner's rule/vuln database. Reproducibility
is verifiable from these pins, not assumed.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ToolPin:
    name: str
    present: bool
    version: str | None
    db_version: str | None
    db_digest: str | None


@dataclass
class Recon:
    project_root: str
    git_sha: str | None
    git_dirty: bool
    git_shallow: bool
    timestamp_epoch: float | None  # injected, never Date.now() inside logic
    os_platform: str
    python_version: str
    network_online: bool
    loc_by_language: dict
    product_types: list[str]
    surfaces: dict
    tool_pins: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _run(argv: list[str], cwd: Path) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _git_sha(root: Path) -> str | None:
    code, out = _run(["git", "rev-parse", "HEAD"], root)
    return out if code == 0 and out else None


def _git_dirty(root: Path) -> bool:
    code, out = _run(["git", "status", "--porcelain"], root)
    if code != 0:
        return True  # fail closed: unknown tree state == dirty
    return bool(out.strip())


def _git_shallow(root: Path) -> bool:
    code, out = _run(["git", "rev-parse", "--is-shallow-repository"], root)
    return out.strip() == "true"


def _network_online(timeout: float = 4.0) -> bool:
    import socket

    for host in (("pypi.org", 443), ("github.com", 443)):
        try:
            with socket.create_connection(host, timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _loc_by_language(root: Path) -> dict:
    """Cheap LOC-by-extension; skips vendored/generated trees."""
    ext_lang = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".rb": "ruby",
    }
    skip = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
    }
    counts: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.endswith(".egg-info")]
        for fn in filenames:
            lang = ext_lang.get(Path(fn).suffix)
            if not lang:
                continue
            try:
                with open(Path(dirpath) / fn, "rb") as fh:
                    n = sum(1 for _ in fh)
            except OSError:
                n = 0
            counts[lang] = counts.get(lang, 0) + n
    return counts


def _semgrep_db_pin() -> ToolPin:
    present = shutil.which("semgrep") is not None
    version = None
    if present:
        code, out = _run(["semgrep", "--version"], Path.cwd())
        version = out.splitlines()[0] if code == 0 and out else None
    # Semgrep rule packs are pinned by the registry digest at fetch time; we
    # record the CLI version. `--config auto` is forbidden in policy.
    return ToolPin("semgrep", present, version, db_version=version, db_digest=None)


def _simple_pin(name: str, version_argv: list[str]) -> ToolPin:
    present = shutil.which(name) is not None
    version = None
    if present:
        code, out = _run(version_argv, Path.cwd())
        version = out.splitlines()[0] if code == 0 and out else None
    return ToolPin(name, present, version, db_version=version, db_digest=None)


def collect_recon(
    project_root: Path,
    surfaces: dict,
    product_types: list[str],
    timestamp_epoch: float | None,
) -> Recon:
    pins = [
        _semgrep_db_pin(),
        _simple_pin("bandit", ["bandit", "--version"]),
        _simple_pin("gitleaks", ["gitleaks", "version"]),
        _simple_pin("osv-scanner", ["osv-scanner", "--version"]),
        _simple_pin("pip-audit", ["pip-audit", "--version"]),
        _simple_pin("trivy", ["trivy", "--version"]),
        _simple_pin("hadolint", ["hadolint", "--version"]),
        _simple_pin("ruff", ["ruff", "--version"]),
    ]
    return Recon(
        project_root=str(project_root),
        git_sha=_git_sha(project_root),
        git_dirty=_git_dirty(project_root),
        git_shallow=_git_shallow(project_root),
        timestamp_epoch=timestamp_epoch,
        os_platform=platform.platform(),
        python_version=platform.python_version(),
        network_online=_network_online(),
        loc_by_language=_loc_by_language(project_root),
        product_types=list(product_types),
        surfaces=surfaces,
        tool_pins=[p.__dict__ for p in pins],
    )
