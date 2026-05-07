import re
from typing import Optional, Union

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
        return "python:3.11-slim"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return f"/home/{self.pr.repo}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""\
FROM {self.dependency()}
{self.global_env}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \\
    git curl wget \\
    gcc g++ python3-dev \\
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN uv sync || true
RUN (uv run patchright install chromium --with-deps 2>/dev/null || uv run playwright install chromium --with-deps 2>/dev/null) || true
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
        return f"/home/{self.pr.repo}"

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
                f"""#!/bin/bash
set -e
cd /home/{self.pr.repo}
git reset --hard
git checkout {self.pr.base.sha}
uv sync || true
(uv run patchright install chromium --with-deps 2>/dev/null || uv run playwright install chromium --with-deps 2>/dev/null) || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
uv run pytest -v
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
uv run pytest -v
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
uv run pytest -v
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


@Instance.register("browser-use", "browser-use")
class BrowserUse(Instance):
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
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest verbose output patterns:
        # test_file.py::test_name PASSED  [ 50%]
        # test_file.py::test_name FAILED  [ 75%]
        # test_file.py::test_name SKIPPED [ 25%]
        passed_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(.+?)\s+PASSED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        passed_tests.update(passed_pattern.findall(clean_log))

        failed_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?FAILED\s+(.+?)(?:\s+-.*)?$", re.MULTILINE
        )
        failed_tests.update(failed_pattern.findall(clean_log))

        skipped_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(.+?)\s+SKIPPED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        skipped_tests.update(skipped_pattern.findall(clean_log))

        # Dedup: worst result wins
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
