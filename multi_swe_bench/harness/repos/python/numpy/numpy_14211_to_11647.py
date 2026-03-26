import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import (
    TestStatus,
    mapping_to_testresult,
    get_modified_files,
)


# PRs 11647, 11902, 12080, 12296, 12754, 12909, 13072, 13392, 13742, 14211
# Python 2.7 | setup.py | ubuntu-16.04
# Dependency signature: py:2.x||os:ubuntu-16.04||deps:setup.py
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
        return "python:2.7-slim-buster"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files_from_patch = get_modified_files(self.pr.test_patch)
        # Filter to only actual test files (skip docs, requirements, utilities)
        test_files_filtered = [
            f
            for f in (test_files_from_patch or [])
            if "/tests/" in f and f.endswith(".py")
        ]
        test_files = " ".join(test_files_filtered) if test_files_filtered else "numpy/"
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
                "check_git_changes.sh",
                """#!/bin/bash
cd /home/numpy
git config --global --add safe.directory /home/numpy
if [ -n "$(git status --porcelain)" ]; then
    echo "Warning: Uncommitted changes detected"
    git diff --stat
fi
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/numpy
git config --global --add safe.directory /home/numpy
git reset --hard
git checkout {base_sha}
pip install --no-cache-dir pytest cython==0.29.36 2>/dev/null || true
pip install --no-cache-dir -e . 2>/dev/null || python setup.py build_ext --inplace 2>/dev/null || true
""".format(base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uxo pipefail
cd /home/numpy
python -m pytest {test_files} -rA --tb=short -v --ignore=doc -p no:cacheprovider -W ignore::pytest.PytestUnknownMarkWarning 2>&1
""".format(test_files=test_files),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uxo pipefail
cd /home/numpy
git config --global --add safe.directory /home/numpy
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
pip install -e . 2>/dev/null || python setup.py build_ext --inplace 2>/dev/null || true
python -m pytest {test_files} -rA --tb=short -v --ignore=doc -p no:cacheprovider -W ignore::pytest.PytestUnknownMarkWarning 2>&1
""".format(test_files=test_files),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uxo pipefail
cd /home/numpy
git config --global --add safe.directory /home/numpy
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
pip install -e . 2>/dev/null || python setup.py build_ext --inplace 2>/dev/null || true
echo "VERIFICATION_START"
python -m pytest {test_files} -rA --tb=short -v --ignore=doc -p no:cacheprovider -W ignore::pytest.PytestUnknownMarkWarning 2>&1
echo "VERIFICATION_END"
""".format(test_files=test_files),
            ),
        ]

    def dockerfile(self) -> str:
        global_env_block = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env_block = f"\n{self.clear_env}" if self.clear_env else ""

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM python:2.7-slim-buster{global_env_block}
ENV DEBIAN_FRONTEND=noninteractive

RUN sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\
    sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \\
    sed -i '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get update -o Acquire::Check-Valid-Until=false && apt-get install -y \\
    build-essential gfortran libopenblas-dev git pkg-config \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then \\
        if command -v apk >/dev/null 2>&1; then apk add --no-cache bash; \\
        elif command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y bash; \\
        elif command -v yum >/dev/null 2>&1; then yum install -y bash; \\
        else exit 1; fi \\
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/numpy/numpy.git /home/numpy

WORKDIR /home/numpy
RUN git reset --hard
RUN git checkout {self.pr.base.sha}
RUN git submodule update --init 2>/dev/null || true
RUN pip install --no-cache-dir "pytest<5" cython==0.29.36 nose 2>/dev/null || true
RUN pip install --no-cache-dir -e . 2>/dev/null || python setup.py build_ext --inplace 2>/dev/null || true

{copy_commands}{clear_env_block}
"""


@Instance.register("numpy", "numpy_14211_to_11647")
class NUMPY_14211_TO_11647(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    _PYTEST_STUB = "pip install pytest -q 2>/dev/null"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return f"bash -c '{self._PYTEST_STUB}; bash /home/run.sh'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return f"bash -c '{self._PYTEST_STUB}; bash /home/test-run.sh'"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return f"bash -c '{self._PYTEST_STUB}; bash /home/fix-run.sh'"

    def parse_log(self, log: str) -> TestResult:
        test_status_map = {}

        pattern = re.compile(
            r"([\w/.\.\-\[\]:]+)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL)"
        )
        for line in log.splitlines():
            match = pattern.search(line)
            if match:
                test_name = match.group(1)
                status = match.group(2).upper()
                if status == "PASSED":
                    test_status_map[test_name] = TestStatus.PASSED.value
                elif status in ("FAILED", "ERROR"):
                    test_status_map[test_name] = TestStatus.FAILED.value
                elif status == "SKIPPED":
                    test_status_map[test_name] = TestStatus.SKIPPED.value
                elif status == "XFAIL":
                    test_status_map[test_name] = TestStatus.XFAIL.value

        return mapping_to_testresult(test_status_map)
