import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "mswebench"

    def image_tag(self) -> str:
        return "base-py39"

    def workdir(self) -> str:
        return "base-py39"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential python3-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{self.global_env}

{code}

{self.clear_env}

"""


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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

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
                """git reset --hard
###ACTION_DELIMITER###
git checkout {pr.base.sha}
###ACTION_DELIMITER###
pip install -e . || true
###ACTION_DELIMITER###
pip install pytest pytest-timeout || true
###ACTION_DELIMITER###
echo 'python3 -m pytest psutil/tests/ -v --no-header -rA --tb=no --continue-on-collection-errors -o addopts= --timeout=300 --timeout-method=thread --deselect psutil/tests/test_process.py::TestProcess::test_cpu_affinity_all_combinations --deselect psutil/tests/test_process.py::LimitedUserTestCase::test_cpu_affinity_all_combinations --deselect psutil/tests/test_process_all.py::TestFetchAllProcesses::test_all' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh || true""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
python3 -m pytest psutil/tests/ -v --no-header -rA --tb=no --continue-on-collection-errors -o addopts= --deselect psutil/tests/test_process.py::TestProcess::test_cpu_affinity_all_combinations --deselect psutil/tests/test_process.py::LimitedUserTestCase::test_cpu_affinity_all_combinations --deselect psutil/tests/test_process_all.py::TestFetchAllProcesses::test_all

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
find /home/{pr.repo} -name '*.so' -delete
find /home/{pr.repo} -name '*.pyc' -delete
find /home/{pr.repo} -name '__pycache__' -type d -exec rm -rf {{}} + 2>/dev/null
pip install -e . || true
python3 -m pytest psutil/tests/ -v --no-header -rA --tb=no --continue-on-collection-errors -o addopts= --deselect psutil/tests/test_process.py::TestProcess::test_cpu_affinity_all_combinations --deselect psutil/tests/test_process.py::LimitedUserTestCase::test_cpu_affinity_all_combinations --deselect psutil/tests/test_process_all.py::TestFetchAllProcesses::test_all

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
find /home/{pr.repo} -name '*.so' -delete
find /home/{pr.repo} -name '*.pyc' -delete
find /home/{pr.repo} -name '__pycache__' -type d -exec rm -rf {{}} + 2>/dev/null
pip install -e . || true
python3 -m pytest psutil/tests/ -v --no-header -rA --tb=no --continue-on-collection-errors -o addopts= --deselect psutil/tests/test_process.py::TestProcess::test_cpu_affinity_all_combinations --deselect psutil/tests/test_process.py::LimitedUserTestCase::test_cpu_affinity_all_combinations --deselect psutil/tests/test_process_all.py::TestFetchAllProcesses::test_all

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("giampaolo", "psutil_2680_to_950")
class PSUTIL_2680_TO_950(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # "psutil/tests/test_system.py::TestCPU::test_count PASSED [  2%]"
        pattern1 = re.compile(
            r"^(psutil/tests/[\w\/\.\-::\[\]]+)\s+"
            r"(passed|failed|skipped|xfailed|xfail) "
            r"\s*\[.+\]$",
            re.MULTILINE | re.IGNORECASE,
        )
        # "PASSED psutil/tests/test_system.py::TestCPU::test_count"
        pattern2 = re.compile(
            r"(passed|failed|skipped|xfailed|xfail) "
            r"(psutil/tests/[\w\/\.\-::\[\]]+)",
            re.MULTILINE | re.IGNORECASE,
        )
        for match in pattern1.finditer(log):
            test_name = match.group(1)
            status = match.group(2)
            if status.upper() == "PASSED":
                passed_tests.add(test_name)
            elif status.upper() == "FAILED":
                failed_tests.add(test_name)
            elif status.upper() in ("SKIPPED", "XFAILED", "XFAIL"):
                skipped_tests.add(test_name)
        for match in pattern2.finditer(log):
            status = match.group(1)
            test_name = match.group(2)
            if status.upper() == "PASSED":
                passed_tests.add(test_name)
            elif status.upper() == "FAILED":
                failed_tests.add(test_name)
            elif status.upper() in ("SKIPPED", "XFAILED", "XFAIL"):
                skipped_tests.add(test_name)
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
