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
        return "python:3.9-slim"

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
                """ls
###ACTION_DELIMITER###
pip install 'setuptools<60' 'numpy==1.22.4' 'Cython>=0.29.21,<3.0' wheel
###ACTION_DELIMITER###
apt-get update && apt-get install -y --no-install-recommends build-essential
###ACTION_DELIMITER###
pip install --no-build-isolation .
###ACTION_DELIMITER###
python setup.py build_ext --inplace
###ACTION_DELIMITER###
pip install pooch
###ACTION_DELIMITER###
python -m pytest -v skimage --co -q 2>&1 | head -20
###ACTION_DELIMITER###
python -m pytest -v skimage -x --timeout=60 2>&1 | tail -30""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
python -m pytest -v skimage --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
python -m pytest -v skimage --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
python -m pytest -v skimage --continue-on-collection-errors

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """FROM python:3.9-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/scikit-image/scikit-image.git /home/scikit-image

WORKDIR /home/scikit-image

RUN pip install 'setuptools<60' 'numpy==1.22.4' 'Cython>=0.29.21,<3.0' wheel
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
RUN pip install --no-build-isolation .
RUN python setup.py build_ext --inplace
RUN pip install pooch pytest
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)

@Instance.register("scikit-image", "scikit_image_3226")
class SCIKIT_IMAGE_3226(Instance):
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
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped
        import re

        # Regex patterns to match test lines
        pattern1 = re.compile(r"^(.*?)\s+(PASSED|FAILED|SKIPPED)\s*(\[\s*\d+%\s*\])?$")
        pattern2 = re.compile(r"^(PASSED|FAILED|SKIPPED)\s*:\s*(.*?)(\s+-\s.*)?$")
        for line in log.split("\n"):
            line = line.strip()
            line = re.sub(r'\x1b\[[0-9;]*m', '', line)
            match1 = pattern1.match(line)
            if match1:
                test_name = match1.group(1).strip()
                status = match1.group(2).upper()
            else:
                match2 = pattern2.match(line)
                if match2:
                    status = match2.group(1).upper()
                    test_name = match2.group(2).strip()
                else:
                    continue
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
