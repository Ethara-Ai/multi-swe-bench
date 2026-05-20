import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# PRs 215, 463, 754, 1380 (April 2023, Auto-GPT release 0.1 -> 0.2)
# Pre-pyproject.toml era. Layout: scripts/ (or autogpt/ after PR 1380's rename),
# requirements.txt at repo root, tests/ directory contains a mix of unittest-
# and pytest-style tests. Python 3.11 works in practice (Dockerfile of this era
# pins python:3.11; the 3.8 matrix in CI is too old for pyyaml==6.0 wheels under
# modern toolchains).
#
# Two-class layout: ImageBase holds the system-level apt deps + repo clone and
# is SHARED across all PRs in this era (so the harness builds the base image
# once, then each PR's ImageDefault layers patches + scripts on top).


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
        return "python:3.11-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        # Fixed per-era tag so all PRs in this era share one cached base image.
        return "base-era0"

    def workdir(self) -> str:
        return "base-era0"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base = self.dependency()
        if isinstance(base, Image):
            base = base.image_full_name()
        # Minimal base: apt deps only. Repo clone is deferred to prepare.sh
        # to keep the cross-arch base build small (the AutoGPT repo at HEAD is
        # ~3,800 files and clone+layer-export dominates buildkit runtime,
        # which caused the parallel native-arch rebuild to OOM out previously).
        return f"""FROM {base}

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git build-essential \\
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
        return f"pr-{self.pr.number}-era0"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}-era0"

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
# Clone here (deferred from ImageBase to keep the shared base image small).
# If the layer cache happens to contain /home/AutoGPT from a sibling build,
# just reuse it; the reset+checkout below pins to the per-PR sha either way.
if [ ! -d /home/AutoGPT/.git ]; then
    git clone https://github.com/Significant-Gravitas/AutoGPT.git /home/AutoGPT
fi
cd /home/AutoGPT
git reset --hard
git checkout {pr.base.sha}
if ! pip install --no-cache-dir -r requirements.txt; then
    while IFS= read -r line; do
        clean=$(echo "$line" | sed 's/#.*//' | xargs)
        [ -z "$clean" ] && continue
        pip install --no-cache-dir "$clean" || true
    done < requirements.txt
fi
pip install --no-cache-dir pytest pytest-mock pytest-asyncio sentry-sdk || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/AutoGPT
if [ -f tests.py ]; then EXTRA="tests.py"; else EXTRA=""; fi
python -m pytest tests/ $EXTRA -v --tb=short --continue-on-collection-errors -p no:cacheprovider \\
    -o python_files='test_*.py *_test.py *_tests.py'
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
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
python -m pytest tests/ $EXTRA -v --tb=short --continue-on-collection-errors -p no:cacheprovider \\
    -o python_files='test_*.py *_test.py *_tests.py'
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
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
python -m pytest tests/ $EXTRA -v --tb=short --continue-on-collection-errors -p no:cacheprovider \\
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


@Instance.register("Significant-Gravitas", "AutoGPT_1380_to_0")
class AUTOGPT_1380_TO_0(Instance):
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

        # pytest verbose lines: "tests/path/test_file.py::test_name PASSED"
        pattern = r"(\btests(?:\.py|/[^\s]*?)::[^\s]+?) (PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b"
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
