import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageDefault(Image):
    """Docker image for kaiko-ai/eva across all 12 LHT instances (v0.0.1 through v0.4.3).

    Uses python:3.10-slim with pdm + nox — matching the repo's actual build
    system (pdm-backend) and test runner (noxfile.py).  The nox sessions
    ``test_core`` and ``test_vision`` exist across every version in the range.
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
                "filter_patch.py",
                (
                    "import re, sys\n"
                    "\n"
                    "if len(sys.argv) < 2:\n"
                    "    sys.exit(1)\n"
                    "\n"
                    "try:\n"
                    "    content = open(sys.argv[1]).read()\n"
                    "except (IOError, OSError):\n"
                    "    sys.exit(1)\n"
                    "\n"
                    "if not content.strip():\n"
                    "    sys.exit(1)\n"
                    "\n"
                    "parts = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)\n"
                    "filtered = [p for p in parts if p.strip() and 'Binary files' not in p]\n"
                    "result = ''.join(filtered)\n"
                    "\n"
                    "if result.strip():\n"
                    "    sys.stdout.write(result)\n"
                    "else:\n"
                    "    sys.exit(1)\n"
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
git lfs pull || true
export PDM_IGNORE_SAVED_PYTHON=1
unset PYTHONPATH
nox -s test_core test_vision -- -vv --no-cov
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
if [ -s /home/test.patch ]; then
    python3 /home/filter_patch.py /home/test.patch > /tmp/filtered_test.patch 2>/dev/null && \
        git apply --whitespace=nowarn /tmp/filtered_test.patch || true
fi
git lfs pull || true
export PDM_IGNORE_SAVED_PYTHON=1
unset PYTHONPATH
nox -s test_core test_vision -- -vv --no-cov
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
if [ -s /home/test.patch ]; then
    python3 /home/filter_patch.py /home/test.patch > /tmp/filtered_test.patch 2>/dev/null && \
        git apply --whitespace=nowarn /tmp/filtered_test.patch || true
fi
if [ -s /home/fix.patch ]; then
    python3 /home/filter_patch.py /home/fix.patch > /tmp/filtered_fix.patch 2>/dev/null && \
        git apply --whitespace=nowarn /tmp/filtered_fix.patch || true
fi
git lfs pull || true
export PDM_IGNORE_SAVED_PYTHON=1
unset PYTHONPATH
nox -s test_core test_vision -- -vv --no-cov
""".format(repo=self.pr.repo),
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
    build-essential \\
    pkg-config \\
    libhdf5-dev \\
    openslide-tools \\
    libgl1 \\
    libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/*

# git-lfs binary segfaults under QEMU cross-arch emulation (amd64-on-arm64),
# so every git-lfs command must be fault-tolerant.
RUN apt-get update && (apt-get install -y --no-install-recommends git-lfs || true); \\
    rm -rf /var/lib/apt/lists/*; \\
    git lfs install || true

# Install pdm and nox (the repo's actual build/test toolchain)
RUN pip install --no-cache-dir pdm nox

WORKDIR /home/

# Clone repo and checkout base commit
RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {base_sha}
RUN git lfs pull || true

# Let pdm recognise the project so nox sessions can call `pdm install`
RUN pdm install --group test --group vision --no-self || pdm install --dev || true

{copy_commands}

""".format(
            org=self.pr.org,
            repo=self.pr.repo,
            base_sha=self.pr.base.sha,
            copy_commands=copy_commands,
        )


@Instance.register("kaiko-ai", "eva")
class EVA(Instance):
    """Evaluation instance for kaiko-ai/eva.

    Handles all 12 LHT instances across versions 0.0.1 through 0.4.3.
    Routes here when both number_interval and tag are empty.
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
        """Parse pytest verbose output embedded in nox session output.

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
