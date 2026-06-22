"""Self-fuzz: the verifier never crashes and never returns OK on an invariant
violation. Uses Hypothesis when available; otherwise a deterministic fallback.
"""

from __future__ import annotations

import pytest
from helpers import build_audit, run_verify, write_findings

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HAVE_HYPOTHESIS = True
except ImportError:  # pragma: no cover
    HAVE_HYPOTHESIS = False


def _fuzz_once(tmp_path, disposition, n_findings, bogus_path):
    audit_root, project_root, _, issues = build_audit(tmp_path)
    findings = []
    for i in range(n_findings):
        findings.append(
            {
                "id": f"F{i}",
                "title": "t",
                "severity": "MEDIUM",
                "disposition": disposition,
                "issue_instance_ids": ["deadbeef" * 8],  # fabricated
                "path": bogus_path,
                "line": 99999,
                "rationale": "x",
            }
        )
    fp = write_findings(audit_root, findings, disposition=disposition)
    res = run_verify(audit_root, project_root, fp)
    # The omitted real MEDIUM issue + fabricated ids + bad spans guarantee a
    # violation; the verifier must NEVER report ok here, and must not crash.
    assert not res.ok
    return res


if HAVE_HYPOTHESIS:

    @settings(max_examples=40, deadline=None)
    @given(
        disposition=st.sampled_from(["SHIP", "HOLD", "BLOCK", "bogus"]),
        n_findings=st.integers(min_value=1, max_value=5),
        bogus_path=st.sampled_from(["../x", "/etc/passwd", "ghost.py", "a/b.py"]),
    )
    def test_fuzz_never_ok_on_violation(tmp_path_factory, disposition, n_findings, bogus_path):
        tmp_path = tmp_path_factory.mktemp("fuzz")
        _fuzz_once(tmp_path, disposition, n_findings, bogus_path)

else:  # pragma: no cover

    @pytest.mark.parametrize("disposition", ["SHIP", "HOLD", "BLOCK", "bogus"])
    @pytest.mark.parametrize("bogus_path", ["../x", "/etc/passwd", "ghost.py"])
    def test_fuzz_never_ok_on_violation(tmp_path, disposition, bogus_path):
        _fuzz_once(tmp_path, disposition, 2, bogus_path)


def test_verifier_does_not_crash_on_empty(tmp_path):
    audit_root, project_root, _, _ = build_audit(tmp_path, issues=[])
    fp = write_findings(audit_root, [])
    res = run_verify(audit_root, project_root, fp)
    assert res is not None
