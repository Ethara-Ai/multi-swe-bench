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

"""Pipeline for Long Horizon Task (LHT) data collection.

Orchestrates the full LHT workflow:
  1. get_all_prs        — Fetch all pull requests (reuse existing)
  2. filter_prs (lht)   — Filter to merged PRs (relaxed: no issue requirement)
  3. get_version_tags   — Fetch version tags and commit dates
  4. group_prs_by_tags  — Group PRs into version-tag ranges
  5. get_related_issues — Fetch issues referenced by the grouped PRs
  6. build_lht_dataset  — Build final dataset with unified diffs

Usage:
    python -m multi_swe_bench.collect.get_lht_pipeline \\
        --out_dir ./output --org kubernetes --repo kubernetes \\
        --tokens ghp_xxx
"""

import argparse
from pathlib import Path

from multi_swe_bench.collect.build_lht_dataset import main as build_lht_dataset
from multi_swe_bench.collect.filter_prs import main as filter_prs
from multi_swe_bench.collect.get_all_prs import main as get_all_prs
from multi_swe_bench.collect.get_related_issues import main as get_related_issues
from multi_swe_bench.collect.get_version_tags import main as get_version_tags
from multi_swe_bench.collect.group_prs_by_tags import main as group_prs_by_tags
from multi_swe_bench.collect.util import get_tokens, optional_int


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pipeline for Long Horizon Task (LHT) data collection."
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
        "--delay-on-error",
        type=optional_int,
        default=300,
        help="Delay in seconds before retrying on error. If none, exit on error.",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=3,
        help="Number of attempts to retry on error.",
    )
    parser.add_argument(
        "--max-tags",
        type=int,
        default=200,
        help="Maximum number of tags to fetch (default: 200).",
    )
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
    parser.add_argument(
        "--lang",
        type=str,
        default="python",
        help="Programming language of the repository (default: python).",
    )
    parser.add_argument(
        "--skip-pr-fetch",
        action="store_true",
        default=False,
        help="Skip PR fetch step (use existing PRs file).",
    )
    return parser


def run_pipeline(
    out_dir: Path,
    tokens: list[str],
    org: str,
    repo: str,
    delay_on_error: int = 300,
    retry_attempts: int = 3,
    max_tags: int = 200,
    window_days: int = 30,
    cache_dir: str = ".repo_cache",
    skip_pr_fetch: bool = False,
    lang: str = "python",
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Fetch all pull requests
    if not skip_pr_fetch:
        print("=" * 60)
        print("STEP 1/6: Fetching all pull requests")
        print("=" * 60)
        get_all_prs(tokens, out_dir, org, repo)
    else:
        print("=" * 60)
        print("STEP 1/6: Skipping PR fetch (using existing file)")
        print("=" * 60)

    # Step 2: Filter PRs (LHT mode — accepts all merged PRs)
    print("=" * 60)
    print("STEP 2/6: Filtering PRs (LHT mode)")
    print("=" * 60)
    pull_file = out_dir / f"{org}__{repo}_prs.jsonl"
    filter_prs(tokens, out_dir, pull_file, skip_commit_message=True, mode="lht")

    # Step 3: Fetch version tags
    print("=" * 60)
    print("STEP 3/6: Fetching version tags")
    print("=" * 60)
    get_version_tags(tokens, out_dir, org, repo, max_tags)

    # Step 4: Group PRs by version-tag ranges
    print("=" * 60)
    print("STEP 4/6: Grouping PRs by version tags")
    print("=" * 60)
    group_prs_by_tags(tokens, out_dir, org, repo, window_days, cache_dir)

    # Step 5: Fetch related issues for grouped PRs
    print("=" * 60)
    print("STEP 5/6: Fetching related issues")
    print("=" * 60)
    # Use the LHT-filtered PRs file for issue extraction
    filtered_prs_file = out_dir / f"{org}__{repo}_lht_filtered_prs.jsonl"
    if not filtered_prs_file.exists():
        filtered_prs_file = out_dir / f"{org}__{repo}_filtered_prs.jsonl"
    get_related_issues(tokens, out_dir, filtered_prs_file)

    # Step 6: Build the final LHT dataset
    print("=" * 60)
    print("STEP 6/6: Building LHT dataset")
    print("=" * 60)
    build_lht_dataset(
        tokens,
        out_dir,
        org,
        repo,
        delay_on_error,
        retry_attempts,
        cache_dir,
        lang,
    )

    print("=" * 60)
    print("LHT PIPELINE COMPLETE")
    print(f"Output: {out_dir / f'{org}__{repo}_lht_dataset.jsonl'}")
    print("=" * 60)


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    tokens = get_tokens(args.tokens)

    run_pipeline(
        out_dir=args.out_dir,
        tokens=tokens,
        org=args.org,
        repo=args.repo,
        delay_on_error=args.delay_on_error,
        retry_attempts=args.retry_attempts,
        max_tags=args.max_tags,
        window_days=args.window_days,
        cache_dir=args.cache_dir,
        skip_pr_fetch=args.skip_pr_fetch,
        lang=args.lang,
    )
