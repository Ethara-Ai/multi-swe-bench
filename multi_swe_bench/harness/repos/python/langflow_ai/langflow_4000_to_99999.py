import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class LangflowUVImageDefault(Image):
    """Docker image for langflow PRs in the UV era (PR >= 4000).

    Uses uv for dependency management.
    Test paths: src/backend/tests, src/lfx/tests
    Python: 3.12
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
        return "python:3.12-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

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
                """ls -la
###ACTION_DELIMITER###
pip install uv
###ACTION_DELIMITER###
uv sync --no-dev 2>/dev/null || uv sync --no-dev --no-build-isolation 2>/dev/null || true
###ACTION_DELIMITER###
uv pip install pytest pytest-xdist pytest-asyncio pytest-mock pytest-timeout pytest-sugar pytest-instafail httpx respx asgi-lifespan blockbuster hypothesis 2>/dev/null || true
###ACTION_DELIMITER###
uv pip install -e . 2>/dev/null || pip install -e . 2>/dev/null || true""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo}
source .venv/bin/activate 2>/dev/null || true
pytest src/backend/tests/unit/ --no-header -rA --tb=no -p no:cacheprovider -v --timeout=60

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}
source .venv/bin/activate 2>/dev/null || true
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    git -C /home/{repo} apply --whitespace=nowarn --3way /home/test.patch || true
fi
pip install -e . 2>/dev/null || true
pytest src/backend/tests/unit/ --no-header -rA --tb=no -p no:cacheprovider -v --timeout=60

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}
source .venv/bin/activate 2>/dev/null || true
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    git -C /home/{repo} apply --whitespace=nowarn --3way /home/test.patch || true
fi
if ! git -C /home/{repo} apply --whitespace=nowarn /home/fix.patch; then
    git -C /home/{repo} apply --whitespace=nowarn --3way /home/fix.patch || true
fi
pip install -e . 2>/dev/null || true
pytest src/backend/tests/unit/ --no-header -rA --tb=no -p no:cacheprovider -v --timeout=60

""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install --upgrade pip && pip install uv || (curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH="$HOME/.local/bin:$PATH")
RUN uv sync --no-dev 2>/dev/null || uv sync --no-dev --no-build-isolation 2>/dev/null || true
RUN uv pip install pytest pytest-xdist pytest-asyncio pytest-mock pytest-timeout pytest-sugar pytest-instafail httpx respx asgi-lifespan blockbuster hypothesis faker 2>/dev/null || true
RUN uv pip install --no-deps --no-build-isolation -e . 2>/dev/null || pip install --no-deps -e . 2>/dev/null || true
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("langflow-ai", "langflow_4000_to_99999")
class LANGFLOW_4000_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LangflowUVImageDefault(self.pr, self._config)

    _PYTEST_CMD = (
        "source .venv/bin/activate 2>/dev/null ; "
        "pytest src/backend/tests/unit/ "
        "--no-header -rA --tb=short -p no:cacheprovider -v "
        "--timeout=60 "
        "-W ignore::DeprecationWarning"
    )

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash -c 'cd /home/{repo} ; {pytest}'".format(
            repo=self.pr.repo, pytest=self._PYTEST_CMD
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git apply --whitespace=nowarn /home/test.patch || "
            "patch -p1 --force --no-backup-if-mismatch < /home/test.patch || true ; "
            "pip install --no-deps -e src/backend/base 2>/dev/null || pip install --no-deps -e . 2>/dev/null || true ; "
            "{pytest}"
            "'".format(repo=self.pr.repo, pytest=self._PYTEST_CMD)
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git apply --whitespace=nowarn /home/test.patch || "
            "patch -p1 --force --no-backup-if-mismatch < /home/test.patch || true ; "
            "git apply --whitespace=nowarn /home/fix.patch || "
            "patch -p1 --force --no-backup-if-mismatch < /home/fix.patch || true ; "
            "pip install --no-deps -e src/backend/base 2>/dev/null || pip install --no-deps -e . 2>/dev/null || true ; "
            "{pytest}"
            "'".format(repo=self.pr.repo, pytest=self._PYTEST_CMD)
        )

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", log)
        lines = clean_log.splitlines()

        # Pytest -v output formats (captured from actual Docker run):
        #   Body:    src/backend/tests/unit/path::test_name PASSED [ XX%]
        #   -rA:     PASSED src/backend/tests/unit/path::test_name
        passed_patterns = [
            re.compile(r"^(\S+::\S+.*?)\s+PASSED"),
            re.compile(r"^PASSED\s+(\S+::\S+.*)$"),
        ]
        failed_patterns = [
            re.compile(r"^(\S+::\S+.*?)\s+FAILED"),
            re.compile(r"^FAILED\s+(\S+::\S+.*)$"),
        ]
        skipped_pattern = re.compile(
            r"^SKIPPED\s+(?:\[\d+\]\s+)?(.*?(?:::\S+|\.py:\d+))\s*",
        )

        for line in lines:
            line = line.strip()
            for pattern in passed_patterns:
                match = pattern.match(line)
                if match:
                    passed_tests.add(match.group(1).strip())
                    break
            else:
                for pattern in failed_patterns:
                    match = pattern.match(line)
                    if match:
                        failed_tests.add(match.group(1).strip())
                        break
                else:
                    match = skipped_pattern.match(line)
                    if match:
                        skipped_tests.add(match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
