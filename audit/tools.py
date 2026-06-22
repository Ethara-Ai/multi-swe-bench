"""Instrument registry + runner.

Each Tool declares a capability contract. The runner executes it, captures the
REAL exit code, writes full stdout/stderr to disk (gzipped), and records a
machine status enum. `nonzero_exit` is NOT an error for most scanners — a tool
that finds issues exits nonzero. Only a missing binary or launch failure is
`tool_blocked`; exceeding the timeout is `timeout`.
"""

from __future__ import annotations

import gzip
import importlib.util
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from models import Disposition, EvidenceClass, RunRecord, RunStatus, Tool, sha256_bytes

# Output volume bound: keep head+tail in the excerpt, gzip the full stream.
_EXCERPT_HEAD = 8000
_EXCERPT_TAIL = 4000

# A sentinel binary name the runner can never resolve on PATH, so a tool whose
# prerequisites are unmet is recorded as TOOL_BLOCKED (fail closed) rather than
# launched against the wrong environment.
_UNRESOLVABLE = "__audit_unresolvable__"


def _resolve_project_python(project_root: Path) -> str | None:
    """Find an interpreter that can import the audited project's deps.

    pytest is a DYNAMIC instrument: it must run in the PROJECT's environment, not
    the isolated audit venv (which deliberately lacks the project's deps). Probe,
    in order: an explicit AUDIT_PROJECT_PYTHON override, a project-root .venv, then
    a VIRTUAL_ENV interpreter. Returns the first whose import of a project sentinel
    succeeds, else None (-> pytest is recorded blocked, with remediation).
    """
    sentinel = "import multi_swe_bench.harness.pull_request"
    cands: list[str] = []
    env_py = os.environ.get("AUDIT_PROJECT_PYTHON")
    if env_py:
        cands.append(env_py)
    for rel in (".venv/bin/python", "venv/bin/python"):
        cands.append(str(project_root / rel))
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        cands.append(str(Path(venv) / "bin" / "python"))
    for c in cands:
        if not c or not Path(c).exists():
            continue
        try:
            r = subprocess.run(
                [c, "-c", sentinel],
                cwd=str(project_root),
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return c
    return None


def emit_representative_dockerfile(project_root: Path) -> bytes | None:
    """Render a Dockerfile from the project's OWN generator, for hadolint stdin.

    Loads `multi_swe_bench/utils/env_to_dockerfile.py` by file path (so the package
    __init__ and its heavy deps are not imported) and calls its real
    `generate_dockerfile`. This lints genuine generator output — notably the
    generator's default unpinned base image. Returns None if the generator source
    is absent or unusable, so hadolint is recorded blocked (the declared
    static-Dockerfile coverage gap stays honest).
    """
    src = project_root / "multi_swe_bench" / "utils" / "env_to_dockerfile.py"
    if not src.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_audit_env_to_dockerfile", src)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        gen = getattr(mod, "generate_dockerfile", None)
        if gen is None:
            return None
        env_vars = [
            ("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            ("DEBIAN_FRONTEND", "noninteractive"),
        ]
        df = gen(env_vars)  # base_image defaults to the generator's own default
        if not isinstance(df, str) or "FROM" not in df:
            return None
        return df.encode("utf-8")
    except Exception:
        return None


# --------------------------------------------------------------------------
# The registry. `required_when` tokens are matched against the scoped surface
# tokens (see policy.applicable_surface_tokens). The argv here is the canonical
# build_argv; policy/provenance bind it to the approved scope argv.
# --------------------------------------------------------------------------
def build_registry(project_root: Path) -> list[Tool]:
    pr = str(project_root)
    project_py = _resolve_project_python(project_root) or _UNRESOLVABLE
    return [
        Tool(
            name="semgrep",
            category="sast_taint",
            binary="semgrep",
            build_argv=(
                "semgrep",
                "--config=p/owasp-top-ten",
                "--config=p/r2c-security-audit",
                "--config=p/secrets",
                "--config=.crucible/semgrep",
                "--metrics=off",
                "--sarif",
                pr,
            ),
            ecosystems=("python",),
            timeout_sec=600,
            required_when=("python",),
            critical_capable=True,
            evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.HOLD,
            forbidden_argv=("--config=auto", "--config", "auto"),
        ),
        Tool(
            name="bandit",
            category="sast_python",
            binary="bandit",
            build_argv=("bandit", "-r", "multi_swe_bench", "-f", "json"),
            ecosystems=("python",),
            timeout_sec=300,
            required_when=("python",),
            critical_capable=True,
            evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.HOLD,
        ),
        Tool(
            name="pip-audit",
            category="supply_chain",
            binary="pip-audit",
            # Audit the PINNED lockfile (reproducible) rather than the installed env.
            build_argv=("pip-audit", "--format", "json", "-r", "requirements.txt"),
            ecosystems=("python",),
            timeout_sec=300,
            required_when=("python",),
            critical_capable=True,
            evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.HOLD,
        ),
        Tool(
            name="osv-scanner",
            category="supply_chain",
            binary="osv-scanner",
            build_argv=("osv-scanner", "--format", "json", "-r", pr),
            ecosystems=("python",),
            timeout_sec=300,
            required_when=("python",),
            critical_capable=True,
            evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.HOLD,
        ),
        Tool(
            name="gitleaks",
            category="secrets",
            binary="gitleaks",
            build_argv=(
                "gitleaks",
                "detect",
                "--config",
                ".gitleaks.toml",
                "--report-format",
                "json",
                "--report-path",
                "-",
                "--log-opts=--all",
            ),
            ecosystems=("any",),
            timeout_sec=300,
            required_when=("always",),
            critical_capable=True,
            evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.BLOCK,
        ),
        Tool(
            name="ruff",
            category="hygiene",
            binary="ruff",
            build_argv=("ruff", "check", "--output-format", "json", pr),
            ecosystems=("python",),
            timeout_sec=120,
            required_when=("python",),
            critical_capable=False,
            evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.HOLD,
        ),
        Tool(
            name="ruff-format",
            category="hygiene",
            binary="ruff",
            build_argv=("ruff", "format", "--check", pr),
            ecosystems=("python",),
            timeout_sec=120,
            required_when=("python",),
            critical_capable=False,
            evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.HOLD,
        ),
        Tool(
            name="pytest",
            category="hygiene",
            # Resolve a project interpreter so the self-tests run in the PROJECT
            # env, not the isolated audit venv. If none is found the binary is
            # unresolvable -> TOOL_BLOCKED with remediation (set AUDIT_PROJECT_PYTHON).
            binary=project_py,
            build_argv=(project_py, "-m", "pytest", "-q"),
            ecosystems=("python",),
            timeout_sec=600,
            required_when=("test_suite",),
            critical_capable=False,
            evidence_class=EvidenceClass.DYNAMIC_LIVE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.HOLD,
        ),
        Tool(
            name="hadolint",
            category="container",
            binary="hadolint",
            # Reads the generator-emitted Dockerfile from stdin (see stdin_provider).
            build_argv=("hadolint", "--format", "json", "-"),
            ecosystems=("docker",),
            timeout_sec=120,
            required_when=("docker",),
            critical_capable=True,
            evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.HOLD,
            stdin_provider=emit_representative_dockerfile,
        ),
        Tool(
            name="trivy",
            category="container",
            binary="trivy",
            build_argv=("trivy", "config", "--format", "json", pr),
            ecosystems=("docker",),
            timeout_sec=300,
            required_when=("docker",),
            critical_capable=True,
            evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
            parser_required=True,
            raw_artifact_required=True,
            disposition_cap_on_absent=Disposition.HOLD,
        ),
    ]


def _split_excerpt(blob: bytes) -> str:
    text = blob.decode("utf-8", "replace")
    if len(text) <= _EXCERPT_HEAD + _EXCERPT_TAIL:
        return text
    return (
        text[:_EXCERPT_HEAD]
        + f"\n...[{len(text) - _EXCERPT_HEAD - _EXCERPT_TAIL} bytes elided]...\n"
        + text[-_EXCERPT_TAIL:]
    )


def run_tool(
    tool: Tool,
    project_root: Path,
    artifacts_dir: Path,
    run_ordinal: int,
    clock: Callable[[], float],
) -> RunRecord:
    """Run one tool, capture provenance. `clock` returns epoch seconds (injected)."""
    run_id = f"CMD-{run_ordinal:03d}"
    argv = list(tool.build_argv)
    cwd = str(project_root)

    # An absolute interpreter/binary path is resolvable if it exists; otherwise
    # fall back to PATH lookup. The sentinel path never resolves (fail closed).
    binary_ok = Path(tool.binary).is_file() or shutil.which(tool.binary) is not None
    if not binary_ok:
        return RunRecord(
            run_id=run_id,
            tool=tool.name,
            argv=argv,
            cwd=cwd,
            status=RunStatus.TOOL_BLOCKED,
            exit_code=None,
            started_epoch=None,
            duration_sec=None,
            stdout_artifact=None,
            stdout_sha256=None,
            stdout_excerpt=(
                "no interpreter with the project deps found — set AUDIT_PROJECT_PYTHON "
                f"to run {tool.name}"
                if tool.binary == _UNRESOLVABLE
                else f"binary not found on PATH: {tool.binary}"
            ),
            parsed_ok=False,
        )

    # Optional stdin payload (e.g. hadolint lints the generator-emitted Dockerfile).
    # A provider that returns None means the input could not be produced -> blocked.
    stdin_payload: bytes | None = None
    if tool.stdin_provider is not None:
        stdin_payload = tool.stdin_provider(project_root)
        if stdin_payload is None:
            return RunRecord(
                run_id=run_id,
                tool=tool.name,
                argv=argv,
                cwd=cwd,
                status=RunStatus.TOOL_BLOCKED,
                exit_code=None,
                started_epoch=None,
                duration_sec=None,
                stdout_artifact=None,
                stdout_sha256=None,
                stdout_excerpt=f"stdin payload could not be produced for {tool.name}",
                parsed_ok=False,
            )

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_rel = f"artifacts/{run_id}.stdout.txt.gz"
    artifact_path = artifacts_dir / f"{run_id}.stdout.txt.gz"

    started = clock()
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=tool.timeout_sec,
            input=stdin_payload,
        )
        duration = clock() - started
        raw = (p.stdout or b"") + b"\n--- STDERR ---\n" + (p.stderr or b"")
        with gzip.open(artifact_path, "wb") as gz:
            gz.write(raw)
        return RunRecord(
            run_id=run_id,
            tool=tool.name,
            argv=argv,
            cwd=cwd,
            status=RunStatus.OK if p.returncode == 0 else RunStatus.NONZERO_EXIT,
            exit_code=p.returncode,
            started_epoch=started,
            duration_sec=duration,
            stdout_artifact=artifact_rel,
            stdout_sha256=sha256_bytes(raw),
            stdout_excerpt=_split_excerpt(p.stdout or b""),
            parsed_ok=False,  # set by normalize step
        )
    except subprocess.TimeoutExpired:
        return RunRecord(
            run_id=run_id,
            tool=tool.name,
            argv=argv,
            cwd=cwd,
            status=RunStatus.TIMEOUT,
            exit_code=None,
            started_epoch=started,
            duration_sec=clock() - started,
            stdout_artifact=None,
            stdout_sha256=None,
            stdout_excerpt=f"timed out after {tool.timeout_sec}s",
            parsed_ok=False,
        )
    except OSError as e:
        return RunRecord(
            run_id=run_id,
            tool=tool.name,
            argv=argv,
            cwd=cwd,
            status=RunStatus.TOOL_BLOCKED,
            exit_code=None,
            started_epoch=started,
            duration_sec=clock() - started,
            stdout_artifact=None,
            stdout_sha256=None,
            stdout_excerpt=f"launch failure: {e}",
            parsed_ok=False,
        )
