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

    def files(self) -> list[File]:
        repo_name = self.pr.repo

        apply_patch_helper = """#!/bin/bash
# apply_patch.sh - Applies patches with binary file fallback via merge commit
set -o pipefail

REPO_DIR="/home/[[REPO_NAME]]"
cd "$REPO_DIR"

PATCH_FILES=("$@")

if git apply --whitespace=nowarn "${PATCH_FILES[@]}" 2>/dev/null; then
    exit 0
fi

for PATCH_FILE in "${PATCH_FILES[@]}"; do
    if [ ! -f "$PATCH_FILE" ]; then
        continue
    fi

    if git apply --whitespace=nowarn "$PATCH_FILE" 2>/dev/null; then
        continue
    fi

    BINARY_FILES=$(grep -E "^Binary files /dev/null and b/" "$PATCH_FILE" | sed 's|^Binary files /dev/null and b/||; s| differ$||')

    if [ -n "$BINARY_FILES" ]; then
        FILTERED_PATCH=$(mktemp)
        python3 -c "
import sys
with open(sys.argv[1], 'r', errors='replace') as f:
    content = f.read()
parts = content.split('diff --git ')
filtered = []
for part in parts:
    if not part.strip():
        continue
    if 'Binary files /dev/null and' in part or 'Binary files a/' in part or 'GIT binary patch' in part:
        continue
    filtered.append(part)
if filtered:
    with open(sys.argv[2], 'w') as f:
        f.write('diff --git ' + 'diff --git '.join(filtered))
else:
    open(sys.argv[2], 'w').close()
" "$PATCH_FILE" "$FILTERED_PATCH"

        if [ -s "$FILTERED_PATCH" ]; then
            git apply --whitespace=nowarn "$FILTERED_PATCH" 2>/dev/null || \
            git apply --whitespace=nowarn --reject "$FILTERED_PATCH" 2>/dev/null || true
        fi
        rm -f "$FILTERED_PATCH"

        MERGE_SHA=""
        if [ -f /home/merge_commit_sha ]; then
            MERGE_SHA=$(cat /home/merge_commit_sha)
        fi

        if [ -n "$MERGE_SHA" ]; then
            git fetch origin "$MERGE_SHA" 2>/dev/null || true
            for BIN_FILE in $BINARY_FILES; do
                mkdir -p "$(dirname "$BIN_FILE")"
                git checkout "$MERGE_SHA" -- "$BIN_FILE" 2>/dev/null || true
            done
        fi
    else
        git apply --whitespace=nowarn --reject "$PATCH_FILE" 2>/dev/null || true
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
pip install -e '.[all]' || pip install -e '.[easyocr,tesserocr,vlm,rapidocr]' || pip install -e '.'
###ACTION_DELIMITER###
pip install pytest
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider -v' > test_commands.sh
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
pytest --no-header -rA --tb=no -p no:cacheprovider -v

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
bash /home/apply_patch.sh /home/test.patch
pytest --no-header -rA --tb=no -p no:cacheprovider -v

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
pytest --no-header -rA --tb=no -p no:cacheprovider -v

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        merge_sha = getattr(self.pr, 'merge_commit_sha', '') or ""

        dockerfile_content = """
# Docling UV-era stable build (PRs 2366-3309)
FROM python:3.10-slim-bookworm

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements and system dependencies for docling
RUN apt-get update && apt-get install -y git build-essential pkg-config \\
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender-dev \\
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-spa \\
    tesseract-ocr-script-latn libleptonica-dev libtesseract-dev \\
    ffmpeg libmagic1 libreoffice

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/docling-project/docling.git /home/docling

WORKDIR /home/docling
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -e '.[easyocr,tesserocr,vlm,rapidocr,xbrl]' || pip install --no-cache-dir -e '.[easyocr,tesserocr,rapidocr]' || pip install --no-cache-dir -e '.'
RUN pip install --no-cache-dir pytest
"""

        dockerfile_content += f'\nRUN echo "{merge_sha}" > /home/merge_commit_sha\n'

        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("docling-project", "docling_3309_to_2366")
class DOCLING_3309_TO_2366(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        pattern_test_status = re.compile(
            r"^\s*(?:\[\s*\d+\s*\]\s*)?"
            r"([\w/.]+::[\w\[\]()=, -]+)"
            r"\s+"
            r"(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)"
            r"\s*.*$"
        )
        pattern_status_test = re.compile(
            r"^\s*(?:\[\s*\d+\s*\]\s*)?"
            r"(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)"
            r"\s+"
            r"([\w/.]+::[\w\[\]()=, -]+)"
            r"\s*.*$"
        )
        for line in log.split("\n"):
            line = line.strip()
            if not line:
                continue
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
