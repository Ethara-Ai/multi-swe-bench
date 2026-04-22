import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


TEST_CMD = "python -m pytest --no-header -rA --tb=no -p no:cacheprovider --timeout=30 --continue-on-collection-errors --ignore=opsdroid/database/redis/tests --ignore=opsdroid/connector/teams/tests"


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
        return "python:3.8-bookworm"

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
                f"""ls -F
###ACTION_DELIMITER###
pip install -e ".[all,test]"
###ACTION_DELIMITER###
{TEST_CMD}
###ACTION_DELIMITER###
echo '{TEST_CMD}' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -o pipefail
cd /home/{self.pr.repo}
timeout 300 {TEST_CMD} || true

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -o pipefail
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch; then
    if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn --3way /home/test.patch; then
        echo "Error: git apply failed" >&2
        exit 1
    fi
fi
timeout 300 {TEST_CMD} || true

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -o pipefail
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch || true
    if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn --3way /home/fix.patch; then
        echo "Error: git apply failed" >&2
        exit 1
    fi
fi
pip install -e ".[all,test]" 2>/dev/null || true
timeout 300 {TEST_CMD} || true

""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.8-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
RUN git clone https://github.com/opsdroid/opsdroid.git /home/opsdroid

WORKDIR /home/opsdroid
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install --upgrade pip
RUN pip install -e ".[all,test]"
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("opsdroid", "opsdroid_2054_to_1628")
class OPSDROID_2054_TO_1628(Instance):
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
        log = re.sub(r'\x1b\[[0-9;]*m', '', log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        test_results = {}
        pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED(?: \[[\d]+\])?|XFAIL|XPASS)\s+(.+?)(?:\s+-\s+.*)?$"
        )
        for line in log.splitlines():
            line = line.strip()
            match = pattern.match(line)
            if match:
                status = match.group(1)
                test_name = match.group(2).strip()
                if "FAIL" in status or "ERROR" in status:
                    test_results[test_name] = "failed"
                elif "SKIP" in status:
                    if test_results.get(test_name) != "failed":
                        test_results[test_name] = "skipped"
                elif "PASS" in status:
                    if test_results.get(test_name) not in ["failed", "skipped"]:
                        test_results[test_name] = "passed"
        for test_name, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_name)
            elif status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
