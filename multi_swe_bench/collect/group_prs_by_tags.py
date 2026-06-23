# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates

#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at

#      http://www.apache.org/licenses/LICENSE-2.0

#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Group merged PRs into version-tag ranges for Long Horizon Tasks.

Multi-layered strategy:

    Layer 1 — Smart tag parsing (consumed from get_version_tags output):
        Tags arrive pre-classified as semver/calver/unknown with release_line,
        pre_release flags, and semver sort_key.

    Layer 2 — Git ancestry verification:
        Before pairing two tags, verify base is an actual git ancestor of head
        via ``git merge-base --is-ancestor``.  Catches parallel branches being
        incorrectly paired, imported sub-project tags, and rebased tags.

    Layer 3 — Branch-aware release-line grouping:
        - Filter out pre-release tags (unless only pre-releases exist)
        - Group by release line (major.minor)
        - Sort by semver within each group (not date)
        - Only pair consecutive tags that pass ancestry check

    Layer 4 — Tiered PR attribution:
        Priority  Method
        1st       git log --merges --first-parent base..head (no 250-cap)
        2nd       GitHub compare API SHA matching (existing approach)
        3rd       git cherry detection (cherry-picked PRs)
        4th       Date-range fallback (last resort)

    Bundles with fewer than _MIN_PRS_PER_BUNDLE PRs are excluded.

Outputs: {org}__{repo}_tag_groups.jsonl — one record per bundle:
    {
        "base_tag": "v1.0.0",
        "head_tag": "v1.1.0",
        "base_sha": "abc...",
        "head_sha": "def...",
        "pr_numbers": [101, 102, 103],
        "release_line": "1.0",
        "attribution_methods": {"git_log_merge": 2, "compare_api": 1}
    }
"""

import argparse
import json
import random
import re
import subprocess  # nosec B404 - exec routed through safe_subprocess (safe_run); import used for PIPE/exception types only
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from github import Auth, Github, GithubException
from tqdm import tqdm

from multi_swe_bench.collect.util import get_tokens
from multi_swe_bench.utils.safe_subprocess import safe_run


# GitHub compare API hard limit
_COMPARE_COMMITS_CAP = 250

# Default timeout for git commands (seconds)
_GIT_TIMEOUT = 120

# Minimum number of PRs required per version-tag pair to form a valid LHT bundle
_MIN_PRS_PER_BUNDLE = 2

# PR number extraction patterns for merge commit messages
_PR_NUMBER_PATTERNS = [
    re.compile(r"Merge pull request #(\d+)"),
    re.compile(r"\(#(\d+)\)\s*$"),
    re.compile(r"PR #(\d+)"),
    re.compile(r"pull request #(\d+)", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Git operations (Layer 2)
# ---------------------------------------------------------------------------


def _run_git(
    args: list[str],
    repo_path: Path,
    *,
    timeout: int = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given repo path."""
    cmd = ["git", "-C", str(repo_path)] + args
    return safe_run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _is_ancestor(repo_path: Path, ancestor_sha: str, descendant_sha: str) -> bool:
    """Check if ancestor_sha is a git ancestor of descendant_sha."""
    try:
        result = _run_git(
            ["merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
            repo_path,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def _get_merge_commits(
    repo_path: Path,
    base_sha: str,
    head_sha: str,
) -> list[dict]:
    """Get merge commits between two refs using git log --merges --first-parent.

    Returns list of {"sha": ..., "message": ..., "pr_numbers": [...]}.
    """
    try:
        result = _run_git(
            [
                "log",
                "--merges",
                "--first-parent",
                "--format=%H%n%aI%n%s%n---END---",
                f"{base_sha}..{head_sha}",
            ],
            repo_path,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, Exception):
        return []

    commits: list[dict] = []
    raw = result.stdout.strip()
    if not raw:
        return commits

    for entry in raw.split("---END---"):
        entry = entry.strip()
        if not entry:
            continue
        lines = entry.split("\n", 2)
        if len(lines) < 3:
            continue
        sha = lines[0].strip()
        message = lines[2].strip()
        pr_numbers = _extract_pr_numbers(message)
        commits.append({"sha": sha, "message": message, "pr_numbers": pr_numbers})

    return commits


def _get_all_commit_shas(
    repo_path: Path,
    base_sha: str,
    head_sha: str,
) -> set[str]:
    """Get all commit SHAs between two refs (first-parent walk)."""
    try:
        result = _run_git(
            ["log", "--first-parent", "--format=%H", f"{base_sha}..{head_sha}"],
            repo_path,
        )
        if result.returncode != 0:
            return set()
    except (subprocess.TimeoutExpired, Exception):
        return set()

    return {sha.strip() for sha in result.stdout.strip().split("\n") if sha.strip()}


def _detect_cherry_picks(
    repo_path: Path,
    upstream_ref: str,
    head_ref: str,
) -> list[str]:
    """Detect cherry-picked commit SHAs via git cherry."""
    try:
        result = _run_git(
            ["cherry", "-v", upstream_ref, head_ref],
            repo_path,
            timeout=60,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, Exception):
        return []

    cherry_shas: list[str] = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("- "):
            parts = line.split(None, 2)
            if len(parts) >= 2:
                cherry_shas.append(parts[1])
    return cherry_shas


def _extract_pr_numbers(message: str) -> list[int]:
    """Extract PR numbers from a merge commit message."""
    numbers: list[int] = []
    seen: set[int] = set()
    for pattern in _PR_NUMBER_PATTERNS:
        for match in pattern.finditer(message):
            num = int(match.group(1))
            if num not in seen:
                numbers.append(num)
                seen.add(num)
    return numbers


def _ensure_repo_cloned(org: str, repo: str, cache_dir: str) -> Optional[Path]:
    """Ensure the repo is cloned as a bare blobless clone. Returns path or None."""
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    repo_path = cache_path / f"{org}__{repo}.git"

    if repo_path.exists():
        # Fetch latest + tags
        print(f"  Fetching latest for cached {org}/{repo}")
        try:
            safe_run(
                ["git", "-C", str(repo_path), "fetch", "--tags", "--force", "--quiet"],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (
            subprocess.TimeoutExpired,
            subprocess.SubprocessError,
            FileNotFoundError,
        ) as e:
            print(f"  Warning: fetch failed for {org}/{repo}: {e}")
        return repo_path

    print(f"  Cloning {org}/{repo} (bare, blobless)...")
    url = f"https://github.com/{org}/{repo}.git"
    try:
        result = safe_run(
            ["git", "clone", "--bare", "--filter=blob:none", url, str(repo_path)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        print(
            "  Error: git not installed. Ancestry checks and git-log attribution disabled."
        )
        return None
    except subprocess.TimeoutExpired:
        print(f"  Error: clone timed out for {org}/{repo}.")
        return None

    if result.returncode != 0:
        print(f"  Error: clone failed for {org}/{repo}: {result.stderr.strip()}")
        return None

    print(f"  Clone complete for {org}/{repo}")
    return repo_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Group PRs into version-tag ranges for LHT (multi-layered strategy)."
    )
    parser.add_argument(
        "--out_dir", type=Path, required=True, help="Output directory path."
    )
    parser.add_argument(
        "--tokens",
        type=str,
        nargs="*",
        default=None,
        help="API token(s) or path to token file.",
    )
    parser.add_argument("--org", type=str, required=True, help="Organization name.")
    parser.add_argument("--repo", type=str, required=True, help="Repository name.")
    parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="Fallback time-window size in days (default: 30).",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=".repo_cache",
        help="Directory for cached bare git clones (default: .repo_cache).",
    )
    return parser


def get_github(token: str) -> Github:
    auth = Auth.Token(token)
    return Github(auth=auth, per_page=100)


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 date string into a timezone-aware datetime."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Layer 3: Tag grouping — filter pre-releases, group by release line, pair
# ---------------------------------------------------------------------------


def _filter_pre_releases(tags: list[dict]) -> list[dict]:
    """Filter out pre-release tags. Keep all if ONLY pre-releases exist."""
    stable = [t for t in tags if not t.get("is_pre_release", False)]
    if not stable:
        print("  All tags are pre-releases — keeping them all")
        return tags
    filtered_count = len(tags) - len(stable)
    if filtered_count > 0:
        print(f"  Filtered {filtered_count} pre-release tags, {len(stable)} remaining")
    return stable


def _group_tags_by_release_line(tags: list[dict]) -> dict[str, list[dict]]:
    """Group tags by release_line, sorted by sort_key within each group."""
    groups: dict[str, list[dict]] = {}
    for t in tags:
        line = t.get("release_line", "unknown")
        groups.setdefault(line, []).append(t)

    for line in groups:
        groups[line].sort(key=lambda t: t.get("sort_key", []))

    return groups


def _find_cross_line_pairs(
    all_sorted: list[dict],
    existing_pairs: set[tuple[str, str]],
    repo_path: Optional[Path],
) -> list[tuple[dict, dict, str]]:
    """Find tag pairs that bridge release lines (e.g., v1.9.5 → v2.0.0)."""
    cross: list[tuple[dict, dict, str]] = []
    for i in range(len(all_sorted) - 1):
        base = all_sorted[i]
        head = all_sorted[i + 1]
        if base.get("release_line") == head.get("release_line"):
            continue
        key = (base["sha"], head["sha"])
        if key in existing_pairs:
            continue
        if not base["sha"] or not head["sha"] or base["sha"] == head["sha"]:
            continue
        if repo_path and not _is_ancestor(repo_path, base["sha"], head["sha"]):
            continue
        line = f"{base.get('release_line', '?')}->{head.get('release_line', '?')}"
        cross.append((base, head, line))
    return cross


# ---------------------------------------------------------------------------
# Layer 4: Tiered PR attribution
# ---------------------------------------------------------------------------


def _collect_prs_for_pair(
    base_tag: dict,
    head_tag: dict,
    pr_by_sha: dict[str, list[dict]],
    pr_by_number: dict[int, dict],
    all_prs: list[dict],
    assigned_pr_numbers: set[int],
    repo_path: Optional[Path],
    tokens: list[str],
    org: str,
    repo: str,
) -> tuple[list[int], dict[str, int]]:
    """Collect PRs for a tag pair using all 4 tiers. Returns (pr_numbers, method_counts)."""

    base_sha = base_tag["sha"]
    head_sha = head_tag["sha"]
    found: list[int] = []
    methods: dict[str, int] = {}

    def _add(pr_num: int, method: str) -> None:
        if pr_num and pr_num not in assigned_pr_numbers and pr_num not in seen:
            found.append(pr_num)
            seen.add(pr_num)
            methods[method] = methods.get(method, 0) + 1

    seen: set[int] = set()

    # ── Tier 1: git log --merges (most accurate, no 250-cap) ──
    if repo_path:
        merge_commits = _get_merge_commits(repo_path, base_sha, head_sha)
        for mc in merge_commits:
            for pr_num in mc["pr_numbers"]:
                _add(pr_num, "git_log_merge")

        # Also SHA-match known PRs against all commits in range
        all_shas = _get_all_commit_shas(repo_path, base_sha, head_sha)
        for sha in all_shas:
            if sha in pr_by_sha:
                for pr_item in pr_by_sha[sha]:
                    _add(pr_item.get("number", 0), "git_log_sha")

    # ── Tier 2: GitHub compare API SHA matching ──
    use_date_fallback = False
    comparison_shas: set[str] = set()

    g = get_github(random.choice(tokens))  # nosec B311 - non-crypto token load-balancing, not a security context
    r = g.get_repo(f"{org}/{repo}")

    try:
        comparison = r.compare(base_sha, head_sha)
        if comparison.total_commits > _COMPARE_COMMITS_CAP:
            print(
                f"  {base_tag['name']}..{head_tag['name']}: "
                f"{comparison.total_commits} commits (>{_COMPARE_COMMITS_CAP}), "
                f"compare API capped"
            )
            use_date_fallback = True
        else:
            comparison_shas = {c.sha for c in comparison.commits}
    except GithubException as e:
        if len(tokens) > 1:
            try:
                g = get_github(random.choice(tokens))  # nosec B311 - non-crypto token load-balancing, not a security context
                r = g.get_repo(f"{org}/{repo}")
                comparison = r.compare(base_sha, head_sha)
                if comparison.total_commits > _COMPARE_COMMITS_CAP:
                    use_date_fallback = True
                else:
                    comparison_shas = {c.sha for c in comparison.commits}
            except GithubException:
                use_date_fallback = True
        else:
            use_date_fallback = True
        if use_date_fallback:
            print(
                f"  Compare API failed for {base_tag['name']}..{head_tag['name']}: {e}"
            )

    if comparison_shas:
        for sha in comparison_shas:
            if sha in pr_by_sha:
                for pr_item in pr_by_sha[sha]:
                    _add(pr_item.get("number", 0), "compare_api")

    # ── Tier 3: Cherry-pick detection ──
    if repo_path:
        cherry_shas = _detect_cherry_picks(repo_path, base_sha, head_sha)
        for sha in cherry_shas:
            if sha in pr_by_sha:
                for pr_item in pr_by_sha[sha]:
                    _add(pr_item.get("number", 0), "cherry_pick")

    # ── Tier 4: Date-range fallback ──
    # Only use when: (a) we found nothing from tiers 1-3, AND
    # (b) compare API failed or was capped (use_date_fallback=True).
    # Do NOT use if compare succeeded but SHA matching found 0 PRs —
    # that means the tags belong to a different lineage.
    if not found and use_date_fallback:
        base_date = _parse_date(base_tag.get("date", ""))
        head_date = _parse_date(head_tag.get("date", ""))
        if base_date and head_date:
            for pr in all_prs:
                pr_num = pr.get("number", 0)
                if pr_num in assigned_pr_numbers or pr_num in seen:
                    continue
                merged_at = _parse_date(pr.get("merged_at", ""))
                if merged_at and base_date < merged_at <= head_date:
                    _add(pr_num, "date_range")
    elif not found and comparison_shas:
        print(
            f"  {base_tag['name']}..{head_tag['name']}: "
            f"{len(comparison_shas)} commits but 0 matched any PR, "
            f"skipping (likely sub-project tags)"
        )

    return found, methods


def _maybe_emit_group(
    base_tag: dict,
    head_tag: dict,
    release_line: str,
    pr_by_sha: dict[str, list[dict]],
    pr_by_number: dict[int, dict],
    all_prs: list[dict],
    assigned_pr_numbers: set[int],
    repo_path: Optional[Path],
    tokens: list[str],
    org: str,
    repo: str,
    groups: list[dict],
    existing_pairs: set[tuple[str, str]],
) -> None:
    """Collect PRs for a tag pair and append to groups if threshold met."""
    pr_numbers, methods = _collect_prs_for_pair(
        base_tag,
        head_tag,
        pr_by_sha,
        pr_by_number,
        all_prs,
        assigned_pr_numbers,
        repo_path,
        tokens,
        org,
        repo,
    )
    if len(pr_numbers) >= _MIN_PRS_PER_BUNDLE:
        assigned_pr_numbers.update(pr_numbers)
        existing_pairs.add((base_tag["sha"], head_tag["sha"]))
        groups.append(
            {
                "base_tag": base_tag["name"],
                "head_tag": head_tag["name"],
                "base_sha": base_tag["sha"],
                "head_sha": head_tag["sha"],
                "pr_numbers": sorted(pr_numbers),
                "release_line": release_line,
                "attribution_methods": methods,
            }
        )


# ---------------------------------------------------------------------------
# Fallback: time-window grouping (unchanged from original)
# ---------------------------------------------------------------------------


def _group_by_time_window(
    prs: list[dict],
    window_days: int = 30,
) -> list[dict]:
    """Fallback: group PRs by time windows when insufficient tags exist."""
    if not prs:
        return []

    sorted_prs = sorted(
        [p for p in prs if p.get("merged_at")],
        key=lambda p: (
            _parse_date(p.get("merged_at", ""))
            or datetime.min.replace(tzinfo=timezone.utc)
        ),
    )
    if not sorted_prs:
        return []

    by_base: dict[str, list[dict]] = {}
    for pr in sorted_prs:
        base_ref = pr.get("base", {}).get("ref", "main")
        by_base.setdefault(base_ref, []).append(pr)

    groups: list[dict] = []
    for _base_ref, branch_prs in by_base.items():
        current_group: list[dict] = [branch_prs[0]]
        group_start = _parse_date(branch_prs[0].get("merged_at", ""))

        for i in range(1, len(branch_prs)):
            curr_date = _parse_date(branch_prs[i].get("merged_at", ""))
            if (
                group_start
                and curr_date
                and (curr_date - group_start).days <= window_days
            ):
                current_group.append(branch_prs[i])
            else:
                if len(current_group) >= _MIN_PRS_PER_BUNDLE:
                    _emit_time_window_group(groups, current_group)
                current_group = [branch_prs[i]]
                group_start = curr_date

        if len(current_group) >= _MIN_PRS_PER_BUNDLE:
            _emit_time_window_group(groups, current_group)

    return groups


def _emit_time_window_group(groups: list[dict], prs: list[dict]) -> None:
    first_pr = prs[0]
    last_pr = prs[-1]
    first_merged = first_pr.get("merged_at", "")
    last_merged = last_pr.get("merged_at", "")
    groups.append(
        {
            "base_tag": f"window:{first_merged[:10]}" if first_merged else "",
            "head_tag": f"window:{last_merged[:10]}" if last_merged else "",
            "base_sha": first_pr.get("base", {}).get("sha", ""),
            "head_sha": last_pr.get("merge_commit_sha", ""),
            "pr_numbers": sorted(p.get("number", 0) for p in prs),
            "release_line": "time_window",
            "attribution_methods": {"time_window": len(prs)},
        }
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(
    tokens: list[str],
    out_dir: Path,
    org: str,
    repo: str,
    window_days: int = 30,
    cache_dir: str = ".repo_cache",
):
    print("starting group PRs by version tags (multi-layered strategy)")
    print(f"Output directory: {out_dir}")
    print(f"Org: {org}")
    print(f"Repo: {repo}")

    # Load tags (now with parsed scheme, release_line, sort_key, etc.)
    tags_file = out_dir / f"{org}__{repo}_tags.jsonl"
    tags: list[dict] = []
    if tags_file.exists():
        with open(tags_file, "r", encoding="utf-8") as f:
            tags = [json.loads(line) for line in f if line.strip()]
    print(f"Loaded {len(tags)} tags")

    # Load all merged PRs
    prs_file = out_dir / f"{org}__{repo}_lht_filtered_prs.jsonl"
    if not prs_file.exists():
        prs_file = out_dir / f"{org}__{repo}_filtered_prs.jsonl"
    if not prs_file.exists():
        print(f"Error: No PR file found at {prs_file}")
        sys.exit(1)

    prs: list[dict] = []
    with open(prs_file, "r", encoding="utf-8") as f:
        prs = [json.loads(line) for line in f if line.strip()]
    print(f"Loaded {len(prs)} PRs from {prs_file.name}")

    if not prs:
        print("No PRs to group. Exiting.")
        return

    # Build lookups
    pr_by_sha: dict[str, list[dict]] = {}
    pr_by_number: dict[int, dict] = {}
    for pr in prs:
        sha = pr.get("merge_commit_sha", "")
        if sha:
            pr_by_sha.setdefault(sha, []).append(pr)
        num = pr.get("number", 0)
        if num:
            pr_by_number[num] = pr

    # Ensure repo is cloned for git operations (ancestry check, git log)
    repo_path = _ensure_repo_cloned(org, repo, cache_dir)
    if repo_path:
        print(f"  Git repo ready at {repo_path}")
    else:
        print(
            "  Warning: no git clone available. Ancestry checks disabled, "
            "falling back to compare API only."
        )

    groups: list[dict] = []

    if len(tags) >= 2:
        # Layer 3: Filter pre-releases, group by release line
        filtered_tags = _filter_pre_releases(tags)

        if len(filtered_tags) >= 2:
            release_groups = _group_tags_by_release_line(filtered_tags)

            print(f"Release lines: {list(release_groups.keys())}")

            assigned_pr_numbers: set[int] = set()
            existing_pairs: set[tuple[str, str]] = set()

            # Pair consecutive tags within each release line
            for line, line_tags in release_groups.items():
                if len(line_tags) < 2:
                    continue

                for i in tqdm(
                    range(len(line_tags) - 1),
                    desc=f"Pairing tags in {line}",
                    leave=False,
                ):
                    base_tag = line_tags[i]
                    head_tag = line_tags[i + 1]
                    base_sha = base_tag.get("sha", "")
                    head_sha = head_tag.get("sha", "")

                    if not base_sha or not head_sha or base_sha == head_sha:
                        continue

                    # Layer 2: Ancestry verification
                    if repo_path and not _is_ancestor(repo_path, base_sha, head_sha):
                        print(
                            f"  Skipping {base_tag['name']}..{head_tag['name']}: "
                            f"NOT ancestor (parallel branch or imported tag)"
                        )
                        continue

                    # Layer 4: Tiered PR attribution
                    _maybe_emit_group(
                        base_tag,
                        head_tag,
                        line,
                        pr_by_sha,
                        pr_by_number,
                        prs,
                        assigned_pr_numbers,
                        repo_path,
                        tokens,
                        org,
                        repo,
                        groups,
                        existing_pairs,
                    )

            # Cross-release-line pairs (e.g., v1.9.5 → v2.0.0)
            all_sorted = sorted(filtered_tags, key=lambda t: t.get("sort_key", ()))
            cross_pairs = _find_cross_line_pairs(all_sorted, existing_pairs, repo_path)
            for base_tag, head_tag, cross_line in cross_pairs:
                _maybe_emit_group(
                    base_tag,
                    head_tag,
                    cross_line,
                    pr_by_sha,
                    pr_by_number,
                    prs,
                    assigned_pr_numbers,
                    repo_path,
                    tokens,
                    org,
                    repo,
                    groups,
                    existing_pairs,
                )

            print(f"Version-range grouping produced {len(groups)} groups")

    # Fallback to time-window grouping
    if not groups:
        print("No version-range groups; falling back to time-window grouping")
        groups = _group_by_time_window(prs, window_days)
        print(f"Time-window grouping produced {len(groups)} groups")

    # Write output
    out_file = out_dir / f"{org}__{repo}_tag_groups.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for group in groups:
            f.write(json.dumps(group, ensure_ascii=False) + "\n")

    total_prs = sum(len(g["pr_numbers"]) for g in groups)

    # Summary of attribution methods across all groups
    total_methods: dict[str, int] = {}
    for g in groups:
        for method, count in g.get("attribution_methods", {}).items():
            total_methods[method] = total_methods.get(method, 0) + count
    print(f"Attribution methods: {total_methods}")
    print(f"Wrote {len(groups)} groups ({total_prs} total PRs) to {out_file}")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    tokens = get_tokens(args.tokens)
    main(
        tokens,
        Path.cwd() / args.out_dir,
        args.org,
        args.repo,
        args.window_days,
        args.cache_dir,
    )
