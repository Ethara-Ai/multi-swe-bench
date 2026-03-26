from __future__ import annotations
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "python:3.10-slim"

    def image_prefix(self) -> str:
        return "envagent"

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
                """#!/bin/bash
set -e
cd /home/kedro-plugins/kedro-datasets
pip install -e ".[plotly]" || true
pip install "pytest~=7.2" "pytest-cov~=3.0" "pytest-mock>=1.7.1,<2.0" "pytest-xdist[psutil]~=2.2.1" || true
pip install adlfs "gcsfs>=2023.1,<2023.7" "s3fs>=2021.04" || true
pip install matplotlib || true
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}/kedro-datasets
pytest tests/plotly -v --numprocesses 4 --dist loadfile --no-cov

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
cd /home/{pr.repo}/kedro-datasets
pytest tests/plotly -v --numprocesses 4 --dist loadfile --no-cov

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
cd /home/{pr.repo}/kedro-datasets
pytest tests/plotly -v --numprocesses 4 --dist loadfile --no-cov

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/kedro-plugins

WORKDIR /home/kedro-plugins
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}
"""
        dockerfile_content += f"""
{copy_commands}

RUN bash /home/prepare.sh
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("kedro-org", "kedro-plugins")
class KEDRO_PLUGINS(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped

        # Strip ANSI color codes from log output
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # Regex pattern to match test results (PASSED, FAILED, SKIPPED)
        # Tests run with --numprocesses 4 (pytest-xdist), output format:
        # [gw0] [ 10%] PASSED tests/plotly/test_json_dataset.py::TestJSONDataset::test_save_and_load
        pattern = re.compile(
            r"^\[gw\d+\]\s+\[\s*\d+%\]\s+(PASSED|FAILED|SKIPPED)\s+(tests/[\w/.:\[\]:-]+)",
            re.IGNORECASE | re.MULTILINE,
        )
        matches = pattern.findall(log)
        for status, test_name in matches:
            status_upper = status.upper()
            if status_upper == "PASSED":
                passed_tests.add(test_name)
            elif status_upper == "FAILED":
                failed_tests.add(test_name)
            elif status_upper == "SKIPPED":
                skipped_tests.add(test_name)
        # Dedup: if a test appears as both passed and failed (retry), remove from passed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
