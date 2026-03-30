import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class Urllib3ToxImage(Image):
    """
    Handles TOX-era urllib3 (v1.19 - v1.25.1, PRs 1089-1580).

    Three sub-eras:
    - NOSE (v1.19-v1.21.1): flat layout, nosetests, nose+mock deps
    - EARLY_PYTEST (v1.22-v1.24): flat layout, pytest==3.1.0
    - LATE_PYTEST (v1.25-v1.25.1): src/ layout, pytest==3.8.2

    Runtime detection via directory structure (src/ exists?) and
    dev-requirements.txt contents (nose present?).
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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        return """ls -la
###ACTION_DELIMITER###
apt-get update && apt-get install -y libssl-dev libbrotli-dev libzstd-dev make
###ACTION_DELIMITER###
python -m venv venv && source venv/bin/activate && pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
source venv/bin/activate && pip install -r dev-requirements.txt
###ACTION_DELIMITER###
source venv/bin/activate && pip install -e ".[socks,secure]" 2>/dev/null || source venv/bin/activate && pip install -e .
###ACTION_DELIMITER###
source venv/bin/activate && pip install nose mock 2>/dev/null || true
###ACTION_DELIMITER###
source venv/bin/activate && pip install pytest pytest-xdist 2>/dev/null || true
###ACTION_DELIMITER###
source venv/bin/activate && pip install PySocks pyOpenSSL ndg-httpsclient pyasn1 2>/dev/null || true
###ACTION_DELIMITER###
source venv/bin/activate && pip install trustme 2>/dev/null || true
###ACTION_DELIMITER###
if [ -d "src" ]; then echo "SRC_LAYOUT=true"; else echo "FLAT_LAYOUT=true"; fi
###ACTION_DELIMITER###
source venv/bin/activate && pytest --version
###ACTION_DELIMITER###
if [ -f "setup.cfg" ]; then sed -i 's/\\[pytest\\]/\\[tool:pytest\\]/g' setup.cfg 2>/dev/null || true; fi
###ACTION_DELIMITER###
if [ -d "src" ]; then source venv/bin/activate && PYTHONPATH=src pytest -v --co test/ 2>&1 | head -20; else source venv/bin/activate && pytest -v --co test/ 2>&1 | head -20; fi"""

    def _test_cmd(self) -> str:
        return """if [ -d "src" ]; then
    source venv/bin/activate
    PYTHONPATH=src pytest -v -n auto test/
else
    source venv/bin/activate
    pytest -v -n auto test/
fi"""

    def files(self) -> list[File]:
        test_cmd = self._test_cmd()
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", self._prepare_sh()),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=test_cmd),
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
{test_cmd}

""".format(pr=self.pr, test_cmd=test_cmd),
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
{test_cmd}

""".format(pr=self.pr, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.9-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git make

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/urllib3/urllib3.git /home/urllib3

WORKDIR /home/urllib3
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN apt-get update && apt-get install -y libssl-dev libbrotli-dev libzstd-dev
RUN python -m venv venv && . venv/bin/activate && pip install --upgrade pip setuptools wheel
RUN . venv/bin/activate && pip install -r dev-requirements.txt || true
RUN . venv/bin/activate && pip install -e ".[socks,secure]" || . venv/bin/activate && pip install -e . || true
RUN . venv/bin/activate && pip install nose mock pytest pytest-xdist PySocks pyOpenSSL trustme 2>/dev/null || true
RUN if [ -f "setup.cfg" ]; then sed -i 's/\\[pytest\\]/\\[tool:pytest\\]/g' setup.cfg; fi || true
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("urllib3", "urllib3_1580_to_1089")
class Urllib3_1580_to_1089(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Urllib3ToxImage(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_matches = re.findall(r"PASSED (test/.*::.*?)\s*$", log, re.MULTILINE)
        passed_tests.update(passed_matches)
        failed_matches = re.findall(r"FAILED (test/.*::.*?)\s*$", log, re.MULTILINE)
        failed_tests.update(failed_matches)
        skipped_matches = re.findall(
            r"SKIPPED (?:\[\d+\] )?(test/(?:.*?::.*?|.*?\.py:\d+))(?:[:\s]|$)",
            log,
            re.MULTILINE,
        )
        skipped_tests.update(skipped_matches)

        xdist_pattern = (
            r"\[gw\d+\] \[\s*\d+%\] (SKIPPED|PASSED|FAILED) ([\w\/:\.-]+)\s*$"
        )
        xdist_matches = re.findall(xdist_pattern, log, re.MULTILINE)
        for status, test_name in xdist_matches:
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
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
