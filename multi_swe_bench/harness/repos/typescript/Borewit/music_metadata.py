"""Borewit/music-metadata harness config — Mocha + Chai, Yarn Berry, TypeScript with ts-node/esm."""

import re
import textwrap
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MusicMetadataImageBase(Image):
    """Base Docker image: node:20-bookworm with the repo cloned.

    music-metadata uses Yarn Berry (4.x) with nodeLinker: node-modules
    and requires corepack for Yarn version management.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*
RUN corepack enable

{code}

{self.clear_env}

"""


class MusicMetadataImageDefault(Image):
    """PR-specific Docker layer: patches, prepare, and run scripts.

    Tests run via mocha with ts-node/esm loader directly on TypeScript
    source (no compilation step needed). The .mocharc.json configures:
        extension: [ts, tsx]
        spec: [test/*.ts]
        loader: [ts-node/esm]
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return MusicMetadataImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "strip_binary_diffs.py",
                """\
#!/usr/bin/env python3
\"\"\"Strip binary diffs from patch files.

Removes binary diff sections (which git apply cannot handle when index
hashes are short) and writes the paths of binary files to a companion
file so they can be restored from the merge commit.

Usage:
    python3 strip_binary_diffs.py <patch1> [<patch2> ...]

For each <patchN>, writes <patchN>.binfiles with one binary path per line.
\"\"\"
import re
import sys

def strip_binary_diffs(patch_path):
    with open(patch_path, 'r', errors='replace') as f:
        content = f.read()

    # Split into per-file diffs
    diffs = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
    text_diffs = []
    binary_paths = []
    for diff in diffs:
        if not diff.strip():
            continue
        # Check for binary markers
        if 'Binary files' in diff or 'GIT binary patch' in diff:
            # Extract the b/ path from "diff --git a/... b/..."
            m = re.match(r'diff --git a/.+ b/(.+)', diff)
            if m:
                binary_paths.append(m.group(1).strip())
            continue
        text_diffs.append(diff)

    with open(patch_path, 'w') as f:
        f.write('\\n'.join(text_diffs))

    # Write binary file paths to companion file
    with open(patch_path + '.binfiles', 'w') as f:
        for p in binary_paths:
            f.write(p + '\\n')

if __name__ == '__main__':
    for path in sys.argv[1:]:
        strip_binary_diffs(path)
""",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install dependencies (Yarn Berry with corepack)
corepack enable || true
YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

# Run mocha tests (ts-node/esm loader runs TS directly)
yarn run test 2>&1 || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

# Strip binary diffs that git apply cannot handle (short index hashes)
# and collect binary file paths into .binfiles companion files
python3 /home/strip_binary_diffs.py /home/test.patch

# Apply text-only diffs
git apply --whitespace=nowarn /home/test.patch

# Restore binary files from the merge commit (the only commit that has them)
if [ -s /home/test.patch.binfiles ]; then
    # Find the merge commit that introduced the binary files
    FIRST_BIN=$(head -1 /home/test.patch.binfiles)
    MERGE_SHA=$(git log --all --diff-filter=A --format='%H' -- "$FIRST_BIN" | head -1)
    if [ -n "$MERGE_SHA" ]; then
        while IFS= read -r binpath; do
            [ -z "$binpath" ] && continue
            git checkout "$MERGE_SHA" -- "$binpath" 2>/dev/null || true
        done < /home/test.patch.binfiles
    fi
fi

# Reinstall in case test patch changes dependencies
corepack enable || true
YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install || true

yarn run test 2>&1 || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

# Strip binary diffs that git apply cannot handle (short index hashes)
# and collect binary file paths into .binfiles companion files
python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch

# Apply text-only diffs
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Restore binary files from the merge commit (the only commit that has them)
for patchfile in /home/test.patch /home/fix.patch; do
    binlist="${{patchfile}}.binfiles"
    if [ -s "$binlist" ]; then
        FIRST_BIN=$(head -1 "$binlist")
        MERGE_SHA=$(git log --all --diff-filter=A --format='%H' -- "$FIRST_BIN" | head -1)
        if [ -n "$MERGE_SHA" ]; then
            while IFS= read -r binpath; do
                [ -z "$binpath" ] && continue
                git checkout "$MERGE_SHA" -- "$binpath" 2>/dev/null || true
            done < "$binlist"
        fi
    fi
done

# Reinstall in case patches change dependencies
corepack enable || true
YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install || true

yarn run test 2>&1 || true
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )

        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("Borewit", "music-metadata")
class BorewitMusicMetadata(Instance):
    """Harness instance for Borewit/music-metadata — Mocha + Chai."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return MusicMetadataImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        """Parse Mocha spec reporter output into pass/fail/skip sets.

        Mocha spec reporter output looks like:

            MusicMetadata
              parse FLAC
                ✓ should parse vorbis comment (234ms)
                ✗ should handle corrupt file
                  AssertionError: expected ...
                - should be skipped (pending)

            7 passing (4s)
            1 failing
            1 pending
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", test_log)

        # Mocha test result markers (spec reporter)
        # Passed: ✓ or ✔ followed by test name, optional (Nms) duration
        re_pass = re.compile(
            r"^(\s+)[✓✔]\s+(.+?)(?:\s+\(\d+m?s\))?\s*$"
        )
        # Numbered failure list at the bottom:
        #   1) Suite > test name
        re_fail_numbered = re.compile(
            r"^\s+(\d+)\)\s+(.+?)\s*$"
        )
        # Skipped/pending: - test name (Mocha uses a dash for pending tests)
        re_skip = re.compile(
            r"^(\s+)-\s+(.+?)(?:\s+\(\d+m?s\))?\s*$"
        )

        # Summary line patterns
        re_summary_failing = re.compile(r"^\s*(\d+)\s+failing\b")

        # Track whether we're in the numbered failure list section
        in_failure_list = False
        failure_list_tests: set[str] = set()

        for line in clean_log.splitlines():
            # Detect start of Mocha's numbered failure list
            m = re_summary_failing.match(line)
            if m:
                in_failure_list = True
                continue

            # Passed test
            m = re_pass.match(line)
            if m:
                test_name = m.group(2).strip()
                passed_tests.add(test_name)
                in_failure_list = False
                continue

            # Skipped/pending test
            m = re_skip.match(line)
            if m:
                test_name = m.group(2).strip()
                skipped_tests.add(test_name)
                in_failure_list = False
                continue

            # Numbered failure (from the failure detail list at bottom)
            m = re_fail_numbered.match(line)
            if m:
                test_name = m.group(2).strip()
                failure_list_tests.add(test_name)
                continue

        # Merge failure list into failed_tests
        failed_tests.update(failure_list_tests)

        # Ensure mutual exclusivity: failed > passed > skipped
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
