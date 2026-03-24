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
        return "python:3.10-slim"

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

###ACTION_DELIMITER###
apt-get update && apt-get install -y build-essential
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
pip install -e ".[complete,test]" || pip install -e ".[test]" || pip install -e .
###ACTION_DELIMITER###
pip install pytest pytest-xdist pytest-timeout
###ACTION_DELIMITER###
echo 'pytest -rA --timeout=300 dask/array/tests/test_array_core.py dask/dataframe/io/tests/test_csv.py dask/dataframe/io/tests/test_demo.py dask/dataframe/io/tests/test_io.py dask/dataframe/io/tests/test_json.py dask/dataframe/io/tests/test_parquet.py dask/dataframe/tests/test_accessors.py dask/dataframe/tests/test_arithmetics_reduction.py dask/dataframe/tests/test_categorical.py dask/dataframe/tests/test_dataframe.py dask/dataframe/tests/test_groupby.py dask/dataframe/tests/test_methods.py dask/dataframe/tests/test_multi.py dask/dataframe/tests/test_pyarrow.py dask/dataframe/tests/test_pyarrow_compat.py dask/dataframe/tests/test_reshape.py dask/dataframe/tests/test_rolling.py dask/dataframe/tests/test_shuffle.py dask/dataframe/tests/test_utils_dataframe.py dask/tests/test_spark_compat.py' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/dask
EXISTING_FILES=""
for f in dask/array/tests/test_array_core.py dask/dataframe/io/tests/test_csv.py dask/dataframe/io/tests/test_demo.py dask/dataframe/io/tests/test_io.py dask/dataframe/io/tests/test_json.py dask/dataframe/io/tests/test_parquet.py dask/dataframe/tests/test_accessors.py dask/dataframe/tests/test_arithmetics_reduction.py dask/dataframe/tests/test_categorical.py dask/dataframe/tests/test_dataframe.py dask/dataframe/tests/test_groupby.py dask/dataframe/tests/test_methods.py dask/dataframe/tests/test_multi.py dask/dataframe/tests/test_pyarrow.py dask/dataframe/tests/test_pyarrow_compat.py dask/dataframe/tests/test_reshape.py dask/dataframe/tests/test_rolling.py dask/dataframe/tests/test_shuffle.py dask/dataframe/tests/test_utils_dataframe.py dask/tests/test_spark_compat.py; do
    if [ -f "$f" ]; then
        EXISTING_FILES="$EXISTING_FILES $f"
    fi
done
if [ -z "$EXISTING_FILES" ]; then
    echo "No test files found at base commit, running dask/tests/"
    pytest -rA --timeout=300 dask/tests/ -x -q 2>&1 || true
else
    pytest -rA --timeout=300 $EXISTING_FILES 2>&1 || true
fi
""",
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
pytest -rA --timeout=300 dask/array/tests/test_array_core.py dask/dataframe/io/tests/test_csv.py dask/dataframe/io/tests/test_demo.py dask/dataframe/io/tests/test_io.py dask/dataframe/io/tests/test_json.py dask/dataframe/io/tests/test_parquet.py dask/dataframe/tests/test_accessors.py dask/dataframe/tests/test_arithmetics_reduction.py dask/dataframe/tests/test_categorical.py dask/dataframe/tests/test_dataframe.py dask/dataframe/tests/test_groupby.py dask/dataframe/tests/test_methods.py dask/dataframe/tests/test_multi.py dask/dataframe/tests/test_pyarrow.py dask/dataframe/tests/test_pyarrow_compat.py dask/dataframe/tests/test_reshape.py dask/dataframe/tests/test_rolling.py dask/dataframe/tests/test_shuffle.py dask/dataframe/tests/test_utils_dataframe.py dask/tests/test_spark_compat.py

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
pytest -rA --timeout=300 dask/array/tests/test_array_core.py dask/dataframe/io/tests/test_csv.py dask/dataframe/io/tests/test_demo.py dask/dataframe/io/tests/test_io.py dask/dataframe/io/tests/test_json.py dask/dataframe/io/tests/test_parquet.py dask/dataframe/tests/test_accessors.py dask/dataframe/tests/test_arithmetics_reduction.py dask/dataframe/tests/test_categorical.py dask/dataframe/tests/test_dataframe.py dask/dataframe/tests/test_groupby.py dask/dataframe/tests/test_methods.py dask/dataframe/tests/test_multi.py dask/dataframe/tests/test_pyarrow.py dask/dataframe/tests/test_pyarrow_compat.py dask/dataframe/tests/test_reshape.py dask/dataframe/tests/test_rolling.py dask/dataframe/tests/test_shuffle.py dask/dataframe/tests/test_utils_dataframe.py dask/tests/test_spark_compat.py

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.10-slim

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
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/dask/dask.git /home/dask

WORKDIR /home/dask
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip setuptools wheel
RUN pip install "numpy<2"
RUN pip install "pandas<2.1"
RUN pip install pytest-cov pytest-rerunfailures xarray tzdata || true
RUN pip install -e ".[complete,test]" || pip install -e ".[test]" || pip install -e . || pip install dask
RUN pip install pytest pytest-xdist pytest-timeout || true
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("dask", "dask_2023_7_1_to_2023_8_0")
class DASK_2023_7_1_TO_2023_8_0(Instance):
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
        passed_tests = set[str]()
        failed_tests = set[str]()
        skipped_tests = set[str]()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_status_pattern = re.compile(
            r"((dask/\S*?\.py::\S+))\s+(PASSED|FAILED|SKIPPED|XFAILED|XPASSED|ERROR)"
            r"|"
            r"(PASSED|FAILED|SKIPPED|XFAILED|XPASSED|ERROR)\s+((dask/\S*?\.py::\S+))"
        )
        test_status = {}
        for line in log.splitlines():
            stripped_line = ansi_escape.sub("", line)
            match = test_status_pattern.search(stripped_line)
            if match:
                test_name = match.group(1) if match.group(1) else match.group(5)
                status = match.group(3) if match.group(3) else match.group(4)
                test_status[test_name] = status

        for test_name, status in test_status.items():
            if status in ("PASSED", "XPASSED"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "XFAILED", "ERROR"):
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
