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

"""Fetch version tags with smart parsing & classification.

Outputs a JSONL file with one record per tag:
    {
        "name": "v1.2.3",
        "sha": "abc123...",
        "date": "2024-01-15T10:30:00Z",
        "scheme": "semver",
        "major": 1, "minor": 2, "patch": 3,
        "pre_release": null,
        "release_line": "1.2",
        "is_pre_release": false,
        "sort_key": [1, 2, 3, [1, ""]]
    }

Tags are sorted by semver order (not date) within release lines.
Pre-release tags are kept but flagged for downstream filtering.

Supported schemes:
    - semver: v1.2.3, v2.0.0-rc.1, v1.0.0-alpha.3
    - calver: 2024.01, 2024.01.15, 24.1
    - unknown: anything else (best-effort pre-release detection)
"""

import argparse
import json
import random
import re
from pathlib import Path

from github import Auth, Github, GithubException
from packaging.version import Version as PkgVersion, InvalidVersion
from tqdm import tqdm

from multi_swe_bench.collect.util import get_tokens


# ---------------------------------------------------------------------------
# Semver / Calver regex patterns
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z\-]+(?:\.[0-9A-Za-z\-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z\-]+(?:\.[0-9A-Za-z\-]+)*))?$"
)

_CALVER_RE = re.compile(
    r"^v?(?P<year>20\d{2}|[2-9]\d)\.(?P<month>0?[1-9]|1[0-2])"
    r"(?:\.(?P<day>0?[1-9]|[12]\d|3[01]))?"
    r"(?:\.(?P<micro>\d+))?"
    r"(?:-(?P<pre>[0-9A-Za-z\-]+(?:\.[0-9A-Za-z\-]+)*))?$"
)

_PREFIX_RE = re.compile(
    r"^(?:release|hotfix|rel|version|ver)[/\-](.+)$",
    re.IGNORECASE,
)

_PRE_RELEASE_IDENTIFIERS = frozenset(
    {
        "alpha",
        "beta",
        "rc",
        "preview",
        "dev",
        "nightly",
        "snapshot",
        "canary",
        "pre",
        "next",
        "insiders",
    }
)

_PRE_RELEASE_RE = re.compile(
    r"[-.](?:" + "|".join(_PRE_RELEASE_IDENTIFIERS) + r")(?:\.\d+|\d+)?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------


def parse_tag(name: str) -> dict:
    """Parse a tag name into a structured record with scheme classification.

    Returns a dict with: scheme, major, minor, patch, year, month, day,
    micro, pre_release, release_line, is_pre_release, sort_key.
    """
    clean = name.strip()

    # Strip release-branch prefixes
    prefix_match = _PREFIX_RE.match(clean)
    if prefix_match:
        clean = prefix_match.group(1)

    # Try semver
    sv = _SEMVER_RE.match(clean)
    if sv:
        major, minor, patch = (
            int(sv.group("major")),
            int(sv.group("minor")),
            int(sv.group("patch")),
        )
        pre = sv.group("pre")
        pre_sort = (0, pre or "") if pre else (1, "")
        return {
            "scheme": "semver",
            "major": major,
            "minor": minor,
            "patch": patch,
            "pre_release": pre,
            "release_line": f"{major}.{minor}",
            "is_pre_release": pre is not None,
            "sort_key": (0, major, minor, patch, pre_sort),
        }

    # Try calver
    cv = _CALVER_RE.match(clean)
    if cv:
        year_raw = int(cv.group("year"))
        year = year_raw if year_raw >= 100 else 2000 + year_raw
        month = int(cv.group("month"))
        day = int(cv.group("day") or 0)
        micro = int(cv.group("micro") or 0)
        pre = cv.group("pre")
        pre_sort = (0, pre or "") if pre else (1, "")
        return {
            "scheme": "calver",
            "major": year,
            "minor": month,
            "patch": day,
            "year": year,
            "month": month,
            "day": day,
            "micro": micro,
            "pre_release": pre,
            "release_line": f"{year}.{month}",
            "is_pre_release": pre is not None,
            "sort_key": (1, year, month, day, micro, pre_sort),
        }

    # Unknown: best-effort
    pre = None
    pre_match = _PRE_RELEASE_RE.search(clean)
    if pre_match:
        pre = pre_match.group(0).lstrip("-.")

    try:
        pv = PkgVersion(clean.lstrip("vV"))
        sk = (2, pv.major, pv.minor, pv.micro, (1, ""))
    except InvalidVersion:
        sk = (2, 0, 0, 0, (0, clean))

    return {
        "scheme": "unknown",
        "major": 0,
        "minor": 0,
        "patch": 0,
        "pre_release": pre,
        "release_line": "unknown",
        "is_pre_release": pre is not None,
        "sort_key": sk,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch version tags with smart parsing & classification."
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
        "--max-tags",
        type=int,
        default=200,
        help="Maximum number of tags to fetch (most recent first, default: 200).",
    )
    return parser


def get_github(token: str) -> Github:
    auth = Auth.Token(token)
    return Github(auth=auth, per_page=100)


def main(tokens: list[str], out_dir: Path, org: str, repo: str, max_tags: int = 200):
    print("starting get version tags (with smart parsing)")
    print(f"Output directory: {out_dir}")
    print(f"Org: {org}")
    print(f"Repo: {repo}")
    print(f"Max tags: {max_tags}")

    g = get_github(random.choice(tokens))
    r = g.get_repo(f"{org}/{repo}")

    tag_records: list[dict] = []
    count = 0

    for tag in tqdm(r.get_tags(), desc="Fetching tags"):
        if count >= max_tags:
            break

        name = tag.name
        sha = tag.commit.sha
        if not name or not sha:
            continue

        # Resolve the commit date for this tag
        try:
            commit = r.get_commit(sha)
            date = commit.commit.committer.date
            date_str = date.isoformat() if date else ""
        except GithubException:
            date_str = ""

        if not date_str:
            continue

        # Parse the tag with smart classification
        parsed = parse_tag(name)

        record = {
            "name": name,
            "sha": sha,
            "date": date_str,
            **parsed,
        }
        tag_records.append(record)
        count += 1

    tag_records.sort(key=lambda t: t["sort_key"])

    # Report scheme breakdown
    schemes = {}
    pre_count = 0
    for t in tag_records:
        schemes[t["scheme"]] = schemes.get(t["scheme"], 0) + 1
        if t["is_pre_release"]:
            pre_count += 1

    print(f"Tag schemes: {schemes}")
    print(f"Pre-release tags: {pre_count}/{len(tag_records)}")

    # Report release lines
    lines: dict[str, int] = {}
    for t in tag_records:
        lines[t["release_line"]] = lines.get(t["release_line"], 0) + 1
    if len(lines) <= 20:
        print(f"Release lines: {dict(sorted(lines.items()))}")
    else:
        print(f"Release lines: {len(lines)} distinct lines")

    out_file = out_dir / f"{org}__{repo}_tags.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for record in tag_records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"Wrote {len(tag_records)} tags to {out_file}")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    tokens = get_tokens(args.tokens)
    main(tokens, Path.cwd() / args.out_dir, args.org, args.repo, args.max_tags)
