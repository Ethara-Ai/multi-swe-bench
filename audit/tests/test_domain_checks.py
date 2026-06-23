"""The bespoke domain-integrity checks catch real dataset/grader defects."""

from __future__ import annotations

import json
from pathlib import Path

from domain import (
    dataset_leakage_check,
    reward_bucket_consistency_check,
    reward_provenance_check,
)
from models import Severity


def _write_jsonl(tmp_path: Path, records) -> Path:
    p = tmp_path / "ds.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def test_reward_miscount_is_critical(tmp_path):
    # a p2p test that regressed at fix -> record is not actually resolved
    rec = {
        "instance_id": "org__repo-1",
        "p2p_tests": {"t_ok": {"run": "PASS", "test": "PASS", "fix": "FAIL"}},
        "f2p_tests": {},
        "s2p_tests": {},
        "n2p_tests": {},
        "fixed_tests": {},
    }
    issues, gaps = reward_bucket_consistency_check(_write_jsonl(tmp_path, [rec]))
    assert any(i.rule_id == "p2p-regression" and i.severity == Severity.CRITICAL for i in issues)


def test_bucket_overlap_is_critical(tmp_path):
    rec = {
        "instance_id": "org__repo-2",
        "p2p_tests": {"shared": {"run": "PASS", "test": "PASS", "fix": "PASS"}},
        "f2p_tests": {"shared": {"run": "NONE", "test": "FAIL", "fix": "PASS"}},
        "s2p_tests": {},
        "n2p_tests": {},
        "fixed_tests": {"shared": {}},
    }
    issues, _ = reward_bucket_consistency_check(_write_jsonl(tmp_path, [rec]))
    assert any(i.rule_id == "bucket-overlap" and i.severity == Severity.CRITICAL for i in issues)


def test_solution_leakage_is_critical(tmp_path):
    solution_lines = "\n".join(
        f"+    let computed_value_{i} = transform(input_{i});" for i in range(20)
    )
    fix_patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n" + solution_lines
    body = "\n".join(f"    let computed_value_{i} = transform(input_{i});" for i in range(20))
    rec = {
        "instance_id": "org__repo-3",
        "title": "bug",
        "body": body,  # the solution pasted into the prompt
        "resolved_issues": [],
        "fix_patch": fix_patch,
        "p2p_tests": {},
        "f2p_tests": {},
        "s2p_tests": {},
        "n2p_tests": {},
        "fixed_tests": {},
    }
    issues, _ = dataset_leakage_check(_write_jsonl(tmp_path, [rec]))
    assert any(
        i.rule_id == "solution-in-prompt" and i.severity == Severity.CRITICAL for i in issues
    )


def test_clean_record_no_critical(tmp_path):
    rec = {
        "instance_id": "org__repo-4",
        "title": "bug",
        "body": "please fix the crash",
        "resolved_issues": [],
        "fix_patch": "diff --git a/x b/x\n+    secret_internal_logic_xyz()\n",
        "p2p_tests": {"a": {"run": "PASS", "test": "PASS", "fix": "PASS"}},
        "f2p_tests": {"b": {"run": "NONE", "test": "FAIL", "fix": "PASS"}},
        "s2p_tests": {},
        "n2p_tests": {},
        "fixed_tests": {"b": {"run": "NONE", "test": "FAIL", "fix": "PASS"}},
    }
    p = _write_jsonl(tmp_path, [rec])
    i1, _ = reward_bucket_consistency_check(p)
    i2, _ = dataset_leakage_check(p)
    assert not [i for i in i1 + i2 if i.severity == Severity.CRITICAL]


def test_no_dataset_is_coverage_gap(tmp_path):
    issues, gaps = reward_bucket_consistency_check(None)
    assert gaps and not issues


def test_dockerfile_unpinned_base_flags_literal_not_prose(tmp_path):
    from domain import dockerfile_generation_check

    root = tmp_path / "proj"
    d = root / "multi_swe_bench" / "utils"
    d.mkdir(parents=True)
    (d / "env_to_dockerfile.py").write_text(
        "def f(base_image):\n"
        '    a = f"FROM ubuntu"\n'  # unpinned literal -> FLAG
        '    b = f"FROM python:3.12"\n'  # tagged -> no flag
        '    c = f"FROM {base_image}"\n'  # parameterized -> no flag
        '    raise ValueError("must contain a FROM instruction")\n',  # prose -> no flag
        encoding="utf-8",
    )
    issues, _ = dockerfile_generation_check(root)
    unpinned = [i for i in issues if i.rule_id == "unpinned-base-image"]
    assert len(unpinned) == 1 and "ubuntu" in unpinned[0].message


def test_dockerfile_interp_excludes_docker_build_args(tmp_path):
    from domain import dockerfile_generation_check

    root = tmp_path / "proj"
    d = root / "multi_swe_bench" / "utils"
    d.mkdir(parents=True)
    (d / "env_to_dockerfile.py").write_text(
        "def f(repo):\n"
        '    a = "RUN git checkout ${BASE_COMMIT}"\n'  # docker build-arg -> NOT flagged
        '    b = f"RUN cd /home/{repo}"\n',  # python interpolation -> flagged
        encoding="utf-8",
    )
    issues, _ = dockerfile_generation_check(root)
    interp = [i for i in issues if i.rule_id == "interpolated-run-line"]
    assert len(interp) == 1 and interp[0].line == 3  # only the {repo} line


def test_bandit_parser_survives_progress_prefix():
    # bandit prints a progress bar to stdout before the JSON; the parser must
    # strip leading non-JSON instead of failing closed on cosmetic output.
    from normalize import _parse_bandit

    blob = (
        b"Working... \xe2\x94\x81\xe2\x94\x81 100% 0:00:21\n"
        b'{"results": [{"test_id": "B602", "issue_severity": "HIGH", '
        b'"filename": "x.py", "line_number": 3, "test_name": "subprocess", '
        b'"issue_text": "shell=True"}]}\n--- STDERR ---\n'
    )
    ok, seeds = _parse_bandit(blob)
    assert ok and len(seeds) == 1 and seeds[0]["rule_id"] == "B602"


def test_reward_provenance_detects_unenforced_guard(tmp_path):
    root = tmp_path / "proj"
    (root / "multi_swe_bench" / "harness").mkdir(parents=True)
    (root / "multi_swe_bench" / "harness" / "test_result.py").write_text(
        "def fix_patch_tampers_with_tests(): pass\n"
    )
    (root / "multi_swe_bench" / "harness" / "run_evaluation.py").write_text(
        "def run_instance(): pass  # never calls the guard\n"
    )
    issues, _ = reward_provenance_check(root)
    assert any(
        i.rule_id == "tamper-guard-not-enforced" and i.severity == Severity.CRITICAL for i in issues
    )


def test_dockerfile_check_skips_sanitized_but_flags_raw(tmp_path):
    """Sanitizer-aware: sanitized interpolation is NOT flagged; a raw value IS."""
    from domain import dockerfile_generation_check

    src = tmp_path / "multi_swe_bench" / "utils" / "env_to_dockerfile.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        "def gen(env, repo, evil):\n"
        "    repo = _safe_path_component(repo)\n"
        "    a = f'RUN clone /home/{repo}'\n"  # sanitized var -> skip
        "    if is_valid_env_name(key):\n"
        "        b = f'ENV {key}=\"{escape_env_value(v)}\"'\n"  # guarded + inline -> skip
        "    c = f'RUN echo {evil}'\n"  # RAW -> must flag
        "    return a, b, c\n",
        encoding="utf-8",
    )
    issues, _ = dockerfile_generation_check(tmp_path)
    flagged = [i for i in issues if i.rule_id == "interpolated-run-line"]
    # exactly the raw {evil} line is flagged; the two sanitized lines are not
    assert len(flagged) == 1
    assert "evil" not in (flagged[0].message or "") or "UNSANITIZED" in flagged[0].message


def test_dockerfile_check_transitive_and_regex_context(tmp_path):
    """A URL built from sanitized parts is skipped; a re.escape() regex context is skipped;
    a URL built from a RAW part is still flagged (fail closed)."""
    from domain import dockerfile_generation_check

    src = tmp_path / "multi_swe_bench" / "utils" / "env_to_dockerfile.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        "def gen(o, r, raw):\n"
        "    org = _safe_path_component(o)\n"
        "    repo = _safe_path_component(r)\n"
        "    url = f'https://github.com/{org}/{repo}.git'\n"  # transitive -> sanitized
        "    a = f'ARG REPO_URL=\"{url}\"'\n"  # uses transitive var -> skip
        "    pat = f'RUN clone {re.escape(repo)}'\n"  # regex context -> skip
        "    bad = f'https://x/{raw}'\n"  # RAW transitive
        "    b = f'ARG U=\"{bad}\"'\n"  # uses raw-built var -> FLAG
        "    return a, pat, b\n",
        encoding="utf-8",
    )
    issues, _ = dockerfile_generation_check(tmp_path)
    flagged_lines = sorted(i.line for i in issues if i.rule_id == "interpolated-run-line")
    assert len(flagged_lines) == 1  # only the raw-built ARG is flagged


def test_live_image_scan_fails_closed_and_parses(tmp_path, monkeypatch):
    """No docker -> coverage gap (fail closed). Trivy json -> dependency-class CVE issues."""
    import imagescan

    monkeypatch.setattr(imagescan.shutil, "which", lambda b: None)  # no docker
    issues, gaps = imagescan.live_image_scan(tmp_path)
    assert issues == [] and gaps and gaps[0].gap_id == "D-COVERAGE-GAP-live-image-no-docker"

    import json as _json

    from models import LocationType

    blob = _json.dumps(
        {
            "Results": [
                {
                    "Target": "img",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-1",
                            "PkgName": "libx",
                            "InstalledVersion": "1.0",
                            "FixedVersion": "1.1",
                            "Severity": "HIGH",
                            "Title": "bad",
                        }
                    ],
                }
            ]
        }
    ).encode()
    parsed, ok = imagescan._parse_trivy_image(blob)
    assert ok and len(parsed) == 1
    assert parsed[0].rule_id == "CVE-2024-1"
    assert parsed[0].severity.value == "HIGH"
    assert parsed[0].location_type == LocationType.DEPENDENCY  # never dropped by allowlist
