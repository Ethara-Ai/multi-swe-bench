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
        return "python:2.7-slim"

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
                """pip install --upgrade "pip<21" "setuptools<45"
###ACTION_DELIMITER###
pip install tox
###ACTION_DELIMITER###
python setup.py develop
###ACTION_DELIMITER###
pip install --force-reinstall "attrs==17.4.0" "pluggy==0.6.0" "py==1.11.0" "six==1.17.0" "more-itertools==4.3.0" "funcsigs==1.0.2" "pathlib2==2.3.7.post1" "hypothesis==3.56.0"
###ACTION_DELIMITER###
echo 'python -m pytest -rA --tb=short -v testing/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
python -m pytest -rA --tb=short -v testing/

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
python -m pytest -rA --tb=short -v testing/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest -rA --tb=short -v testing/

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:2.7-slim

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8

RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \\
    sed -i '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y git build-essential

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/pytest-dev/pytest.git /home/pytest

WORKDIR /home/pytest
RUN git reset --hard
RUN git fetch origin {pr.base.sha} 2>/dev/null || git fetch --unshallow 2>/dev/null || true
RUN git checkout {pr.base.sha}

RUN pip install --upgrade "pip<21" "setuptools<45"
RUN pip install tox
RUN python setup.py develop || true
RUN pip install --force-reinstall "attrs==17.4.0" "pluggy==0.6.0" "py==1.11.0" "six==1.17.0" "more-itertools==4.3.0" "funcsigs==1.0.2" "pathlib2==2.3.7.post1" "hypothesis==3.56.0"

RUN python -c "import pytest; print('pytest', pytest.__version__)" || echo "WARNING: pytest import check failed"
"""
        dockerfile_content += f"""
{copy_commands}
"""
        dockerfile_content += """
CMD ["/bin/bash"]
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("pytest-dev", "pytest_3642_to_2068")
class PYTEST_3642_TO_2068(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes before parsing
        log = re.sub(r'\x1b\[[0-9;]*m', '', log)

        pattern = re.compile(
            r"^\s*(?:([^\s]+)\s+(PASSED|FAILED|SKIPPED)|(PASSED|FAILED|SKIPPED)\s+([^\s]+))(?:\s+\[.*?\])?\s*$",
            re.MULTILINE,
        )
        for match in pattern.finditer(log):
            test_name = (match.group(1) or match.group(4)).strip()
            status = match.group(2) or match.group(3)
            # Filter out inner subprocess test output — real tests start with "testing/"
            if not test_name.startswith("testing/"):
                continue
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Deduplicate: if a test appears in both passed and failed, keep only failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
