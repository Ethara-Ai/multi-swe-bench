import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


CF_XARRAY_473_TO_354_PR_TESTS = {
    354: [
        "cf_xarray/tests/test_accessor.py",
    ],
    370: [
        "cf_xarray/tests/test_accessor.py",
    ],
    435: [
        "cf_xarray/tests/test_accessor.py",
    ],
    458: [
        "cf_xarray/tests/test_accessor.py",
    ],
    473: [
        "cf_xarray/tests/test_accessor.py",
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
        return "python:3.9-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        test_files = CF_XARRAY_473_TO_354_PR_TESTS.get(self.pr.number, [])
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
cd /home/[[REPO_NAME]]
pip install --upgrade pip setuptools wheel
pip install xarray numpy pandas "setuptools<70" netCDF4 dask matplotlib pint pooch shapely
pip install regex rich scipy flox lxml
pip install pytest pytest-cov setuptools-scm
pip install -e .
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
# Filter to only test files that exist at this base commit
EXISTING_TESTS=""
for f in [[TEST_FILES]]; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    echo "No test files found"
    exit 1
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""".replace("[[REPO_NAME]]", repo_name).replace("[[TEST_FILES]]", test_files_str),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Filter to only test files that exist after applying test patch
EXISTING_TESTS=""
for f in [[TEST_FILES]]; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    echo "No test files found"
    exit 1
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""".replace("[[REPO_NAME]]", repo_name).replace("[[TEST_FILES]]", test_files_str),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.png' /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.png' /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
# Filter to only test files that exist after applying patches
EXISTING_TESTS=""
for f in [[TEST_FILES]]; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    echo "No test files found"
    exit 1
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""".replace("[[REPO_NAME]]", repo_name).replace("[[TEST_FILES]]", test_files_str),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = f"""
FROM python:3.9-bookworm

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content


@Instance.register("xarray-contrib", "cf-xarray_473_to_354")
class CF_XARRAY_473_TO_354(Instance):
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
                # pytest -rA summary: "test_path::test_name PASSED"
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
