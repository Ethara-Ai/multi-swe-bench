"""Unit tests for multi_swe_bench.harness.pull_request (MSB-SELFTEST-007).

The PR/repository dataclasses derive the instance ``id`` used as the join key
across the whole grader (datasets, reports, predictions), so their identity,
ordering, and field validation are load-bearing.
"""

from __future__ import annotations

import pytest

from multi_swe_bench.harness.pull_request import (
    Base,
    PullRequest,
    PullRequestBase,
    Repository,
    ResolvedIssue,
)


# --- Repository -------------------------------------------------------------
def test_repository_name_properties():
    repo = Repository(org="apify", repo="crawlee")
    assert repo.repo_full_name == "apify/crawlee"
    assert repo.repo_file_name == "apify__crawlee"
    assert repr(repo) == "apify/crawlee"


def test_repository_equality_and_hash():
    a = Repository(org="o", repo="r")
    b = Repository(org="o", repo="r")
    c = Repository(org="o", repo="other")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    assert len({a, b, c}) == 2  # a and b collapse


def test_repository_ordering():
    assert Repository(org="a", repo="z") < Repository(org="b", repo="a")
    assert Repository(org="o", repo="a") < Repository(org="o", repo="b")


def test_repository_rejects_non_string_org():
    # validated in __post_init__ via the direct constructor (the dataclass_json
    # schema-based from_dict coerces types and is intentionally not under test)
    with pytest.raises(ValueError, match="Invalid org"):
        Repository(org=123, repo="r")


# --- PullRequestBase: the instance id ---------------------------------------
def test_pull_request_base_id_format():
    prb = PullRequestBase(org="valkey-io", repo="valkey", number=3111)
    assert prb.id == "valkey-io/valkey:pr-3111"
    assert repr(prb) == "valkey-io/valkey:pr-3111"


def test_pull_request_base_ordering_by_number():
    assert PullRequestBase(org="o", repo="r", number=1) < PullRequestBase(
        org="o", repo="r", number=2
    )


def test_pull_request_base_rejects_non_int_number():
    with pytest.raises(ValueError, match="Invalid number"):
        PullRequestBase(org="o", repo="r", number="7")


# --- Base -------------------------------------------------------------------
def test_base_rejects_non_string_sha():
    with pytest.raises(ValueError, match="Invalid sha"):
        Base(label="l", ref="main", sha=1234)


# --- ResolvedIssue ----------------------------------------------------------
def test_resolved_issue_allows_none_body():
    issue = ResolvedIssue(number=5, title="t", body=None)
    assert issue.number == 5 and issue.body is None


# --- PullRequest ------------------------------------------------------------
def _valid_pr(**overrides) -> dict:
    base = {
        "org": "o",
        "repo": "r",
        "number": 1,
        "state": "closed",
        "title": "t",
        "body": None,
        "base": Base(label="l", ref="main", sha="abc"),
        "resolved_issues": [],
        "fix_patch": "",
        "test_patch": "",
    }
    base.update(overrides)
    return base


def test_pull_request_construction_and_id():
    pr = PullRequest(**_valid_pr(org="apify", repo="crawlee", number=3045))
    assert pr.id == "apify/crawlee:pr-3045"
    assert pr.fix_patch == "" and pr.test_patch == ""


def test_pull_request_rejects_non_base_object():
    with pytest.raises(ValueError, match="Invalid base"):
        PullRequest(**_valid_pr(base={"not": "a Base"}))


def test_pull_request_rejects_non_string_fix_patch():
    with pytest.raises(ValueError, match="Invalid fix_patch"):
        PullRequest(**_valid_pr(fix_patch=None))
