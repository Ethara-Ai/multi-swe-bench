import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# PRs 5241, 5868 (Sept -> Dec 2023, AutoGPT release 0.4.7 -> 0.5.x).
# Forge era: project restructured under autogpts/autogpt/. Uses Poetry.
# Python 3.10 per pyproject.toml (`python = "^3.10"`).
#
# Two-class layout: ImageBase installs Poetry + system deps + clones the repo
# (shared by both PRs). ImageDefault layers patches + scripts. prepare.sh does
# the per-PR checkout, then `poetry install` with a fallback chain for PR 5241
# whose poetry.lock pins yanked agent-protocol* versions.


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
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base = self.dependency()
        if isinstance(base, Image):
            base = base.image_full_name()
        # Minimal base: apt deps + Poetry CLI. Repo clone deferred to
        # prepare.sh so the cross-arch base build stays small. Poetry CLI
        # is fine to install here because it's era-wide, not per-PR.
        return f"""FROM {base}

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/root/.local/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl build-essential libxml2-dev libxslt1-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 -

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
        return f"pr-{self.pr.number}-era2"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}-era2"

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
export PATH="/root/.local/bin:$PATH"
cd /home
if [ ! -d /home/AutoGPT/.git ]; then
    git clone https://github.com/Significant-Gravitas/AutoGPT.git /home/AutoGPT
fi
cd /home/AutoGPT
git reset --hard
git checkout {pr.base.sha}
cd /home/AutoGPT/autogpts/autogpt
# Try a clean install; if locked agent-protocol* (PR 5241) is yanked, fall back
# to pip-installing AutoGPT's runtime deps directly with version pins that
# keep pydantic v1 working.
poetry install --no-interaction 2>&1 | tee /tmp/poetry.log
poetry_status=${{PIPESTATUS[0]}}
if [ "$poetry_status" -ne 0 ]; then
    poetry lock --no-cache --regenerate || true
    poetry install --no-interaction --no-root --without dev || true
    poetry run pip install --no-cache-dir \\
        'pydantic<2' 'spacy>=3.5,<3.7' 'openapi-python-client<0.14' \\
        'numpy<2' \\
        PyPDF2 jsonschema pyyaml openai colorama distro inflection \\
        beautifulsoup4 requests tiktoken orjson python-dotenv click \\
        gitpython ftfy charset-normalizer numpy selenium \\
        webdriver-manager docker duckduckgo-search prompt_toolkit \\
        Pillow markdown pylatexenc python-docx readability-lxml \\
        gTTS 'playsound==1.2.2' pinecone-client redis fastapi \\
        hypercorn uvicorn psycopg2-binary boto3 \\
        google-api-python-client google-cloud-logging google-cloud-storage \\
        'agent-protocol>=1' 'agent-protocol-client>=1' \\
        'auto-gpt-plugin-template @ git+https://github.com/Significant-Gravitas/Auto-GPT-Plugin-Template@0.1.0' \\
        sentry-sdk pypdf \\
        || true
fi
poetry run pip install --no-cache-dir pytest pytest-mock pytest-asyncio \\
    pytest-cov pytest-recording pytest-xdist asynctest vcrpy || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PATH="/root/.local/bin:$PATH"
cd /home/AutoGPT/autogpts/autogpt
if [ -f tests.py ]; then EXTRA="tests.py"; else EXTRA=""; fi
poetry run pytest tests/unit -v --tb=short --continue-on-collection-errors \\
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
export PATH="/root/.local/bin:$PATH"
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
cd /home/AutoGPT/autogpts/autogpt
if [ -f tests.py ]; then EXTRA="tests.py"; else EXTRA=""; fi
poetry run pytest tests/unit -v --tb=short --continue-on-collection-errors \\
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
export PATH="/root/.local/bin:$PATH"
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
cd /home/AutoGPT/autogpts/autogpt
if [ -f tests.py ]; then EXTRA="tests.py"; else EXTRA=""; fi
poetry run pytest tests/unit -v --tb=short --continue-on-collection-errors \\
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


@Instance.register("Significant-Gravitas", "AutoGPT_5868_to_5241")
class AUTOGPT_5868_TO_5241(Instance):
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
