"""Conformance tests for the four newly-wired instruments (CRUCIBLE §1.2).

ruff-format / pytest / hadolint / trivy were declared required in the approved
scope but previously unwired (fail-closed `not-instrumented` coverage gaps). These
tests prove each parser turns real tool output into normalized seeds, that the
hadolint stdin payload comes from the project's OWN generator, and that a stdin
provider returning None fails closed to TOOL_BLOCKED.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from models import Disposition, EvidenceClass, LocationType, RunStatus, Tool
from normalize import (
    _parse_hadolint,
    _parse_pytest,
    _parse_ruff_format,
    _parse_trivy,
)
from tools import _UNRESOLVABLE, emit_representative_dockerfile, run_tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _blob(stdout: str, stderr: str = "") -> bytes:
    return (stdout + "\n--- STDERR ---\n" + stderr).encode("utf-8")


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------
def test_ruff_format_parses_would_reformat_lines():
    ok, seeds = _parse_ruff_format(_blob("Would reformat: a/b.py\nWould reformat: c.py\n"))
    assert ok
    assert {s["path"] for s in seeds} == {"a/b.py", "c.py"}
    assert all(s["rule_id"] == "ruff-format" for s in seeds)


def test_ruff_format_clean_yields_no_seeds():
    ok, seeds = _parse_ruff_format(_blob("3 files already formatted\n"))
    assert ok and seeds == []


def test_pytest_failed_is_medium_error_is_low():
    blob = _blob("FAILED tests/test_x.py::test_a - AssertionError\nERROR tests/test_y.py\n")
    ok, seeds = _parse_pytest(blob)
    assert ok
    by_rule = {s["rule_id"]: s for s in seeds}
    assert by_rule["pytest-failed"]["native_sev"] == "medium"
    assert by_rule["pytest-error"]["native_sev"] == "low"
    assert by_rule["pytest-failed"]["path"] == "tests/test_x.py"


def test_hadolint_parses_json_array():
    out = json.dumps([{"code": "DL3007", "line": 1, "level": "warning", "message": "Using latest"}])
    ok, seeds = _parse_hadolint(_blob(out))
    assert ok and len(seeds) == 1
    assert seeds[0]["rule_id"] == "DL3007"
    assert seeds[0]["native_sev"] == "warning"
    assert seeds[0]["location_type"] == LocationType.ARTIFACT


def test_hadolint_clean_yields_no_seeds():
    ok, seeds = _parse_hadolint(_blob("[]"))
    assert ok and seeds == []


def test_hadolint_unparsable_fails_closed():
    ok, _ = _parse_hadolint(_blob("not json {"))
    assert ok is False


def test_trivy_parses_misconfigurations():
    out = json.dumps(
        {
            "Results": [
                {
                    "Target": "Dockerfile",
                    "Misconfigurations": [
                        {"ID": "DS001", "Severity": "HIGH", "Title": "x", "Description": "y"}
                    ],
                }
            ]
        }
    )
    ok, seeds = _parse_trivy(_blob(out))
    assert ok and len(seeds) == 1
    assert seeds[0]["rule_id"] == "DS001"
    assert seeds[0]["native_sev"] == "HIGH"
    assert seeds[0]["location_type"] == LocationType.CONFIG


# --------------------------------------------------------------------------
# hadolint stdin: emitted from the project's OWN generator
# --------------------------------------------------------------------------
def test_emit_dockerfile_uses_real_generator():
    df = emit_representative_dockerfile(PROJECT_ROOT)
    assert df is not None
    assert b"FROM" in df  # a real Dockerfile from generate_dockerfile()


def test_emit_dockerfile_absent_generator_returns_none(tmp_path):
    # no multi_swe_bench/utils/env_to_dockerfile.py under an empty root -> None
    assert emit_representative_dockerfile(tmp_path) is None


# --------------------------------------------------------------------------
# Fail-closed: a stdin provider that yields None -> TOOL_BLOCKED (never launched)
# --------------------------------------------------------------------------
def test_stdin_provider_none_blocks_tool(tmp_path):
    tool = Tool(
        name="hadolint",
        category="container",
        binary="hadolint",
        build_argv=("hadolint", "--format", "json", "-"),
        ecosystems=("docker",),
        timeout_sec=10,
        required_when=("docker",),
        critical_capable=True,
        evidence_class=EvidenceClass.STATIC_REPRODUCIBLE,
        parser_required=True,
        raw_artifact_required=True,
        disposition_cap_on_absent=Disposition.HOLD,
        stdin_provider=lambda _: None,
    )
    rec = run_tool(tool, tmp_path, tmp_path / "artifacts", 1, time.time)
    assert rec.status == RunStatus.TOOL_BLOCKED


def test_unresolvable_binary_blocks(tmp_path):
    tool = Tool(
        name="pytest",
        category="hygiene",
        binary=_UNRESOLVABLE,
        build_argv=(_UNRESOLVABLE, "-m", "pytest", "-q"),
        ecosystems=("python",),
        timeout_sec=10,
        required_when=("test_suite",),
        critical_capable=False,
        evidence_class=EvidenceClass.DYNAMIC_LIVE,
        parser_required=True,
        raw_artifact_required=True,
        disposition_cap_on_absent=Disposition.HOLD,
    )
    rec = run_tool(tool, tmp_path, tmp_path / "artifacts", 1, time.time)
    assert rec.status == RunStatus.TOOL_BLOCKED
    assert "AUDIT_PROJECT_PYTHON" in (rec.stdout_excerpt or "")
