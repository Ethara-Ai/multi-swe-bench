import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# openai/openai-agents-python  --  Era 2: PRs #2460 .. #99999
#
# Toolchain (verified in Docker against base commits of PR #2472, #3311):
#   * package manager : uv (astral)            -- unchanged across the era
#   * requires-python : >=3.10                 -- era boundary marker
#   * test framework  : pytest (asyncio_mode=auto, xdist available)
#   * base image      : python:3.11-slim       -- satisfies >=3.10
#
# Era boundary: `requires-python` flips 3.9 -> 3.10 between PR #2456 (3.9) and
# PR #2472 (3.10); the cutoff is fixed at #2460.  Era 1 lives in
# `openai_agents_python_2459_to_1.py`.
#
# Docker-discovered requirements:
#   * apt: git curl wget ca-certificates build-essential linux-libc-dev rclone
#     - linux-libc-dev  -> kernel uapi headers needed to build `evdev`
#                          (pulled transitively via the `pynput` dev dep);
#                          without it `uv sync` aborts the whole environment.
#     - rclone          -> required by tests/sandbox integration tests
#                          (e.g. test_runner_pause_resume).
#   * install: `uv sync --all-extras --all-packages --group dev`
#              (plain `uv sync` omits litellm/voice/sqlalchemy extras and
#               yields ~13 test-collection ImportErrors)
#   * tests need a dummy OPENAI_API_KEY; without it the SDK raises
#     openai.OpenAIError at client construction and most tests error out.
# ---------------------------------------------------------------------------


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
        return "base-99999-to-2460"

    def workdir(self) -> str:
        return "base-99999-to-2460"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""\
FROM {self.dependency()}
{self.global_env}
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl wget ca-certificates \\
    build-essential gcc g++ python3-dev \\
    linux-libc-dev rclone \\
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN uv sync --all-extras --all-packages --group dev || uv sync || true
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
git clean -fdx -e .venv
git checkout {self.pr.base.sha}
uv sync --all-extras --all-packages --group dev || uv sync || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
uv run pytest -v
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
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
export OPENAI_API_KEY=sk-fake-key-for-testing
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


@Instance.register("openai", "openai-agents-python_99999_to_2460")
class OPENAI_AGENTS_PYTHON_99999_TO_2460(Instance):
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

        # pytest -v verbose output, e.g.:
        #   tests/test_agent_config.py::test_system_instructions PASSED  [  0%]
        #   tests/test_agent_hooks.py::test_streamed_agent_hooks FAILED  [  2%]
        #   tests/extensions/memory/test_redis_session.py::test_x SKIPPED [ 11%]
        passed_pattern = re.compile(
            r"^(.+?)\s+PASSED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        passed_tests.update(passed_pattern.findall(clean_log))

        skipped_pattern = re.compile(
            r"^(.+?)\s+SKIPPED\s+(?:\[\s*\d+%\s*\]|\[\d+\])", re.MULTILINE
        )
        skipped_tests.update(skipped_pattern.findall(clean_log))

        # Inline verbose failure line: "<nodeid> FAILED [ 2%]"
        failed_inline = re.compile(
            r"^(.+?)\s+FAILED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        failed_tests.update(failed_inline.findall(clean_log))

        # Summary section: "FAILED <nodeid> - <reason>" / "ERROR <nodeid>"
        failed_summary = re.compile(
            r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE
        )
        failed_tests.update(failed_summary.findall(clean_log))

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
