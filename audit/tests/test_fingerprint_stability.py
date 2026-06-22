"""cluster_fingerprint must survive a reformat (line shifts + whitespace churn)."""

from __future__ import annotations

import json

from models import RunRecord, RunStatus, ToolReport
from normalize import normalize_report


def _bandit_blob(line, indent):
    return json.dumps(
        {
            "results": [
                {
                    "test_id": "B602",
                    "test_name": "subprocess_popen_with_shell_equals_true",
                    "issue_severity": "HIGH",
                    "filename": "m/x.py",
                    "line_number": line,
                    "code": f"{indent}subprocess.run(cmd, shell=True)",
                    "issue_text": "shell=True is dangerous",
                }
            ]
        }
    ).encode()


def _report(blob):
    rec = RunRecord(
        run_id="CMD-001",
        tool="bandit",
        argv=["bandit"],
        cwd=".",
        status=RunStatus.NONZERO_EXIT,
        exit_code=1,
        started_epoch=0.0,
        duration_sec=0.0,
        stdout_artifact=None,
        stdout_sha256=None,
        stdout_excerpt=blob.decode(),
        parsed_ok=False,
    )
    r = ToolReport(tool="bandit", run=rec)
    normalize_report(r, from_required=True, source_manifest_digest="d", results_dir=None)
    return r


def test_cluster_fingerprint_survives_reformat():
    before = _report(_bandit_blob(line=10, indent="    "))
    after = _report(_bandit_blob(line=42, indent="\t\t"))  # ruff format moved it + retabbed
    assert before.issues and after.issues
    assert before.issues[0].cluster_fingerprint == after.issues[0].cluster_fingerprint
    # but the instance ids stay stable too here (same ordinal/manifest)
    assert before.issues[0].line != after.issues[0].line


def test_distinct_rules_distinct_fingerprints():
    a = _report(_bandit_blob(line=1, indent=""))
    b = json.dumps(
        {
            "results": [
                {
                    "test_id": "B105",
                    "test_name": "hardcoded_password",
                    "issue_severity": "LOW",
                    "filename": "m/x.py",
                    "line_number": 1,
                    "code": "p='x'",
                    "issue_text": "pw",
                }
            ]
        }
    ).encode()
    rb = _report(b)
    assert a.issues[0].cluster_fingerprint != rb.issues[0].cluster_fingerprint
