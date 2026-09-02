import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.python.mem0ai.mem0 import (
    ImageBase,
    pr_dockerfile,
    register_era,
)

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

register_era("mem0_4917_to_2367", min_version=(0, 1, 101), min_anchor=2738)


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v --no-header -rA output (mem0 / embedchain test layout)."""
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    pattern_status_after = re.compile(
        r"^((?:tests|embedchain/tests)/.*)::(.*) (PASSED|FAILED|ERROR|SKIPPED|XFAIL)"
    )
    pattern_failed = re.compile(
        r"^FAILED ((?:tests|embedchain/tests)/.*?)::(.*?)(?= - |$)"
    )

    for line in log.splitlines():
        line = ANSI_ESCAPE.sub("", line).strip()
        match_status_after = pattern_status_after.match(line)
        if match_status_after:
            test_path = match_status_after.group(1)
            test_name = match_status_after.group(2)
            status = match_status_after.group(3)
            full_test_name = f"{test_path}::{test_name}"
            if status == "PASSED":
                passed_tests.add(full_test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(full_test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(full_test_name)
            continue
        match_failed = pattern_failed.match(line)
        if match_failed:
            test_path = match_failed.group(1)
            test_name = match_failed.group(2)
            full_test_name = f"{test_path}::{test_name}"
            failed_tests.add(full_test_name)

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
                """#!/bin/bash
set -e
cd /home/{pr.repo}
###ACTION_DELIMITER###
git reset --hard
###ACTION_DELIMITER###
git clean -fd
###ACTION_DELIMITER###
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
###ACTION_DELIMITER###
git checkout {pr.base.sha}
###ACTION_DELIMITER###
test -z "$(git status --porcelain)"
###ACTION_DELIMITER###
bash /home/install-deps.sh
###ACTION_DELIMITER###
echo 'pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o addopts=' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh || true""".format(pr=self.pr),
            ),
            File(
                ".",
                "install-deps.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel poetry-core hatchling poetry || true
###ACTION_DELIMITER###
poetry config virtualenvs.create false 2>/dev/null || true
###ACTION_DELIMITER###
pip install -e ".[test]" 2>/dev/null || pip install -e ".[dev,test]" 2>/dev/null || pip install -e ".[dev]" 2>/dev/null || pip install -e . 2>/dev/null || true
###ACTION_DELIMITER###
pip install pytest pytest-mock pytest-asyncio pytest-env || true
###ACTION_DELIMITER###
pip install litellm google-generativeai vertexai ollama groq together huggingface_hub chromadb || true""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export no_proxy=
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch 2>/dev/null || git -C /home/{pr.repo} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export no_proxy=
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch 2>/dev/null || git -C /home/{pr.repo} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
git -C /home/{pr.repo} apply --whitespace=nowarn /home/fix.patch 2>/dev/null || git -C /home/{pr.repo} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export no_proxy=
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self)


@Instance.register("mem0ai", "mem0_4917_to_2367")
class MEM0_HATCHLING(Instance):
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
        return parse_pytest_log(log)
