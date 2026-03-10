import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageDefault(Image):
    """Docker image for kaiko-ai/eva across all LHT version ranges (v0.0.1 through v0.4.3).

    Uses python:3.10-slim as base and relies on the project's own pyproject.toml
    (via `pip install -e .[all]`) to resolve the correct dependency versions at
    each base commit. This single image definition works for all 12 validated
    LHT instances because:
      - Python >=3.10 is required across all versions
      - The pyproject.toml at each commit pins the right deps for that version
      - The pytest configuration (testpaths, addopts) is stable across versions
      - System dependencies (openslide-tools, git-lfs) are needed from v0.1.0+
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

    def dependency(self) -> str:
        return "python:3.10-slim"

    def image_prefix(self) -> str:
        return "mswebench"

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
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
source /home/venv/bin/activate
export TORCH_FORCE_WEIGHTS_ONLY_LOAD=1
pytest -vv --no-cov tests/eva/core tests/eva/vision
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
    git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch
fi
source /home/venv/bin/activate
export TORCH_FORCE_WEIGHTS_ONLY_LOAD=1
pytest -vv --no-cov tests/eva/core tests/eva/vision
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
    git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch
fi
git -C /home/{pr.repo} apply --whitespace=nowarn /home/fix.patch
source /home/venv/bin/activate
export TORCH_FORCE_WEIGHTS_ONLY_LOAD=1
pytest -vv --no-cov tests/eva/core tests/eva/vision
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# System dependencies required by the project
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    git-lfs \\
    build-essential \\
    openslide-tools \\
    libgl1-mesa-glx \\
    libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/* \\
    && git lfs install

WORKDIR /home/

# Clone repo and checkout base commit
RUN git clone https://github.com/kaiko-ai/eva.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}
RUN git lfs pull

# Create venv and install project dependencies from the checked-out pyproject.toml
# This ensures correct dep versions for each base commit automatically
RUN python -m venv /home/venv
RUN /home/venv/bin/pip install --upgrade pip setuptools wheel
RUN /home/venv/bin/pip install -e .[all]
RUN /home/venv/bin/pip install pytest pytest-cov

{copy_commands}

""".format(pr=self.pr, copy_commands=copy_commands)


@Instance.register("kaiko-ai", "eva")
class EVA(Instance):
    """Evaluation instance for kaiko-ai/eva.

    Handles all 12 LHT instances across versions 0.0.1 through 0.4.3.
    The Instance.create() lookup resolves to "kaiko-ai/eva" because all
    dataset entries have empty number_interval and empty tag.
    """

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
        """Parse pytest verbose output for PASSED/FAILED/SKIPPED test results.

        Handles both formats seen in pytest -vv output:
          - tests/path/file.py::test_name PASSED
          - PASSED tests/path/file.py::test_name
        Strips ANSI color codes before matching.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Remove ANSI color codes
        log_clean = re.sub(r"\x1b\[.*?m", "", log)

        # Match: test_path STATUS  or  STATUS test_path
        pattern = re.compile(
            r"(tests/[\w/.-]+\.py::[\w\[\]._-]+)\s+(PASSED|FAILED|SKIPPED|ERROR)|"
            r"(PASSED|FAILED|SKIPPED|ERROR)\s+(tests/[\w/.-]+\.py::[\w\[\]._-]+)"
        )

        for match in pattern.finditer(log_clean):
            if match.group(1):
                test_name = match.group(1).strip()
                status = match.group(2)
            elif match.group(3):
                status = match.group(3)
                test_name = match.group(4).strip()
            else:
                continue

            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
