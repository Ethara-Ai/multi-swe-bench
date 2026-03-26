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


# PRs 30490, 30629, 30759
# Python 3.12 | meson | numpy 2.4.x (requires Python >=3.11)
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
        return "python:3.12-slim-bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files_from_patch = get_modified_files(self.pr.test_patch)
        test_files_filtered = [
            f
            for f in (test_files_from_patch or [])
            if "/tests/" in f and f.endswith(".py") and "typing/tests/data/" not in f
        ]
        resolve_prefix = 'cd /tmp && NUMPY_INSTALLED=$(python -c "import numpy; print(numpy.__path__[0])" 2>/dev/null) && if [ -z "$NUMPY_INSTALLED" ]; then NUMPY_INSTALLED=$(python -c "import site; import glob; dirs=glob.glob(site.getsitepackages()[0]+\'/numpy\'); print(dirs[0] if dirs else \'\')" 2>/dev/null); fi && if [ -z "$NUMPY_INSTALLED" ]; then echo "ERROR: numpy not installed"; exit 1; fi'
        if test_files_filtered:
            resolved_test_args = " ".join(
                f'"$NUMPY_INSTALLED/{f.split("numpy/", 1)[-1]}"'
                if "numpy/" in f
                else f'"$NUMPY_INSTALLED/{f}"'
                for f in test_files_filtered
            )
        else:
            resolved_test_args = '"$NUMPY_INSTALLED/"'
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
pip install --no-cache-dir pytest "hypothesis<6.31" cython 2>/dev/null || true
pip install --no-cache-dir meson-python meson ninja cython pybind11 2>/dev/null || true
pip install --no-cache-dir --no-build-isolation -e . 2>/dev/null || pip install --no-cache-dir -e . 2>/dev/null || python setup.py build_ext --inplace 2>/dev/null || true
""".format(base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uxo pipefail
{resolve_prefix}
python -m pytest {test_args} --no-header -rA --tb=short -v -p no:cacheprovider -W ignore::pytest.PytestUnknownMarkWarning 2>&1
""".format(resolve_prefix=resolve_prefix, test_args=resolved_test_args),
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
# Try rebuild; if it fails (e.g. C ABI incompatibility), fall back to base image numpy
pip install --no-build-isolation . 2>/dev/null || echo "WARN: rebuild failed, using base image numpy"
{resolve_prefix}
python -m pytest {test_args} --no-header -rA --tb=short -v -p no:cacheprovider -W ignore::pytest.PytestUnknownMarkWarning 2>&1
""".format(resolve_prefix=resolve_prefix, test_args=resolved_test_args),
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
# Try rebuild; if it fails (e.g. C ABI incompatibility), fall back to base image numpy
pip install --no-build-isolation . 2>/dev/null || echo "WARN: rebuild failed, using base image numpy"
{resolve_prefix}
echo "VERIFICATION_START"
python -m pytest {test_args} --no-header -rA --tb=short -v -p no:cacheprovider -W ignore::pytest.PytestUnknownMarkWarning 2>&1
echo "VERIFICATION_END"
""".format(resolve_prefix=resolve_prefix, test_args=resolved_test_args),
            ),
        ]

    def dockerfile(self) -> str:
        global_env_block = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env_block = f"\n{self.clear_env}" if self.clear_env else ""

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM python:3.12-slim-bookworm{global_env_block}
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \\
    build-essential gfortran libopenblas-dev git pkg-config cmake ninja-build \\
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
RUN pip install --no-cache-dir pytest "hypothesis<6.31" typing_extensions meson-python meson ninja cython pybind11 2>/dev/null || true
RUN pip install --no-cache-dir --no-build-isolation . 2>/dev/null || true

{copy_commands}{clear_env_block}
"""


@Instance.register("numpy", "numpy_30759_to_30490")
class NUMPY_30759_TO_30490(Instance):
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
            r"(\.{0,2}[\w/.\-\[\]:]+)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL)"
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
