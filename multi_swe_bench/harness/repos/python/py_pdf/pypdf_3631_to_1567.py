from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TAG_SUFFIX = "3631_to_1567"
_PYTHON_IMAGE = "python:3.11-slim-bookworm"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_STATUS = r"(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
_NODE = r"(tests/[^\s\[]+\.py(?:::[^\s\[]+)*(?:\[.*?\])?)"
_VERBOSE_RE = re.compile(rf"^{_NODE}\s+{_STATUS}(?:\s|$)")
_SUMMARY_RE = re.compile(rf"^{_STATUS}\s+{_NODE}(?:\s+-\s.*)?$")

_TEST_CMD = (
    "python -m pytest tests -v -rA --color=no --tb=short "
    "-p no:cacheprovider --benchmark-disable"
)


class PypdfImageBase_PYPDF_3631_TO_1567(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class PypdfImageDefault_PYPDF_3631_TO_1567(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return PypdfImageBase_PYPDF_3631_TO_1567(self.pr, self.config)

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
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
git submodule update --init --recursive || {{ echo "prepare.sh: sample-files submodule checkout failed"; exit 1; }}
test -n "$(ls -A sample-files)" || {{ echo "prepare.sh: sample-files submodule is empty"; exit 1; }}
bash /home/check_git_changes.sh

python --version
export PIP_DEFAULT_TIMEOUT=120
test -f requirements/ci-3.11.txt || {{ echo "prepare.sh: requirements/ci-3.11.txt not found at the base commit"; exit 1; }}
python -m pip install --no-cache-dir -r requirements/ci-3.11.txt
python -m pip install --no-cache-dir --no-deps -e .

mkdir -p tests/pdf_cache
if python -c "from tests import download_test_pdfs" 2>/dev/null; then
  python -c "from tests import download_test_pdfs; download_test_pdfs()"
else
  {_TEST_CMD} -q > /tmp/warm_pdf_cache.log 2>&1 || true
  tail -5 /tmp/warm_pdf_cache.log
  git checkout -- .
  git clean -fd
fi
test -n "$(ls -A tests/pdf_cache)" || {{ echo "prepare.sh: tests/pdf_cache is empty after the download step"; exit 1; }}
echo "prepare.sh: tests/pdf_cache holds $(ls tests/pdf_cache | wc -l) files"

python -c "import pathlib, pypdf; assert pathlib.Path(pypdf.__file__).resolve().parent == pathlib.Path('/home/{self.pr.repo}/pypdf')" || {{ echo "prepare.sh: pypdf does not import from the working tree"; exit 1; }}
python -m pytest --version || {{ echo "prepare.sh: pytest is missing"; exit 1; }}
python -c "import pytest_benchmark" || {{ echo "prepare.sh: pytest-benchmark is missing"; exit 1; }}
python -c "import PIL" || {{ echo "prepare.sh: pillow is missing"; exit 1; }}
if grep -qi '^pycryptodome==' requirements/ci-3.11.txt; then
  python -c "import Crypto" || {{ echo "prepare.sh: pycryptodome is pinned but does not import"; exit 1; }}
fi
if grep -qi '^cryptography==' requirements/ci-3.11.txt; then
  python -c "import cryptography" || {{ echo "prepare.sh: cryptography is pinned but does not import"; exit 1; }}
fi
if grep -qi '^pytest-socket==' requirements/ci-3.11.txt; then
  python -c "import pytest_socket" || {{ echo "prepare.sh: pytest-socket is pinned but does not import"; exit 1; }}
fi
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "PypdfImageDefault_PYPDF_3631_TO_1567 dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("py-pdf", "pypdf_3631_to_1567")
class PYPDF_3631_TO_1567(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PypdfImageDefault_PYPDF_3631_TO_1567(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()
            name = status = None
            m = _VERBOSE_RE.match(line)
            if m:
                name, status = m.group(1), m.group(2)
            else:
                m = _SUMMARY_RE.match(line)
                if m:
                    status, name = m.group(1), m.group(2)
            if not name:
                continue
            if status == "PASSED":
                passed.add(name)
            elif status in ("FAILED", "ERROR"):
                failed.add(name)
            else:
                skipped.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
