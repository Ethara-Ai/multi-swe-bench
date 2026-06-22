"""② Generated-Dockerfile sampling — narrows D-COVERAGE-GAP-dockerfile-static.

The repo emits a Dockerfile per instance at runtime (thousands of instance
`dockerfile()` defs under harness/repos/**); hadolint/trivy have no STATIC file to
scan. This module renders a representative SAMPLE of real instance Dockerfiles —
WITH their RUN layers — by driving the project's OWN instance classes through the
project interpreter, writing them to an EPHEMERAL tempdir (outside the scanned tree
so `trivy config .` cannot re-scan them), and lints each with hadolint.

This NARROWS the gap (sampled, not exhaustive). It does NOT fully close it: full
coverage needs a live `docker build` + `trivy image` scan (a dynamic CI-time
instrument, declared separately). When no project interpreter is available the
surface is uncovered → a coverage gap (fail closed), never a silent pass.

Returns the SAME shape as the domain checks — (list[NormalizedIssue],
list[CoverageGap]) — so wiring it into `audit run` is a one-line change.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from domain import _issue
from models import CoverageGap, Disposition, NormalizedIssue
from policy import map_severity
from tools import _resolve_project_python

REPOS_REL = "multi_swe_bench/harness/repos"
_GAP_SURFACE = "docker_containers"
# Bounded sample + bounded render time so the gate never hangs on this surface.
_DEFAULT_N_PER_LANG = 1
_RENDER_TIMEOUT = 120
_HADOLINT_TIMEOUT = 30


def sample_instance_paths(project_root: Path, n_per_lang: int = _DEFAULT_N_PER_LANG) -> list[str]:
    """Pick a representative instance script per language dir (deterministic, pure)."""
    base = project_root / REPOS_REL
    out: list[str] = []
    if not base.is_dir():
        return out
    for lang_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        files = sorted(f for f in lang_dir.rglob("*.py") if f.name != "__init__.py")
        for f in files[:n_per_lang]:
            out.append(str(f.relative_to(project_root)).replace("\\", "/"))
    return out


# Driver executed UNDER the project interpreter: constructs a minimal synthetic
# PullRequest + Config (the repo's own dataclasses) and renders each instance's
# dockerfile(). Best-effort per instance — a failure is reported, never fatal.
_DRIVER = r"""
import importlib, inspect, json, sys, pathlib
root, outdir, rels_json = sys.argv[1], sys.argv[2], sys.argv[3]
rels = json.loads(rels_json)
sys.path.insert(0, root)
from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.pull_request import PullRequest
pr = PullRequest.from_dict({
    "org": "o", "repo": "r", "number": 1, "state": "closed", "title": "t", "body": "",
    "base": {"label": "main", "ref": "main", "sha": "0" * 40},
    "resolved_issues": [], "fix_patch": "--- a\n+++ b\n", "test_patch": "--- a\n+++ b\n",
})
cfg = Config(need_clone=False, global_env=None, clear_env=False)
written, errors = [], []
outp = pathlib.Path(outdir)
outp.mkdir(parents=True, exist_ok=True)
for rel in rels:
    dotted = rel[:-3].replace("/", ".")
    try:
        mod = importlib.import_module(dotted)
        cls = next(v for v in vars(mod).values()
                   if inspect.isclass(v) and issubclass(v, Image) and v is not Image)
        df = cls(pr, cfg).dockerfile()
        if not isinstance(df, str) or "FROM" not in df:
            errors.append({"instance": rel, "error": "no FROM in rendered output"})
            continue
        name = rel.replace("/", "__")[:-3] + ".Dockerfile"
        (outp / name).write_text(df, encoding="utf-8")
        written.append({"instance": rel, "file": str(outp / name)})
    except Exception as e:
        errors.append({"instance": rel, "error": f"{type(e).__name__}: {e}"})
print(json.dumps({"written": written, "errors": errors}))
"""


def render_samples(
    project_python: str, project_root: Path, rels: list[str], out_dir: Path
) -> tuple[list[dict], list[dict], str]:
    """Render the sampled instances' Dockerfiles. Returns (written, errors, fatal)."""
    try:
        p = subprocess.run(
            [project_python, "-c", _DRIVER, str(project_root), str(out_dir), json.dumps(rels)],
            cwd=str(project_root),
            capture_output=True,
            timeout=_RENDER_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return [], [], f"render driver failed to launch: {e}"
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        # The driver always prints a JSON summary, even when every instance errors.
        # Empty stdout means a driver-level crash (import/construction) -> fail closed.
        err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = err[-1] if err else f"exit {p.returncode}"
        return [], [], f"render driver crashed (exit {p.returncode}): {tail}"
    try:
        data = json.loads(out.splitlines()[-1])
    except (json.JSONDecodeError, ValueError, IndexError):
        return [], [], f"render driver produced no parseable output (exit {p.returncode})"
    return data.get("written", []), data.get("errors", []), ""


def _lint_dockerfile(path: Path, ordinal: int) -> list[NormalizedIssue]:
    try:
        p = subprocess.run(
            ["hadolint", "--format", "json", str(path)],
            capture_output=True,
            timeout=_HADOLINT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        return []
    try:
        findings = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []
    issues: list[NormalizedIssue] = []
    for i, f in enumerate(findings if isinstance(findings, list) else []):
        code = f.get("code", "hadolint")
        issues.append(
            _issue(
                check="dockerfile_sample_check",
                rule_id=code,
                # hadolint is critical-capable → floor MEDIUM on unknown levels.
                severity=map_severity(f.get("level"), from_required_instrument=True),
                message=(
                    f"hadolint {code} on generated Dockerfile {path.name} "
                    f"(line {f.get('line')}): {f.get('message', '')}"
                ),
                # Artifact-level: the Dockerfile is GENERATED (no stable source span),
                # so path is None — R2 skips it and the ignore-allowlist (which targets
                # SAST noise under harness/repos/**) does not drop it.
                path=None,
                subject=f"{code}:{path.name}",
                ordinal=ordinal * 100 + i,
            )
        )
    return issues


def dockerfile_sample_check(
    project_root: Path, n_per_lang: int = _DEFAULT_N_PER_LANG
) -> tuple[list[NormalizedIssue], list[CoverageGap]]:
    """Render + hadolint a sample of generated Dockerfiles. Fail closed to a gap."""
    project_python = _resolve_project_python(project_root)
    if project_python is None:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-dockerfile-sample-no-interp",
                _GAP_SURFACE,
                "cannot render generated Dockerfiles for hadolint: no interpreter with "
                "the project deps (set AUDIT_PROJECT_PYTHON). Surface uncovered — fail closed",
                Disposition.HOLD,
            )
        ]
    if shutil.which("hadolint") is None:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-dockerfile-sample-no-hadolint",
                _GAP_SURFACE,
                "hadolint not installed; cannot lint rendered Dockerfiles — fail closed",
                Disposition.HOLD,
            )
        ]
    rels = sample_instance_paths(project_root, n_per_lang)
    if not rels:
        return [], [
            CoverageGap(
                "D-COVERAGE-GAP-dockerfile-sample-empty",
                _GAP_SURFACE,
                f"no instance scripts found under {REPOS_REL} to sample — fail closed",
                Disposition.HOLD,
            )
        ]
    # Render to a tempdir OUTSIDE the scanned project tree, then clean up: the
    # rendered Dockerfiles are intermediate hadolint inputs, not provenance
    # artifacts. Keeping them under the repo would let `trivy config .` re-scan
    # them, double-counting ②'s output and making trivy non-deterministic.
    out_dir = Path(tempfile.mkdtemp(prefix="audit-rendered-dockerfiles-"))
    try:
        written, errors, fatal = render_samples(project_python, project_root, rels, out_dir)
        if fatal:
            return [], [
                CoverageGap(
                    "D-COVERAGE-GAP-dockerfile-sample-render", _GAP_SURFACE, fatal, Disposition.HOLD
                )
            ]
        if not written:
            return [], [
                CoverageGap(
                    "D-COVERAGE-GAP-dockerfile-sample-render",
                    _GAP_SURFACE,
                    f"rendered 0/{len(rels)} sampled Dockerfiles ({len(errors)} errors) — fail closed",
                    Disposition.HOLD,
                )
            ]
        issues: list[NormalizedIssue] = []
        for ordinal, w in enumerate(written):
            issues.extend(_lint_dockerfile(Path(w["file"]), ordinal))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    # Sampling succeeded — findings flow. A RESIDUAL coverage gap remains by nature:
    # the sample is not exhaustive across all instances, and a static hadolint lint
    # is not a live `docker build` + `trivy image` scan of the built image. This is
    # the SINGLE source of truth for the container-static surface (the scope no
    # longer declares D-COVERAGE-GAP-dockerfile-static).
    residual = CoverageGap(
        "D-COVERAGE-GAP-dockerfile-static-residual",
        _GAP_SURFACE,
        f"hadolint linted a {len(written)}/{len(rels)}-language sample of generated "
        f"Dockerfiles ({len(errors)} render errors); RESIDUAL: sample not exhaustive "
        "and no live `docker build`/`trivy image` scan of the built image — caps HOLD",
        Disposition.HOLD,
    )
    return issues, [residual]


if __name__ == "__main__":  # manual: render + lint the sample, print a summary
    here = Path(__file__).resolve().parent
    iss, gp = dockerfile_sample_check(here.parent)
    print(f"rendered-sample hadolint issues: {len(iss)}; coverage gaps: {len(gp)}")
    for g in gp:
        print(f"  gap: {g.gap_id} — {g.reason}")
    for i in iss[:10]:
        print(f"  {i.severity.value} {i.rule_id}: {i.message[:90]}")
