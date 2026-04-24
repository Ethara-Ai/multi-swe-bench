import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


CIRQ_3358_PR_TESTS = {
    3358: [
        "cirq/ion/convert_to_ion_gates_test.py",
        "cirq/optimizers/decompositions_test.py",
    ],
}


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
        return "python:3.8-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        test_files = CIRQ_3358_PR_TESTS.get(self.pr.number, [])
        test_files_str = " ".join(test_files)
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
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
cd /home/[[REPO_NAME]]
pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
pip install numpy==1.21.6 scipy==1.7.3 sympy==1.9 networkx==2.6.3 matplotlib==3.5.3 pandas==1.3.5
###ACTION_DELIMITER###
pip install protobuf==3.20.3 'google-api-core[grpc]<2' google-auth==1.20.1
###ACTION_DELIMITER###
pip install sortedcontainers==2.4.0 typing-extensions==4.1.1 mypy-extensions==0.4.4
###ACTION_DELIMITER###
pip install -e . --no-deps
###ACTION_DELIMITER###
pip install pytest
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
cd /home/[[REPO_NAME]]
python -m pytest [[TEST_FILES]] --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""".replace("[[REPO_NAME]]", repo_name).replace("[[TEST_FILES]]", test_files_str),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest [[TEST_FILES]] --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""".replace("[[REPO_NAME]]", repo_name).replace("[[TEST_FILES]]", test_files_str),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest [[TEST_FILES]] --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""".replace("[[REPO_NAME]]", repo_name).replace("[[TEST_FILES]]", test_files_str),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = f"""
FROM python:3.8-slim

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content


@Instance.register("quantumlib", "Cirq_3358_to_3358")
class CIRQ_3358_TO_3358(Instance):
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

        for line in log.split("\n"):
            line = line.strip()
            if line.startswith("PASSED "):
                test_name = line[len("PASSED "):].strip()
                passed_tests.add(test_name)
            elif line.startswith("FAILED "):
                test_name = line[len("FAILED "):].strip()
                if " - " in test_name:
                    test_name = test_name.split(" - ")[0]
                failed_tests.add(test_name)
            elif line.startswith("SKIPPED "):
                test_name = line[len("SKIPPED "):].strip()
                skipped_tests.add(test_name)
            else:
                # pytest -rA summary format: "test_path::test_name PASSED"
                match = re.match(r"^(.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL)\s*(\[.*\])?$", line)
                if match:
                    test_name = match.group(1)
                    status = match.group(2)
                    if status == "PASSED":
                        passed_tests.add(test_name)
                    elif status in ("FAILED", "ERROR"):
                        failed_tests.add(test_name)
                    elif status == "SKIPPED":
                        skipped_tests.add(test_name)
                    elif status == "XFAIL":
                        passed_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
