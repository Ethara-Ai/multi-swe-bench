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
from pathlib import Path

from github import Auth, Github
from tqdm import tqdm

from multi_swe_bench.collect.util import get_tokens


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
        "--filtered_prs_file", type=Path, required=True, help="Path to pull file."
    )

    return parser


def get_github(token) -> Github:
    auth = Auth.Token(token)
    return Github(auth=auth, per_page=100)


def get_top_repositories(g: Github, language=None, limit=10):
    query = "stars:>1"
    if language:
        query += f" language:{language}"

    # Search repositories and sort by stars
    result = g.search_repositories(query=query, sort="stars", order="desc")

    top_repos = []
    for repo in result[:limit]:
        top_repos.append((repo.full_name, repo.stargazers_count))

    return top_repos


def main(tokens, out_dir: Path, filtered_prs_file: Path):
    print("starting get all related issues")
    print(f"Output directory: {out_dir}")
    print(f"Tokens: {len(tokens)} loaded")
    print(f"Pull file: {filtered_prs_file}")

    org_repo_re = re.compile(r"(.+)__(.+?)_(?:lht_)?filtered_prs.jsonl")
    m = org_repo_re.match(filtered_prs_file.name)
    if not m:
        print(f"Error: Invalid pull file name: {filtered_prs_file.name}")
        sys.exit(1)

    org = m.group(1)
    repo = m.group(2)
    print(f"Org: {org}")
    print(f"Repo: {repo}")

    with open(filtered_prs_file, "r", encoding="utf-8") as file:
        filtered_prs = [json.loads(line) for line in file]
        target_issues = set()
        for pr in filtered_prs:
            for issue in pr.get("resolved_issues", []):
                if isinstance(issue, int):
                    target_issues.add(issue)
                elif isinstance(issue, dict):
                    num = issue.get("number")
                    if num is not None:
                        target_issues.add(num)

    if not target_issues:
        print("No resolved issues to fetch. Writing empty file.")
        out_file_path = out_dir / f"{org}__{repo}_related_issues.jsonl"
        out_file_path.write_text("")
        return

    g = get_github(random.choice(tokens))
    r = g.get_repo(f"{org}/{repo}")

    print(f"Fetching {len(target_issues)} specific issues by number...")

    with open(
        out_dir / f"{org}__{repo}_related_issues.jsonl", "w", encoding="utf-8"
    ) as out_file:
        for issue_num in tqdm(sorted(target_issues), desc="Fetching issues"):
            try:
                issue = r.get_issue(issue_num)
                out_file.write(
                    json.dumps(
                        {
                            "org": org,
                            "repo": repo,
                            "number": issue.number,
                            "state": issue.state,
                            "title": issue.title,
                            "body": issue.body,
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                )
            except Exception as e:
                print(f"  Warning: could not fetch issue #{issue_num}: {e}")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    tokens = get_tokens(args.tokens)

    main(tokens, Path.cwd() / args.out_dir, args.filtered_prs_file)
