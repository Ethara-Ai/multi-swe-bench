import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# PRs 289, 882, 905, 1296, 2665, 2804, 3058, 4987
# Auto-GPT releases 0.2 -> 0.4.6 (April -> July 2023). Repo layout: autogpt/
# package at root, requirements.txt + pyproject.toml. Tests at tests/. Python
# 3.10 matches CI's min-python-version and the pyproject `requires-python`.
#
# Two-class layout: ImageBase pre-installs system deps + clones the repo,
# shared across all 8 PRs in this era. ImageDefault adds patches + scripts;
# prepare.sh does the per-PR checkout + Python install with per-line fallback
# (PR 905 pins `sourcery` which has been yanked from PyPI).


class ImageBase(Image):
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
        return "python:3.10-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-era1"

    def workdir(self) -> str:
        return "base-era1"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base = self.dependency()
        if isinstance(base, Image):
            base = base.image_full_name()
        # Minimal base: apt deps only. Repo clone deferred to prepare.sh.
        return f"""FROM {base}

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git build-essential libxml2-dev libxslt1-dev \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
"""


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-era1"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}-era1"

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
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home
if [ ! -d /home/AutoGPT/.git ]; then
    git clone https://github.com/Significant-Gravitas/AutoGPT.git /home/AutoGPT
fi
cd /home/AutoGPT
git reset --hard
git checkout {pr.base.sha}
pip install --no-cache-dir --upgrade pip setuptools wheel || true
# Bulk install first (fast). If it fails (e.g. PR 905 pins `sourcery` which has
# been yanked from PyPI), fall back to per-line installs so one bad pin does
# not skip everything below it.
if ! pip install --no-cache-dir -r requirements.txt; then
    while IFS= read -r line; do
        clean=$(echo "$line" | sed 's/#.*//' | xargs)
        [ -z "$clean" ] && continue
        pip install --no-cache-dir "$clean" || true
    done < requirements.txt
fi
# numpy<2 is REQUIRED: spacy 3.5/thinc 8.1 were built against numpy 1.x ABI;
# pip otherwise resolves unpinned `numpy` in requirements.txt to 2.x and
# pytest fails to collect with "numpy.dtype size changed" on import.
pip install --no-cache-dir 'numpy<2' \\
    pytest pytest-mock pytest-asyncio pytest-cov pytest-integration \\
    pytest-recording pytest-xdist asynctest vcrpy \\
    ftfy distro pypdf 'duckduckgo_search<6' || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONPATH=/home/AutoGPT
cd /home/AutoGPT
if [ -f tests.py ]; then EXTRA="tests.py"; else EXTRA=""; fi
python -m pytest tests/unit -v --tb=short --continue-on-collection-errors \\
    -p no:cacheprovider --no-header \\
    -o python_files='test_*.py *_test.py *_tests.py'
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONPATH=/home/AutoGPT
cd /home/AutoGPT
apply_patch_lenient() {
    local p="$1"
    if git -C /home/AutoGPT apply --whitespace=nowarn \\
        --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
        --exclude='*.ico' --exclude='*.zip' --exclude='*.pdf' --exclude='*.bin' \\
        --exclude='*.wasm' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' \\
        --exclude='*.otf' --exclude='*.eot' --exclude='*.tar.gz' --exclude='*.so' \\
        --exclude='*.dylib' "$p" 2>/dev/null; then
        return 0
    fi
    echo "git apply failed for $p, trying patch -p1 --fuzz=3 fallback..." >&2
    if patch -p1 --forward --fuzz=3 --batch --reject-file=/tmp/$(basename $p).rej < "$p" 2>&1 | tail -50; then
        return 0
    fi
    echo "Warning: $p did not fully apply, continuing anyway" >&2
    return 0
}
apply_patch_lenient /home/test.patch
if [ -f tests.py ]; then EXTRA="tests.py"; else EXTRA=""; fi
python -m pytest tests/unit -v --tb=short --continue-on-collection-errors \\
    -p no:cacheprovider --no-header \\
    -o python_files='test_*.py *_test.py *_tests.py'
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONPATH=/home/AutoGPT
cd /home/AutoGPT
apply_patch_lenient() {
    local p="$1"
    if git -C /home/AutoGPT apply --whitespace=nowarn \\
        --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
        --exclude='*.ico' --exclude='*.zip' --exclude='*.pdf' --exclude='*.bin' \\
        --exclude='*.wasm' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' \\
        --exclude='*.otf' --exclude='*.eot' --exclude='*.tar.gz' --exclude='*.so' \\
        --exclude='*.dylib' "$p" 2>/dev/null; then
        return 0
    fi
    echo "git apply failed for $p, trying patch -p1 --fuzz=3 fallback..." >&2
    if patch -p1 --forward --fuzz=3 --batch --reject-file=/tmp/$(basename $p).rej < "$p" 2>&1 | tail -50; then
        return 0
    fi
    echo "Warning: $p did not fully apply, continuing anyway" >&2
    return 0
}
apply_patch_lenient /home/test.patch
apply_patch_lenient /home/fix.patch
if [ -f tests.py ]; then EXTRA="tests.py"; else EXTRA=""; fi
python -m pytest tests/unit -v --tb=short --continue-on-collection-errors \\
    -p no:cacheprovider --no-header \\
    -o python_files='test_*.py *_test.py *_tests.py'
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_name()
        base_tag = base.image_tag()
        copy_lines = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return f"""FROM {base_name}:{base_tag}

{copy_lines}

RUN bash /home/prepare.sh
"""


@Instance.register("Significant-Gravitas", "AutoGPT_4987_to_289")
class AUTOGPT_4987_TO_289(Instance):
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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        pattern = r"(\btests(?:\.py|/[^\s]+?)::[^\s]+?) (PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b"
        for test_name, status in re.findall(pattern, log):
            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
