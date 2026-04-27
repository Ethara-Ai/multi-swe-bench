import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageDefault_3734_2785(Image):
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
        return "python:3.8-slim"

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
apt-get update && apt-get install -y nodejs npm curl libxml2-dev libxslt-dev gcc g++ pkg-config zlib1g-dev || true
###ACTION_DELIMITER###
npm install -g n && n lts || true
###ACTION_DELIMITER###
hash -r
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel || true
###ACTION_DELIMITER###
pip install "werkzeug<2.1" "Flask<2.3" || true
###ACTION_DELIMITER###
pip install -e . || true
###ACTION_DELIMITER###
pip install -r requires-ci.txt 2>/dev/null || pip install -r requirements/ci.txt 2>/dev/null || true
###ACTION_DELIMITER###
pip install -r requires-testing.txt 2>/dev/null || pip install -r requirements/testing.txt 2>/dev/null || true
###ACTION_DELIMITER###
pip install mock six flaky flask-talisman numpy redis dash-dangerously-set-inner-html pytest pytest-mock multiprocess psutil 2>/dev/null || true
###ACTION_DELIMITER###
pip install "pytest>=4.6,<5" "pytest-mock<3.12" 2>/dev/null || true
###ACTION_DELIMITER###
cd /home/[[REPO_NAME]] && for comp in dash-test-components dash-generator-test-component-nested dash-generator-test-component-standard dash-generator-test-component-typescript; do if [ -d "@plotly/$comp" ]; then cd "@plotly/$comp" && npm ci 2>/dev/null && npm run build 2>/dev/null && pip install -e . 2>/dev/null; cd /home/[[REPO_NAME]]; fi; done || true
###ACTION_DELIMITER###
echo 'prepare done'""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
pytest tests/ -vv --ignore=tests/integration 2>&1; true

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest tests/ -vv --ignore=tests/integration 2>&1; true

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest tests/ -vv --ignore=tests/integration 2>&1; true

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# Dockerfile for plotly/dash PRs (Python 3.8 era, range 2785-3734)

FROM python:3.8-slim

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements including Node.js support
RUN apt-get update && apt-get install -y git curl libxml2-dev libxslt-dev gcc g++ pkg-config zlib1g-dev

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/plotly/dash.git /home/dash

WORKDIR /home/dash
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
RUN bash /home/prepare.sh || true
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("plotly", "dash_3734_to_2785")
class DASH_3734_TO_2785(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_3734_2785(self.pr, self._config)

    _DEP_ENSURE = 'pip uninstall -y pytest-rerunfailures pytest-sugar 2>/dev/null; pip install "setuptools<70" 2>/dev/null; pip install "werkzeug>=2.0" "Flask>=2.3,<3" "pytest>=4.6,<8" "pytest-mock<4" 2>/dev/null; pip install -e ".[dev]" 2>/dev/null; pip install -e . 2>/dev/null; pip install pyyaml mock six flaky flask-talisman numpy "pytest>=4.6,<8" "pytest-mock<4" dash-dangerously-set-inner-html selenium 2>/dev/null || true'
    _TEST_CMD = "pytest tests/ -vv --ignore=tests/integration --ignore=tests/test_integration.py 2>&1; true"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return f"bash -c 'cd /home/dash && {self._DEP_ENSURE} && {self._TEST_CMD}'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return f"bash -c 'cd /home/dash && git apply --whitespace=nowarn /home/test.patch && {self._DEP_ENSURE} && {self._TEST_CMD}'"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return f"bash -c 'cd /home/dash && git apply --whitespace=nowarn /home/test.patch /home/fix.patch && {self._DEP_ENSURE} && {self._TEST_CMD}'"

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        pattern = r"(tests/[^:]+::[^\s]+)\s+(PASSED|FAILED|ERROR|SKIPPED)|(PASSED|FAILED|ERROR|SKIPPED)\s+(tests/[^:]+::[^\s]+)"
        for line in log.splitlines():
            match = re.search(pattern, line)
            if not match:
                continue
            test = match.group(1) or match.group(4)
            status = match.group(2) or match.group(3)
            if not (test and status):
                continue
            if status == "PASSED":
                passed_tests.add(test)
            elif status in ["FAILED", "ERROR"]:
                failed_tests.add(test)
            elif status == "SKIPPED":
                skipped_tests.add(test)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
