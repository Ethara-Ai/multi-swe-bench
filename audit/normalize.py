"""Normalize raw tool output into NormalizedIssue with content-anchored identity.

The verifier (not the producer) computes both ids from raw output; the producer
may only reference verifier-emitted ids. Identity is a MULTISET — the same
cluster can occur N times and each occurrence gets a distinct issue_instance_id.

Parsing FAILS CLOSED: a parse error on a required tool sets parsed_ok=False,
which the verifier treats as a coverage gap that caps the disposition.

cluster_fingerprint deliberately EXCLUDES the line number so it survives a
reformat (e.g. `ruff format`):
    sha256(tool, rule_id, subject, path_or_package, message_class_or_vuln_id, snippet)
"""

from __future__ import annotations

import json
import re

from models import (
    LocationType,
    NormalizedIssue,
    Severity,
    ToolReport,
    sha256_hex,
)
from policy import map_severity

_WS = re.compile(r"\s+")
_NUM = re.compile(r"\d+")


def _norm_snippet(s: str | None) -> str:
    if not s:
        return ""
    # collapse whitespace and digits so trivial edits don't fork identity
    return _NUM.sub("0", _WS.sub(" ", s.strip().lower()))[:200]


def _cluster_fp(
    tool: str,
    rule_id: str,
    subject: str,
    path_or_pkg: str,
    message_class_or_vuln: str,
    snippet: str,
) -> str:
    return sha256_hex(
        tool, rule_id, subject, path_or_pkg, message_class_or_vuln, _norm_snippet(snippet)
    )


def _instance_id(
    cluster_fp: str, ordinal: int, source_manifest_digest: str, tool_id: str, rule_id: str
) -> str:
    return sha256_hex(cluster_fp, ordinal, source_manifest_digest, tool_id, rule_id)


def _finalize(
    issues_seed: list[dict],
    *,
    tool: str,
    from_required: bool,
    source_manifest_digest: str,
) -> list[NormalizedIssue]:
    """Assign multiset ordinals + both ids."""
    out: list[NormalizedIssue] = []
    seen: dict[str, int] = {}
    for seed in issues_seed:
        cluster = _cluster_fp(
            tool,
            seed["rule_id"],
            seed.get("subject", ""),
            seed.get("path_or_pkg", ""),
            seed.get("message_class_or_vuln", ""),
            seed.get("snippet", ""),
        )
        ordinal = seen.get(cluster, 0)
        seen[cluster] = ordinal + 1
        iid = _instance_id(cluster, ordinal, source_manifest_digest, tool, seed["rule_id"])
        out.append(
            NormalizedIssue(
                tool=tool,
                rule_id=seed["rule_id"],
                severity=map_severity(
                    seed.get("native_sev"), from_required_instrument=from_required
                ),
                location_type=seed.get("location_type", LocationType.SOURCE),
                path=seed.get("path"),
                line=seed.get("line"),
                package=seed.get("package"),
                version=seed.get("version"),
                vuln_id=seed.get("vuln_id"),
                advisory_id=seed.get("advisory_id"),
                tool_native_fingerprint=seed.get("native_fp"),
                message=seed.get("message", ""),
                cluster_fingerprint=cluster,
                issue_instance_id=iid,
                occurrence_ordinal=ordinal,
            )
        )
    return out


# --------------------------------------------------------------------------
# Per-tool parsers. Each returns (parsed_ok, seeds). A required tool that fails
# to parse -> parsed_ok False -> coverage gap downstream.
# --------------------------------------------------------------------------
def _read_artifact(report: ToolReport, results_dir) -> bytes:
    """Read the full gzipped stdout artifact if present, else the excerpt."""
    import gzip

    rec = report.run
    if rec.stdout_artifact and results_dir is not None:
        p = results_dir / rec.stdout_artifact
        if p.is_file():
            try:
                return gzip.decompress(p.read_bytes())
            except (OSError, gzip.BadGzipFile):
                pass
    return rec.stdout_excerpt.encode("utf-8", "replace")


def _json_payload(blob: bytes) -> bytes:
    """Stdout up to the STDERR marker, trimmed to the first JSON token.

    Some scanners (bandit) print a progress bar to stdout BEFORE the JSON
    document; strip any leading non-JSON so the parse does not fail closed on
    cosmetic output. Returns b"" when no JSON token is present.
    """
    payload = blob.split(b"\n--- STDERR ---\n", 1)[0]
    starts = [i for i in (payload.find(b"{"), payload.find(b"[")) if i != -1]
    if not starts:
        return b""
    return payload[min(starts) :]


def _parse_bandit(blob: bytes) -> tuple[bool, list[dict]]:
    try:
        data = json.loads(_json_payload(blob) or b"{}")
    except (json.JSONDecodeError, ValueError):
        return False, []
    seeds = []
    for r in data.get("results", []):
        seeds.append(
            {
                "rule_id": r.get("test_id", "bandit"),
                "native_sev": r.get("issue_severity"),
                "path": r.get("filename"),
                "line": r.get("line_number"),
                "subject": r.get("test_name", ""),
                "path_or_pkg": r.get("filename", ""),
                "message_class_or_vuln": r.get("test_id", ""),
                "snippet": r.get("code", ""),
                "message": r.get("issue_text", ""),
                "native_fp": r.get("test_id"),
                "location_type": LocationType.SOURCE,
            }
        )
    return True, seeds


def _parse_ruff(blob: bytes) -> tuple[bool, list[dict]]:
    try:
        data = json.loads(blob.split(b"\n--- STDERR ---\n", 1)[0] or b"[]")
    except (json.JSONDecodeError, ValueError):
        return False, []
    seeds = []
    for r in data:
        loc = r.get("location") or {}
        seeds.append(
            {
                "rule_id": r.get("code") or "ruff",
                "native_sev": "low",
                "path": r.get("filename"),
                "line": loc.get("row"),
                "subject": r.get("code", ""),
                "path_or_pkg": r.get("filename", ""),
                "message_class_or_vuln": r.get("code", ""),
                "snippet": r.get("message", ""),
                "message": r.get("message", ""),
                "native_fp": r.get("code"),
                "location_type": LocationType.SOURCE,
            }
        )
    return True, seeds


def _parse_sarif(blob: bytes) -> tuple[bool, list[dict]]:
    try:
        data = json.loads(blob.split(b"\n--- STDERR ---\n", 1)[0] or b"{}")
    except (json.JSONDecodeError, ValueError):
        return False, []
    seeds = []
    for run in data.get("runs", []):
        rules = {}
        driver = run.get("tool", {}).get("driver", {})
        for rule in driver.get("rules", []):
            rules[rule.get("id")] = rule
        for res in run.get("results", []):
            rid = res.get("ruleId", "semgrep")
            level = res.get("level") or rules.get(rid, {}).get("defaultConfiguration", {}).get(
                "level"
            )
            locs = res.get("locations", [])
            path = line = None
            if locs:
                phys = locs[0].get("physicalLocation", {})
                path = phys.get("artifactLocation", {}).get("uri")
                line = phys.get("region", {}).get("startLine")
            msg = (res.get("message", {}) or {}).get("text", "")
            seeds.append(
                {
                    "rule_id": rid,
                    "native_sev": level or "high",
                    "path": path,
                    "line": line,
                    "subject": rid,
                    "path_or_pkg": path or "",
                    "message_class_or_vuln": rid,
                    "snippet": msg,
                    "message": msg,
                    "native_fp": res.get("fingerprints", {}).get("matchBasedId/v1")
                    if isinstance(res.get("fingerprints"), dict)
                    else None,
                    "location_type": LocationType.SOURCE,
                }
            )
    return True, seeds


def _parse_gitleaks(blob: bytes) -> tuple[bool, list[dict]]:
    payload = blob.split(b"\n--- STDERR ---\n", 1)[0].strip()
    if not payload:
        return True, []  # gitleaks prints nothing to stdout when clean (report to path -)
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return False, []
    seeds = []
    for r in data if isinstance(data, list) else []:
        seeds.append(
            {
                "rule_id": r.get("RuleID", "gitleaks"),
                "native_sev": "high",
                "path": r.get("File"),
                "line": r.get("StartLine"),
                "subject": r.get("RuleID", ""),
                "path_or_pkg": r.get("File", ""),
                "message_class_or_vuln": r.get("RuleID", ""),
                "snippet": r.get("Match", ""),
                "message": f"secret: {r.get('Description', r.get('RuleID', ''))}",
                "native_fp": r.get("Fingerprint"),
                "location_type": LocationType.SECRET,
            }
        )
    return True, seeds


def _parse_osv(blob: bytes) -> tuple[bool, list[dict]]:
    payload = blob.split(b"\n--- STDERR ---\n", 1)[0].strip()
    if not payload:
        return True, []
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return False, []
    seeds = []
    for res in data.get("results", []):
        for pkg in res.get("packages", []):
            info = pkg.get("package", {})
            for v in pkg.get("vulnerabilities", []):
                seeds.append(
                    {
                        "rule_id": v.get("id", "osv"),
                        "native_sev": "high",
                        "package": info.get("name"),
                        "version": info.get("version"),
                        "vuln_id": v.get("id"),
                        "subject": info.get("name", ""),
                        "path_or_pkg": info.get("name", ""),
                        "message_class_or_vuln": v.get("id", ""),
                        "snippet": v.get("summary", ""),
                        "message": v.get("summary", v.get("id", "")),
                        "location_type": LocationType.DEPENDENCY,
                    }
                )
    return True, seeds


def _parse_pip_audit(blob: bytes) -> tuple[bool, list[dict]]:
    payload = blob.split(b"\n--- STDERR ---\n", 1)[0].strip()
    if not payload:
        return True, []
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return False, []
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    seeds = []
    for d in deps if isinstance(deps, list) else []:
        for v in d.get("vulns", []):
            seeds.append(
                {
                    "rule_id": v.get("id", "pip-audit"),
                    "native_sev": "high",
                    "package": d.get("name"),
                    "version": d.get("version"),
                    "vuln_id": v.get("id"),
                    "subject": d.get("name", ""),
                    "path_or_pkg": d.get("name", ""),
                    "message_class_or_vuln": v.get("id", ""),
                    "snippet": v.get("description", ""),
                    "message": v.get("description", v.get("id", "")),
                    "location_type": LocationType.DEPENDENCY,
                }
            )
    return True, seeds


_WOULD_REFORMAT = re.compile(rb"^Would reformat:\s*(.+)$", re.MULTILINE)


def _parse_ruff_format(blob: bytes) -> tuple[bool, list[dict]]:
    """`ruff format --check` is text: each drifted file is a `Would reformat:` line."""
    payload = blob.split(b"\n--- STDERR ---\n", 1)[0]
    seeds = []
    for m in _WOULD_REFORMAT.finditer(payload):
        path = m.group(1).decode("utf-8", "replace").strip()
        seeds.append(
            {
                "rule_id": "ruff-format",
                "native_sev": "low",
                "path": path,
                "subject": "format-drift",
                "path_or_pkg": path,
                "message_class_or_vuln": "format-drift",
                "snippet": "would reformat",
                "message": f"{path}: file is not formatted (ruff format --check)",
                "location_type": LocationType.SOURCE,
            }
        )
    return True, seeds


_PYTEST_FAILED = re.compile(rb"^FAILED\s+(\S+)(?:\s+-\s+(.*))?$", re.MULTILINE)
_PYTEST_ERROR = re.compile(rb"^ERROR\s+(\S+)(?:\s+-\s+(.*))?$", re.MULTILINE)


def _parse_pytest(blob: bytes) -> tuple[bool, list[dict]]:
    """Parse pytest -q output: FAILED -> MEDIUM, collection ERROR -> LOW (env signal)."""
    payload = blob.split(b"\n--- STDERR ---\n", 1)[0]
    seeds = []
    for m in _PYTEST_FAILED.finditer(payload):
        node = m.group(1).decode("utf-8", "replace")
        why = (m.group(2) or b"").decode("utf-8", "replace")
        path = node.split("::", 1)[0]
        seeds.append(
            {
                "rule_id": "pytest-failed",
                "native_sev": "medium",
                "path": path,
                "subject": node,
                "path_or_pkg": node,
                "message_class_or_vuln": "test-failure",
                "snippet": why,
                "message": f"pytest FAILED {node}: {why}".strip(),
                "location_type": LocationType.SOURCE,
            }
        )
    for m in _PYTEST_ERROR.finditer(payload):
        node = m.group(1).decode("utf-8", "replace")
        path = node.split("::", 1)[0]
        seeds.append(
            {
                "rule_id": "pytest-error",
                "native_sev": "low",  # collection/usage error — often an env mismatch
                "path": path,
                "subject": node,
                "path_or_pkg": node,
                "message_class_or_vuln": "collection-error",
                "snippet": "collection error",
                "message": f"pytest could not collect {node} (collection/usage error)",
                "location_type": LocationType.SOURCE,
            }
        )
    return True, seeds


def _parse_hadolint(blob: bytes) -> tuple[bool, list[dict]]:
    """hadolint --format json: an array of {file,line,code,level,message}."""
    payload = _json_payload(blob)
    if not payload.strip():
        return True, []  # clean Dockerfile -> hadolint emits []
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return False, []
    seeds = []
    for r in data if isinstance(data, list) else []:
        code = r.get("code", "hadolint")
        seeds.append(
            {
                "rule_id": code,
                "native_sev": r.get("level"),  # error|warning|info|style
                "path": "<generated Dockerfile>",
                "line": r.get("line"),
                "subject": code,
                "path_or_pkg": "generated-dockerfile",
                "message_class_or_vuln": code,
                "snippet": r.get("message", ""),
                "message": f"hadolint {code}: {r.get('message', '')}",
                "native_fp": code,
                "location_type": LocationType.ARTIFACT,
            }
        )
    return True, seeds


def _parse_trivy(blob: bytes) -> tuple[bool, list[dict]]:
    """trivy config --format json: Results[].Misconfigurations[]."""
    payload = _json_payload(blob)
    if not payload.strip():
        return True, []
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return False, []
    seeds = []
    for res in (data.get("Results", []) if isinstance(data, dict) else []) or []:
        target = res.get("Target", "")
        for mc in res.get("Misconfigurations", []) or []:
            cid = mc.get("ID", "trivy")
            seeds.append(
                {
                    "rule_id": cid,
                    "native_sev": mc.get("Severity"),
                    "path": target or None,
                    "line": (mc.get("CauseMetadata", {}) or {}).get("StartLine"),
                    "subject": cid,
                    "path_or_pkg": target,
                    "message_class_or_vuln": cid,
                    "snippet": mc.get("Title", ""),
                    "message": f"trivy {cid}: {mc.get('Title', '')} — {mc.get('Description', '')}",
                    "native_fp": cid,
                    "location_type": LocationType.CONFIG,
                }
            )
    return True, seeds


_PARSERS = {
    "bandit": _parse_bandit,
    "ruff": _parse_ruff,
    "ruff-format": _parse_ruff_format,
    "pytest": _parse_pytest,
    "semgrep": _parse_sarif,
    "gitleaks": _parse_gitleaks,
    "osv-scanner": _parse_osv,
    "pip-audit": _parse_pip_audit,
    "hadolint": _parse_hadolint,
    "trivy": _parse_trivy,
}


def normalize_report(
    report: ToolReport,
    *,
    from_required: bool,
    source_manifest_digest: str,
    results_dir=None,
) -> ToolReport:
    """Parse the raw artifact for `report.tool` and attach normalized issues.

    Mutates and returns the report. Sets report.run.parsed_ok.
    """
    from models import RunStatus

    rec = report.run
    if rec.status in (RunStatus.TOOL_BLOCKED, RunStatus.TIMEOUT):
        rec.parsed_ok = False
        report.issues = []
        return report

    parser = _PARSERS.get(report.tool)
    if parser is None:
        rec.parsed_ok = False
        report.issues = []
        return report

    blob = _read_artifact(report, results_dir)
    ok, seeds = parser(blob)
    rec.parsed_ok = ok
    if not ok:
        report.issues = []
        return report
    report.issues = _finalize(
        seeds,
        tool=report.tool,
        from_required=from_required,
        source_manifest_digest=source_manifest_digest,
    )
    return report


__all__ = ["normalize_report", "Severity"]
