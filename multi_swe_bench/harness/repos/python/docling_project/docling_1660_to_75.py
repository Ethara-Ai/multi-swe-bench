import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "python:3.10-slim-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _get_merge_commit_sha(self) -> str:
        """Extract merge_commit_sha from the raw PR data."""
        # The PullRequest model uses from_json which passes through extra fields.
        # We access the raw data to get merge_commit_sha.
        return getattr(self.pr, 'merge_commit_sha', '')

    def files(self) -> list[File]:
        repo_name = self.pr.repo

        # Helper script to apply patches with binary file support
        apply_patch_helper = """#!/bin/bash
# apply_patch.sh - Applies a patch with binary file fallback
# Usage: bash /home/apply_patch.sh <patch_file> [patch_file2 ...]
set -o pipefail

REPO_DIR="/home/[[REPO_NAME]]"
cd "$REPO_DIR"

# Collect all patch files from arguments
PATCH_FILES=("$@")

# Try applying all patches at once first
if git apply --whitespace=nowarn "${PATCH_FILES[@]}" 2>/dev/null; then
    exit 0
fi

# If that failed, apply each patch separately with binary fallback
for PATCH_FILE in "${PATCH_FILES[@]}"; do
    if [ ! -f "$PATCH_FILE" ]; then
        echo "Warning: patch file $PATCH_FILE not found, skipping" >&2
        continue
    fi

    # Try clean apply first
    if git apply --whitespace=nowarn "$PATCH_FILE" 2>/dev/null; then
        continue
    fi

    # Check if there are binary files in this patch
    BINARY_FILES=$(grep -E "^Binary files /dev/null and b/" "$PATCH_FILE" | sed 's|^Binary files /dev/null and b/||; s| differ$||')

    if [ -n "$BINARY_FILES" ]; then
        # Strip binary hunks and apply only text hunks
        # Create a filtered patch without binary file diffs
        FILTERED_PATCH=$(mktemp)
        python3 -c "
import sys
patch_file = sys.argv[1]
with open(patch_file, 'r', errors='replace') as f:
    content = f.read()

# Split by diff headers
parts = content.split('diff --git ')
filtered_parts = []
for part in parts:
    if not part.strip():
        continue
    # Check if this diff section contains a binary marker
    if 'Binary files /dev/null and' in part or 'Binary files a/' in part or 'GIT binary patch' in part:
        continue
    filtered_parts.append(part)

if filtered_parts:
    result = 'diff --git ' + 'diff --git '.join(filtered_parts)
    with open(sys.argv[2], 'w') as f:
        f.write(result)
else:
    # All hunks were binary, write empty file
    open(sys.argv[2], 'w').close()
" "$PATCH_FILE" "$FILTERED_PATCH"

        # Apply filtered text-only patch
        if [ -s "$FILTERED_PATCH" ]; then
            git apply --whitespace=nowarn "$FILTERED_PATCH" 2>/dev/null || \
            git apply --whitespace=nowarn --reject "$FILTERED_PATCH" 2>/dev/null || true
        fi
        rm -f "$FILTERED_PATCH"

        # Fetch binary files from merge commit
        MERGE_SHA=""
        if [ -f /home/merge_commit_sha ]; then
            MERGE_SHA=$(cat /home/merge_commit_sha)
        fi

        if [ -n "$MERGE_SHA" ]; then
            # Fetch the merge commit
            git fetch origin "$MERGE_SHA" 2>/dev/null || true

            for BIN_FILE in $BINARY_FILES; do
                echo "Fetching binary file: $BIN_FILE from merge commit $MERGE_SHA"
                # Create parent directory
                mkdir -p "$(dirname "$BIN_FILE")"
                # Checkout the binary file from the merge commit
                git checkout "$MERGE_SHA" -- "$BIN_FILE" 2>/dev/null || \
                echo "Warning: could not fetch binary file $BIN_FILE" >&2
            done
        else
            echo "Warning: no merge_commit_sha available, binary files may be missing" >&2
        fi
    else
        # No binary files, try with --reject to apply what we can
        git apply --whitespace=nowarn --reject "$PATCH_FILE" 2>/dev/null || true
        # Clean up .rej files
        find . -name "*.rej" -delete 2>/dev/null || true
    fi
done
""".replace("[[REPO_NAME]]", repo_name)

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
                "apply_patch.sh",
                apply_patch_helper,
            ),
            File(
                ".",
                "prepare.sh",
                """ls -la
###ACTION_DELIMITER###
pip install poetry
###ACTION_DELIMITER###
poetry install --all-extras
###ACTION_DELIMITER###
echo 'poetry run pytest -v tests' > test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider -v tests

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
bash /home/apply_patch.sh /home/test.patch
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider -v tests

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider -v tests

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        merge_sha = getattr(self.pr, 'merge_commit_sha', '') or ""

        dockerfile_content = """
# Docling Poetry-era build (PRs 75-1660)
FROM python:3.10-slim-bookworm

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements and system dependencies for docling
RUN apt-get update && apt-get install -y git build-essential pkg-config \\
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender-dev \\
    tesseract-ocr tesseract-ocr-eng libleptonica-dev libtesseract-dev \\
    ffmpeg libmagic1

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/docling-project/docling.git /home/docling

WORKDIR /home/docling
RUN git reset --hard
RUN git checkout {pr.base.sha}

# Install poetry first, then install project deps
RUN pip install --no-cache-dir "poetry<1.9"
RUN poetry config virtualenvs.create false
RUN poetry install --all-extras --no-interaction || poetry install --no-interaction

# Fix packaging.licenses: force-reinstall AFTER poetry to prevent version downgrade
RUN pip install --no-cache-dir --force-reinstall "packaging>=24.2"
"""

        # Write merge_commit_sha for binary file fetching
        dockerfile_content += f'\nRUN echo "{merge_sha}" > /home/merge_commit_sha\n'

        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("docling-project", "docling_1660_to_75")
class DOCLING_1660_TO_75(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        # Parse the log content and extract test execution results.
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped

        # Pattern 1: test_name STATUS [percentage]
        pattern_test_status = re.compile(
            r"^\s*(?:\[\s*\d+\s*\]\s*)?"  # Optional line number in brackets
            r"([\w/.]+::[\w\[\]()=, -]+)"  # Test name (contains ::)
            r"\s+"
            r"(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)"  # Status
            r"\s*.*$"  # Remaining content
        )
        # Pattern 2: STATUS test_name
        pattern_status_test = re.compile(
            r"^\s*(?:\[\s*\d+\s*\]\s*)?"  # Optional line number in brackets
            r"(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)"  # Status
            r"\s+"
            r"([\w/.]+::[\w\[\]()=, -]+)"  # Test name (contains ::)
            r"\s*.*$"  # Remaining content
        )
        for line in log.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Check pattern 1: test name followed by status
            match = pattern_test_status.match(line)
            if match:
                test_name = match.group(1)
                status = match.group(2)
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
                elif status == "XFAIL":
                    failed_tests.add(test_name)
                elif status == "XPASS":
                    passed_tests.add(test_name)
                continue
            # Check pattern 2: status followed by test name
            match = pattern_status_test.match(line)
            if match:
                status = match.group(1)
                test_name = match.group(2)
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
                elif status == "XFAIL":
                    failed_tests.add(test_name)
                elif status == "XPASS":
                    passed_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
