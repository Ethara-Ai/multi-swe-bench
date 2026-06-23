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

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from tqdm import tqdm
from unidiff import PatchSet

from multi_swe_bench.collect.util import get_tokens, optional_int


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A command-line tool for processing repositories."
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
    parser.add_argument(
        "--filtered-prs-with-issues-file",
        type=Path,
        required=True,
        help="Filtered PRs with issues file.",
    )
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

    return parser


def extract_patches(pull: dict, token: str) -> tuple[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    patch = requests.get(pull["diff_url"], headers=headers, timeout=30).text
    test_patch = ""
    fix_patch = ""
    if (
        "exceeded a secondary rate limit" in patch
        or "Access to this site has been restricted" in patch
    ):
        print("GitHub API rate limit exceeded or Network error.")
        raise Exception(
            "GitHub API rate limit exceeded. Please wait before making additional requests, or check your network connection."
        )

    for hunk in PatchSet(patch):
        if any(
            test_word in hunk.path for test_word in ["test", "tests", "e2e", "testing"]
        ):
            test_patch += str(hunk)
        else:
            fix_patch += str(hunk)
    return fix_patch, test_patch


def extract_patches_from_compare(pull: dict, token: str) -> tuple[str, str]:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.diff",
    }

    org = pull.get("org")
    repo = pull.get("repo")
    base_sha = pull.get("base", {}).get("sha")
    commits = pull.get("commits", [])
    test_patch = ""
    fix_patch = ""

    if not all([org, repo, base_sha]) or not commits:
        return fix_patch, test_patch

    head_sha = commits[-1].get("sha")
    if not head_sha:
        raise ValueError("Missing head SHA in last commit")

    compare_url = (
        f"https://api.github.com/repos/{org}/{repo}/compare/{base_sha}...{head_sha}"
    )
    response = requests.get(compare_url, headers=headers, timeout=30)
    if response.status_code != 200:
        raise Exception(
            f"Failed to fetch patch: {response.status_code} - {response.text[:300]}"
        )

    patch = response.text

    for hunk in PatchSet(patch):
        if any(
            test_word in hunk.path.lower()
            for test_word in ["test", "tests", "e2e", "testing"]
        ):
            test_patch += str(hunk)
        else:
            fix_patch += str(hunk)
    return fix_patch, test_patch


def get_failed_number(log_file: Path) -> Optional[int]:
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as file:
            n = file.read().strip()
            if n != "":
                return int(n)
    return None


def set_failed_number(log_file: Path, failed_number: int):
    with open(log_file, "w", encoding="utf-8") as file:
        file.write(f"{failed_number}")


def main(
    tokens: list[str],
    out_dir: Path,
    filtered_prs_with_issues_file: Path,
    delay_on_error: int,
    retry_attempts: int,
):
    print("starting build complete dataset")
    print(f"Tokens: {len(tokens)} loaded")
    print(f"Output directory: {out_dir}")
    print(f"Dataset file: {filtered_prs_with_issues_file}")
    print(f"Delay on error: {delay_on_error}")
    print(f"Retry attempts: {retry_attempts}")

    org_repo_re = re.compile(r"(.+)__(.+)_filtered_prs_with_issues.jsonl")
    m = org_repo_re.match(filtered_prs_with_issues_file.name)
    if not m:
        print(f"Error: Invalid pull file name: {filtered_prs_with_issues_file.name}")
        sys.exit(1)

    org = m.group(1)
    repo = m.group(2)
    print(f"Org: {org}")
    print(f"Repo: {repo}")

    with open(filtered_prs_with_issues_file, "r", encoding="utf-8") as file:
        filtered_prs_with_issues = [json.loads(line) for line in file]

    try:
        with open(
            out_dir / f"{org}__{repo}_raw_dataset.jsonl", "r", encoding="utf-8"
        ) as file:
            raw_dataset = {}
            for line in file:
                data = json.loads(line)
                raw_dataset[data["number"]] = data
    except FileNotFoundError:
        raw_dataset = {}

    failed_number = max([pr["number"] for pr in filtered_prs_with_issues])

    log_file = out_dir / f"{org}__{repo}_raw_dataset.log"
    last_failed_number = get_failed_number(log_file)
    if last_failed_number is not None:
        failed_number = last_failed_number

    processed_number = min(raw_dataset.keys()) if raw_dataset else failed_number
    print(f"minimum processed number: {processed_number}")

    with open(
        out_dir / f"{org}__{repo}_raw_dataset.jsonl", "a", encoding="utf-8"
    ) as file:
        for pr in tqdm(filtered_prs_with_issues, desc="Building Dataset"):
            if (
                pr["number"] in raw_dataset
                or pr["number"] >= processed_number
                or pr["number"] > failed_number
            ):
                continue

            for attempt in range(retry_attempts):
                try:
                    fix_patch, test_patch = extract_patches_from_compare(
                        pr,
                        random.choice(tokens),  # nosec B311 - non-crypto token load-balancing, not a security context
                    )
                    pr["fix_patch"] = fix_patch
                    pr["test_patch"] = test_patch

                    if fix_patch == "" or test_patch == "":
                        break

                    file.write(json.dumps(pr, ensure_ascii=False) + "\n")
                    break
                except Exception as e:
                    set_failed_number(log_file, pr["number"])
                    # Permanent errors (e.g. 404 "no common ancestor") won't
                    # resolve with retries — skip this PR and continue.
                    error_msg = str(e)
                    is_permanent = any(
                        marker in error_msg
                        for marker in [
                            "404",
                            "No common ancestor",
                            "422",
                            "Not Found",
                        ]
                    )
                    if is_permanent:
                        print(
                            f"\nSkipping PR #{pr['number']}: permanent error — {error_msg}"
                        )
                        break
                    if delay_on_error is None or attempt == retry_attempts - 1:
                        raise e
                    else:
                        print(
                            f"The {attempt + 1} attempt failed. Sleeping then retrying..."
                        )
                        for i in tqdm(range(delay_on_error), desc="Sleeping"):
                            time.sleep(1)


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    tokens = get_tokens(args.tokens)

    main(
        tokens,
        args.out_dir,
        args.filtered_prs_with_issues_file,
        args.delay_on_error,
        args.retry_attempts,
    )
