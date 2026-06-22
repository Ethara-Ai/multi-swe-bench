"""Conformance tests for ② generated-Dockerfile sampling (dockerfiles.py).

Proves the pure sampler picks representative instances, the check fails CLOSED to a
coverage gap when no project interpreter is available, and a driver crash (empty
stdout) is reported as fatal rather than swallowed as "0 rendered".
"""

from __future__ import annotations

from pathlib import Path

import dockerfiles
from dockerfiles import (
    REPOS_REL,
    dockerfile_sample_check,
    render_samples,
    sample_instance_paths,
)


def _make_repos(tmp_path: Path) -> Path:
    base = tmp_path / REPOS_REL
    for lang in ("python", "golang", "rust"):
        d = base / lang / "acme"
        d.mkdir(parents=True)
        (d / "__init__.py").write_text("", encoding="utf-8")
        (d / "inst_a.py").write_text("# instance a\n", encoding="utf-8")
        (d / "inst_b.py").write_text("# instance b\n", encoding="utf-8")
    return tmp_path


def test_sampler_picks_one_per_language(tmp_path):
    root = _make_repos(tmp_path)
    rels = sample_instance_paths(root, n_per_lang=1)
    langs = {r.split("/")[3] for r in rels}  # repos/<lang>/...
    assert langs == {"python", "golang", "rust"}
    assert all(not r.endswith("__init__.py") for r in rels)
    assert len(rels) == 3


def test_sampler_empty_when_no_repos(tmp_path):
    assert sample_instance_paths(tmp_path) == []


def test_check_fails_closed_without_interpreter(tmp_path, monkeypatch):
    # no project interpreter -> a coverage gap, never a silent pass
    monkeypatch.setattr(dockerfiles, "_resolve_project_python", lambda _: None)
    issues, gaps = dockerfile_sample_check(tmp_path)
    assert issues == []
    assert len(gaps) == 1
    assert gaps[0].gap_id == "D-COVERAGE-GAP-dockerfile-sample-no-interp"
    assert "AUDIT_PROJECT_PYTHON" in gaps[0].reason


def test_render_driver_crash_is_fatal(tmp_path, monkeypatch):
    # a project interpreter that crashes with empty stdout must be FATAL, not "0 rendered"
    monkeypatch.setattr(dockerfiles, "_resolve_project_python", lambda _: "/bin/false")
    monkeypatch.setattr(dockerfiles.shutil, "which", lambda _: "/usr/bin/hadolint")
    _make_repos(tmp_path)
    issues, gaps = dockerfile_sample_check(tmp_path)
    assert issues == []
    assert any(g.gap_id == "D-COVERAGE-GAP-dockerfile-sample-render" for g in gaps)


def test_success_emits_findings_plus_residual_gap(tmp_path, monkeypatch):
    # when sampling succeeds, findings flow AND a single residual gap remains (HOLD)
    monkeypatch.setattr(dockerfiles, "_resolve_project_python", lambda _: "/usr/bin/python")
    monkeypatch.setattr(dockerfiles.shutil, "which", lambda _: "/usr/bin/hadolint")
    monkeypatch.setattr(
        dockerfiles,
        "render_samples",
        lambda *_a, **_k: ([{"instance": "a/b/c/i.py", "file": "/tmp/x.Dockerfile"}], [], ""),
    )
    from models import Severity

    monkeypatch.setattr(
        dockerfiles,
        "_lint_dockerfile",
        lambda *_a, **_k: [
            dockerfiles._issue(
                check="dockerfile_sample_check",
                rule_id="DL3008",
                severity=Severity.MEDIUM,
                message="pin apt",
                path=None,
            )
        ],
    )
    _make_repos(tmp_path)
    issues, gaps = dockerfile_sample_check(tmp_path)
    assert len(issues) == 1 and issues[0].rule_id == "DL3008"
    assert [g.gap_id for g in gaps] == ["D-COVERAGE-GAP-dockerfile-static-residual"]
    assert gaps[0].disposition_cap.value == "HOLD"


def test_render_samples_reports_fatal_on_empty_stdout(tmp_path):
    # /bin/false exits nonzero with no stdout -> fatal string, not silent empties
    written, errors, fatal = render_samples(
        "/bin/false", tmp_path, ["x/y/z/inst.py"], tmp_path / "out"
    )
    assert written == [] and errors == []
    assert fatal  # non-empty fatal reason
