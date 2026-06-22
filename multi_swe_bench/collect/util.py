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
import os
import sys
from pathlib import Path


def parse_tokens(tokens: str | list[str] | Path) -> list[str]:
    """
    Try to parse tokens as a list of strings.
    """

    if isinstance(tokens, list):
        return tokens
    elif isinstance(tokens, str):
        return [tokens]
    elif isinstance(tokens, Path):
        if not tokens.exists() or not tokens.is_file():
            raise ValueError(f"Token file {tokens} does not exist or is not a file.")
        with tokens.open("r", encoding="utf-8") as file:
            return [line.strip() for line in file if line.strip()]
    return []


def _load_env_tokens() -> list[str]:
    """Load tokens from a .env file (GITHUB_TOKENS=... or GITHUB_TOKEN=...)."""
    # Walk up from cwd to find a .env file
    for directory in [Path.cwd()] + list(Path.cwd().parents):
        env_file = directory / ".env"
        if env_file.is_file():
            break
    else:
        return []

    tokens: list[str] = []
    with env_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # Strip optional quotes
            value = value.strip().strip('"').strip("'")
            if key in ("GITHUB_TOKENS", "GITHUB_TOKEN", "GH_TOKEN"):
                # Support comma-separated and newline-separated
                for tok in value.split(","):
                    tok = tok.strip()
                    if tok:
                        tokens.append(tok)
    return tokens


def find_default_token_file() -> Path:
    """
    Try to find a default token file in the current directory.
    """

    possible_files = ["token", "tokens", "token.txt", "tokens.txt"]
    for file_name in possible_files:
        file_path = Path(file_name)
        file_path = Path.cwd() / file_path
        if file_path.exists() and file_path.is_file():
            return file_path
    return None


def get_tokens(tokens) -> list[str]:
    if tokens is None:
        # Try .env file first, then token files
        env_tokens = _load_env_tokens()
        if env_tokens:
            print(f"Loaded {len(env_tokens)} token(s) from .env file")
            return env_tokens

        default_token_file = find_default_token_file()
        if default_token_file is None:
            # Last resort: check environment variables directly
            for var in ("GITHUB_TOKENS", "GITHUB_TOKEN", "GH_TOKEN"):
                val = os.environ.get(var, "").strip()
                if val:
                    env_list = [t.strip() for t in val.split(",") if t.strip()]
                    if env_list:
                        print(f"Loaded {len(env_list)} token(s) from ${var}")
                        return env_list
            print("Error: No tokens provided. Pass --tokens, set GITHUB_TOKENS in .env, or create a tokens file.")
            sys.exit(1)
        tokens = default_token_file
    else:
        # If tokens are provided as a list, they might need conversion
        tokens = tokens[0] if len(tokens) == 1 else tokens

    try:
        token_list = parse_tokens(tokens)
        if not token_list:
            raise ValueError("Token list is empty after parsing.")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not token_list:
        raise ValueError("No tokens provided.")
    return token_list


def optional_int(value):
    if value.lower() == "none" or value.lower() == "null" or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid integer value: {value}")
