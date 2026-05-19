import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "python:3.12-bookworm"

    def image_tag(self) -> str:
        return "base-agno_99999_to_5000"

    def workdir(self) -> str:
        return "base-agno_99999_to_5000"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""\
FROM {self.dependency()}
{self.global_env}
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}/libs/agno
RUN pip install --no-cache-dir --upgrade pip uv
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
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd /home/{self.pr.repo}
git reset --hard
git checkout {self.pr.base.sha}
cd /home/{self.pr.repo}/libs/agno
EXTRAS_LIST=$(python -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml','rb') as f:
    data = tomllib.load(f)
# Skip known-conflicting extras (lmstudio + brave-search conflict on httpx);
# they're recovered later in the per-extra fallback if needed.
SKIP = {{'lmstudio', 'brave', 'brave-search', 'brave_search', 'infinity', 'infinity_client'}}
extras = [k for k in data.get('project', {{}}).get('optional-dependencies', {{}}).keys() if k not in SKIP]
print(','.join(extras))
print('---')
print('\\n'.join(extras))
" 2>/dev/null)
EXTRAS_CSV=$(echo "$EXTRAS_LIST" | sed -n '1p')
EXTRAS_ITER=$(echo "$EXTRAS_LIST" | sed -n '3,$p')
UV_OPTS="--no-cache --python /usr/local/bin/python --index-strategy unsafe-best-match"
if [ -n "$EXTRAS_CSV" ] && uv pip install $UV_OPTS -e ".[${{EXTRAS_CSV}}]"; then
    echo "--- bulk install succeeded ---"
else
    echo "--- bulk install failed; falling back to per-extra loop ---"
    uv pip install $UV_OPTS -e ".[dev]" \\
        || uv pip install $UV_OPTS -e . \\
        || pip install --no-cache-dir -e ".[dev]" \\
        || true
    if [ -n "$EXTRAS_ITER" ]; then
        for extra in $EXTRAS_ITER; do
            [ "$extra" = "dev" ] && continue
            echo "--- installing extra: $extra ---"
            uv pip install $UV_OPTS -e ".[${{extra}}]" || true
        done
    fi
fi
uv pip install $UV_OPTS sqlalchemy pypdf chromadb lancedb tantivy qdrant-client pgvector pillow || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}/libs/agno
PATCH_TESTS=$( (grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/test.patch 2>/dev/null || true; \\
                grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/fix.patch 2>/dev/null || true) \\
              | sed 's|^diff --git a/libs/agno/||' | sort -u )
EXISTING=""
for p in $PATCH_TESTS; do
    [ -f "$p" ] && EXISTING="$EXISTING $p"
done
PYTHONUNBUFFERED=1 timeout --kill-after=10 300 python -m pytest $EXISTING -v --no-header --tb=short --continue-on-collection-errors || true
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
GIT_BIN_EXCL="--exclude=*.pdf --exclude=*.docx --exclude=*.doc --exclude=*.swp \\
              --exclude=*.jpg --exclude=*.jpeg --exclude=*.png --exclude=*.gif --exclude=*.ico --exclude=*.bmp --exclude=*.svg \\
              --exclude=*.zip --exclude=*.tar --exclude=*.gz --exclude=*.bz2 --exclude=*.xz \\
              --exclude=*.bin --exclude=*.so --exclude=*.dll --exclude=*.exe --exclude=*.pyc \\
              --exclude=*.mp3 --exclude=*.mp4 --exclude=*.wav --exclude=*.ogg --exclude=*.webm \\
              --exclude=*.pkl --exclude=*.npz --exclude=*.npy --exclude=*.db --exclude=*.sqlite"
if ! git apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn $GIT_BIN_EXCL /home/test.patch; then
        echo "Error: git apply test.patch failed (even after excluding binary files)" >&2
        exit 1
    fi
    echo "Note: test.patch applied with binary file exclusions"
fi
cd /home/{self.pr.repo}/libs/agno
PATCH_TESTS=$( (grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/test.patch 2>/dev/null || true) \\
              | sed 's|^diff --git a/libs/agno/||' | sort -u )
EXISTING=""
for p in $PATCH_TESTS; do
    [ -f "$p" ] && EXISTING="$EXISTING $p"
done
PYTHONUNBUFFERED=1 timeout --kill-after=10 300 python -m pytest $EXISTING -v --no-header --tb=short --continue-on-collection-errors || true
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
GIT_BIN_EXCL="--exclude=*.pdf --exclude=*.docx --exclude=*.doc --exclude=*.swp \\
              --exclude=*.jpg --exclude=*.jpeg --exclude=*.png --exclude=*.gif --exclude=*.ico --exclude=*.bmp --exclude=*.svg \\
              --exclude=*.zip --exclude=*.tar --exclude=*.gz --exclude=*.bz2 --exclude=*.xz \\
              --exclude=*.bin --exclude=*.so --exclude=*.dll --exclude=*.exe --exclude=*.pyc \\
              --exclude=*.mp3 --exclude=*.mp4 --exclude=*.wav --exclude=*.ogg --exclude=*.webm \\
              --exclude=*.pkl --exclude=*.npz --exclude=*.npy --exclude=*.db --exclude=*.sqlite"
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn $GIT_BIN_EXCL /home/test.patch /home/fix.patch; then
        echo "Error: git apply test+fix patches failed (even after excluding binary files)" >&2
        exit 1
    fi
    echo "Note: test+fix patches applied with binary file exclusions"
fi
cd /home/{self.pr.repo}/libs/agno
PATCH_TESTS=$( (grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/test.patch 2>/dev/null || true; \\
                grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/fix.patch 2>/dev/null || true) \\
              | sed 's|^diff --git a/libs/agno/||' | sort -u )
EXISTING=""
for p in $PATCH_TESTS; do
    [ -f "$p" ] && EXISTING="$EXISTING $p"
done
PYTHONUNBUFFERED=1 timeout --kill-after=10 300 python -m pytest $EXISTING -v --no-header --tb=short --continue-on-collection-errors || true
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}
{self.global_env}
COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh
{self.clear_env}
"""


@Instance.register("agno-agi", "agno_99999_to_5000")
class AGNO_99999_TO_5000(Instance):
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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        passed_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(\S+)\s+PASSED\s+\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        failed_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(\S+)\s+FAILED\s+\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        skipped_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(\S+)\s+SKIPPED\s+\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        passed_tests.update(passed_pattern.findall(clean_log))
        failed_tests.update(failed_pattern.findall(clean_log))
        skipped_tests.update(skipped_pattern.findall(clean_log))

        summary_failed = re.compile(r"^FAILED\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE)
        summary_passed = re.compile(r"^PASSED\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE)
        summary_skipped = re.compile(r"^SKIPPED\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE)
        failed_tests.update(summary_failed.findall(clean_log))
        passed_tests.update(summary_passed.findall(clean_log))
        skipped_tests.update(summary_skipped.findall(clean_log))

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
