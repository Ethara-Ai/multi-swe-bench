"""Live container image scan — narrows D-COVERAGE-GAP-dockerfile-static-residual.

② lints generated Dockerfiles statically; this goes one step further: it renders the
generator's representative Dockerfile, `docker build`s it, and `trivy image`-scans the
BUILT image for OS/package CVEs — the live coverage the static sample cannot give.

Fail closed: missing docker/trivy, a build failure, or a scan failure → a coverage
gap (never a silent pass). The built image is removed afterwards. A RESIDUAL gap
remains by nature: this covers the representative/base layer, not an exhaustive build
of every per-instance image (those are heavy/slow and out of scope for the gate).

Returns the domain-check shape — (list[NormalizedIssue], list[CoverageGap]).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from domain import _issue
from models import CoverageGap, Disposition, LocationType, NormalizedIssue
from policy import map_severity
from tools import emit_representative_dockerfile

_GAP_SURFACE = "docker_containers"
_TAG = "audit-livescan:ephemeral"
_BUILD_TIMEOUT = 600
_SCAN_TIMEOUT = 600


def _gap(gid: str, reason: str) -> list[CoverageGap]:
    return [CoverageGap(gid, _GAP_SURFACE, reason, Disposition.HOLD)]


def _docker_build(context: Path) -> tuple[bool, str]:
    """Build the image, retrying once. BuildKit attestation export is disabled (it
    flakes and adds nothing for a scan); returns (ok, error_tail)."""
    env = {**os.environ, "BUILDX_NO_DEFAULT_ATTESTATIONS": "1"}
    cmd = ["docker", "build", "-q", "-t", _TAG, str(context)]
    last = ""
    for _ in range(3):  # Docker Desktop build is occasionally flaky; retry
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=_BUILD_TIMEOUT, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            last = f"launch failed: {e}"
            continue
        if p.returncode == 0:
            return True, ""
        tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        last = tail[-1] if tail else f"exit {p.returncode}"
    return False, last


def _parse_trivy_image(blob: bytes) -> tuple[list[NormalizedIssue], bool]:
    """Parse `trivy image --format json`. Returns (issues, parsed_ok)."""
    try:
        data = json.loads(blob.decode("utf-8", "replace") or "{}")
    except (json.JSONDecodeError, ValueError):
        return [], False
    issues: list[NormalizedIssue] = []
    ordinal = 0
    for res in (data.get("Results", []) if isinstance(data, dict) else []) or []:
        target = res.get("Target", "")
        for v in res.get("Vulnerabilities", []) or []:
            cve = v.get("VulnerabilityID", "CVE-?")
            pkg = v.get("PkgName", "?")
            installed = v.get("InstalledVersion", "")
            fixed = v.get("FixedVersion", "")
            issues.append(
                _issue(
                    check="live_image_scan",
                    rule_id=cve,
                    # trivy is critical-capable; unknown native severity floors MEDIUM.
                    severity=map_severity(v.get("Severity"), from_required_instrument=True),
                    message=(
                        f"trivy image {cve} in {pkg} {installed} "
                        f"({'fix: ' + fixed if fixed else 'no fix'}) [{target}]: "
                        f"{(v.get('Title') or v.get('Description') or '')[:80]}"
                    ),
                    path=None,  # built-image package vuln — no source span
                    subject=f"{cve}:{pkg}",
                    ordinal=ordinal,
                )
            )
            ordinal += 1
    # location_type for these is dependency-class; _issue defaults ARTIFACT, so retag.
    issues = [_retag_dependency(i) for i in issues]
    return issues, True


def _retag_dependency(issue: NormalizedIssue) -> NormalizedIssue:
    # NormalizedIssue is a dataclass; rebuild with dependency location_type so the
    # ignore-allowlist (source/config only) never drops a real CVE.
    object.__setattr__(issue, "location_type", LocationType.DEPENDENCY)
    return issue


def live_image_scan(
    project_root: Path,
) -> tuple[list[NormalizedIssue], list[CoverageGap]]:
    """Build the representative Dockerfile and trivy-image scan it. Fail closed."""
    if shutil.which("docker") is None:
        return [], _gap(
            "D-COVERAGE-GAP-live-image-no-docker",
            "docker not installed — cannot build/scan a live image",
        )
    if shutil.which("trivy") is None:
        return [], _gap(
            "D-COVERAGE-GAP-live-image-no-trivy", "trivy not installed — cannot scan a built image"
        )
    df = emit_representative_dockerfile(project_root)
    if df is None:
        return [], _gap(
            "D-COVERAGE-GAP-live-image-no-dockerfile", "no Dockerfile could be emitted to build"
        )

    out_dir = Path(tempfile.mkdtemp(prefix="audit-livescan-"))
    try:
        (out_dir / "Dockerfile").write_bytes(df)
        built, build_err = _docker_build(out_dir)
        if not built:
            return [], _gap("D-COVERAGE-GAP-live-image-build", f"docker build failed: {build_err}")
        try:
            s = subprocess.run(
                ["trivy", "image", "--quiet", "--scanners", "vuln", "--format", "json", _TAG],
                capture_output=True,
                timeout=_SCAN_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return [], _gap("D-COVERAGE-GAP-live-image-scan", f"trivy image failed to launch: {e}")
        if s.returncode != 0:
            return [], _gap(
                "D-COVERAGE-GAP-live-image-scan", f"trivy image scan failed (exit {s.returncode})"
            )
        issues, parsed_ok = _parse_trivy_image(s.stdout)
        if not parsed_ok:
            return [], _gap(
                "D-COVERAGE-GAP-live-image-scan", "trivy image output unparseable — fail closed"
            )
    finally:
        subprocess.run(["docker", "rmi", "-f", _TAG], capture_output=True, timeout=120)
        shutil.rmtree(out_dir, ignore_errors=True)

    # Live scan succeeded — base/representative image covered. RESIDUAL remains:
    # per-instance images (with their apt/pip installs) are not exhaustively built.
    residual = CoverageGap(
        "D-COVERAGE-GAP-live-image-residual",
        _GAP_SURFACE,
        f"live trivy-image scan covered the representative base image ({len(issues)} CVEs); "
        "per-instance images (full repo installs) are not exhaustively built — caps HOLD",
        Disposition.HOLD,
    )
    return issues, [residual]


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    iss, gp = live_image_scan(here.parent)
    print(f"live-image CVEs: {len(iss)}; gaps: {[g.gap_id for g in gp]}")
