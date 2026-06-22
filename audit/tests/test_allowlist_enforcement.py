"""The approved ignore_allowlist drops SAST/hygiene noise but KEEPS secrets+deps.

The allowlist is part of the signed command/config policy: only the exact scoped
paths are excluded, and only for source/config issues. Secret and dependency
findings on the same paths are never dropped (gitleaks coverage is preserved),
and the drop is recorded (never silent).
"""

from __future__ import annotations

from types import SimpleNamespace

from audit import _apply_ignore_allowlist
from models import LocationType, NormalizedIssue, Severity


def _issue(path, loc=LocationType.SOURCE, rule="r"):
    return NormalizedIssue(
        tool="ruff",
        rule_id=rule,
        severity=Severity.LOW,
        location_type=loc,
        path=path,
        line=1,
        package=None,
        version=None,
        vuln_id=None,
        advisory_id=None,
        tool_native_fingerprint=None,
        message="m",
        cluster_fingerprint="c",
        issue_instance_id="i",
    )


def _scope():
    return SimpleNamespace(
        command_config_policy={
            "ignore_allowlist": [
                {"path": "multi_swe_bench/harness/repos/**"},
                {"path": "multi_swe_bench/collect/.repo_cache/**"},
            ]
        }
    )


def test_source_noise_under_allowlist_is_dropped_and_recorded():
    issues = [
        _issue("multi_swe_bench/harness/repos/foo/run.py"),
        _issue("multi_swe_bench/harness/report.py"),  # framework, kept
    ]
    kept, summary = _apply_ignore_allowlist(_scope(), issues)
    kept_paths = [i.path for i in kept]
    assert "multi_swe_bench/harness/report.py" in kept_paths
    assert "multi_swe_bench/harness/repos/foo/run.py" not in kept_paths
    assert summary == {"multi_swe_bench/harness/repos": 1}


def test_secret_and_dependency_under_allowlist_are_kept():
    issues = [
        _issue("multi_swe_bench/harness/repos/x/cfg.py", loc=LocationType.SECRET),
        _issue("multi_swe_bench/harness/repos/x/req.txt", loc=LocationType.DEPENDENCY),
    ]
    kept, summary = _apply_ignore_allowlist(_scope(), issues)
    assert len(kept) == 2  # neither dropped
    assert summary == {}  # nothing recorded as excluded


def test_empty_allowlist_keeps_everything():
    scope = SimpleNamespace(command_config_policy={})
    issues = [_issue("multi_swe_bench/harness/repos/x/run.py")]
    kept, summary = _apply_ignore_allowlist(scope, issues)
    assert len(kept) == 1 and summary == {}
