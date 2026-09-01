import re
import json
from typing import Optional, Union

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
        repo_name = self.pr.repo
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
                """ls
###ACTION_DELIMITER###
pip install pytest
###ACTION_DELIMITER###
echo 'pytest tests/ -vv -s' > test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install click
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install mock
###ACTION_DELIMITER###
pip install pytest-asyncio
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install pytest-mock
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
pytest tests/ -vv -s

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest tests/ -vv -s

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest tests/ -vv -s

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

# Choose an appropriate base image based on the project's requirements - replace [base image] with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM python:3.10-slim

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git build-essential python3-dev

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/AgnostiqHQ/covalent.git /home/covalent

WORKDIR /home/covalent
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
RUN bash /home/prepare.sh || true
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("AgnostiqHQ", "covalent_951_to_817")
class COVALENT_951_TO_817(Instance):
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
        import re

        # ANSI first: pytest colourises status words, and a stray escape makes
        # every anchor below miss.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # IDs keep the FILE PATH, not just the name after "::".
        #
        # The bare-name form was a real collision, observed in this repo's own
        # output at PR 902:
        #     tests/covalent_dispatcher_tests/_cli/service_test.py::test_cluster_status_cli PASSED
        #     tests/functional_tests/dask_cluster_cli_test.py::test_cluster_status_cli      SKIPPED
        # Two distinct tests in two files collapsed to one id, landing in both
        # passed_tests and skipped_tests, and TestResult.__post_init__ rejects
        # overlapping sets — so parse_log raised ValueError and the instance
        # died. Capturing "<file>::<test>" makes the id unique per test.
        status_re = re.compile(
            r"^(?P<id>\S+\.py::\S+?)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b",
            re.MULTILINE,
        )
        for m in status_re.finditer(log):
            tid, status = m.group("id"), m.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(tid)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(tid)
            else:  # SKIPPED, XFAIL
                skipped_tests.add(tid)

        # Summary lines ("FAILED <id> - reason") catch anything the per-line
        # status form missed, e.g. a test whose failure is only reported in the
        # short summary.
        for m in re.finditer(r"^(?:FAILED|ERROR)\s+(\S+\.py::\S+)", log, re.MULTILINE):
            failed_tests.add(m.group(1).rstrip(":"))

        # TestResult.__post_init__ enforces disjoint sets. Failure wins over a
        # retry that later passed, then skip.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
