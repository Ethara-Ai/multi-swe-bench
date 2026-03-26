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
        return "ubuntu:latest"

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

apt-get update
apt-get install -y python3 python3-pip python3.12-venv gfortran libopenblas-dev pkg-config

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip setuptools wheel

pip install 'numpy<2' 'scipy<1.14' 'matplotlib<3.8' pandas networkx sympy h5py jsonschema 'scikit-image<0.23'
pip install 'numba==0.60.0' 'llvmlite==0.43.0' pyamg tqdm transforms3d rich docrep
pip install chemicals thermo porespy
pip install pytest ipython

mkdir -p /tmp/pypardiso_stub
cat > /tmp/pypardiso_stub/setup.py << 'STUBEOF'
from setuptools import setup
setup(name="pypardiso", version="0.0.0", py_modules=["pypardiso"])
STUBEOF
cat > /tmp/pypardiso_stub/pypardiso.py << 'STUBEOF'
def spsolve(A, b):
    raise RuntimeError("pypardiso not available (mkl not supported on this platform)")
STUBEOF
pip install /tmp/pypardiso_stub

pip install --no-deps -e .

python -c 'import openpnm; print("openpnm imported successfully")'
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
venv/bin/pytest -v -rA tests/

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
venv/bin/pytest -v -rA tests/

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
venv/bin/pytest -v -rA tests/

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

# Choose an appropriate base image based on the project's requirements - replace ubuntu:latest with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM ubuntu:latest

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/PMEAL/OpenPNM.git /home/OpenPNM

WORKDIR /home/OpenPNM
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("PMEAL", "OpenPNM_2798_to_2615")
class OPENPNM_2798_TO_2615(Instance):
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

        # Pattern 1: test name followed by status (PASSED, FAILED, ERROR)
        pattern1 = re.compile(r"(tests/.*?\.py::\w+::\w+) (PASSED|FAILED|ERROR)")
        # Pattern 2: status (FAILED, ERROR) followed by test name
        pattern2 = re.compile(r"(FAILED|ERROR) (tests/.*?\.py::\w+::\w+)")
        # Pattern 3: SKIPPED followed by test name (file or test case)
        pattern3 = re.compile(r"SKIPPED .*? (tests/.*?\.py(?:::\w+::\w+)?)")
        # Process pattern1 matches
        for test_name, status in pattern1.findall(log):
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ["FAILED", "ERROR"]:
                failed_tests.add(test_name)
        # Process pattern2 matches
        for status, test_name in pattern2.findall(log):
            if status in ["FAILED", "ERROR"]:
                failed_tests.add(test_name)
        # Process pattern3 matches
        for test_name in pattern3.findall(log):
            skipped_tests.add(test_name)
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
