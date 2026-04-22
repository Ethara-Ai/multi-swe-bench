from __future__ import annotations

import re
from typing import Optional, Union

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

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.8-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base_python38_poetry_doorstop"

    def workdir(self) -> str:
        return "base_python38_poetry_doorstop"

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

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git bash gcc build-essential && rm -rf /var/lib/apt/lists/*

{code}

RUN pip install --upgrade pip "setuptools<72"
RUN pip install poetry
RUN pip install "Cython<3" && pip install --no-build-isolation "pyyaml==5.4.1"
RUN cd /home/{self.pr.repo} && sed -i 's/version = "5.4"/version = "5.4.1"/g' poetry.lock 2>/dev/null || true && sed -i 's/pyyaml = "5.4"/pyyaml = "5.4.1"/g' poetry.lock 2>/dev/null || true && sed -i 's/PyYAML==5.4/PyYAML==5.4.1/g' poetry.lock 2>/dev/null || true && poetry config virtualenvs.create false && PIP_CONSTRAINT=/dev/null SETUPTOOLS_USE_DISTUTILS=stdlib poetry install --no-dev --no-interaction
RUN pip install pytest mock

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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

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
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

pip install --upgrade pip "setuptools<72"
pip install poetry
pip install "Cython<3"
pip install --no-build-isolation "pyyaml==5.4.1"
find /usr/local/lib/python3.8/site-packages -name '~*' -exec rm -rf {{}} + 2>/dev/null || true
# Patch poetry.lock to accept pyyaml 5.4.1 (5.4 sdist fails to build without Cython)
sed -i 's/version = "5.4"/version = "5.4.1"/g' poetry.lock 2>/dev/null || true
sed -i 's/pyyaml = "5.4"/pyyaml = "5.4.1"/g' poetry.lock 2>/dev/null || true
sed -i 's/PyYAML==5.4/PyYAML==5.4.1/g' poetry.lock 2>/dev/null || true
poetry config virtualenvs.create false && PIP_CONSTRAINT=/dev/null SETUPTOOLS_USE_DISTUTILS=stdlib poetry install --no-dev --no-interaction
pip install pytest mock

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
python -m pytest doorstop/core/tests/ doorstop/cli/tests/ -v --tb=short --override-ini="addopts="

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='poetry.lock' --exclude='*.woff' --exclude='*.woff2' --exclude='*.png' /home/test.patch \
  || git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='poetry.lock' --exclude='*.woff' --exclude='*.woff2' --exclude='*.png' --3way /home/test.patch \
  || true
python -m pytest doorstop/core/tests/ doorstop/cli/tests/ -v --tb=short --override-ini="addopts="

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='poetry.lock' --exclude='*.woff' --exclude='*.woff2' --exclude='*.png' /home/test.patch /home/fix.patch \
  || git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='poetry.lock' --exclude='*.woff' --exclude='*.woff2' --exclude='*.png' --3way /home/test.patch /home/fix.patch \
  || true
python -m pytest doorstop/core/tests/ doorstop/cli/tests/ -v --tb=short --override-ini="addopts="

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


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest verbose output for doorstop-dev/doorstop tests.

    Handles test output in the format:
      PASSED:  doorstop/core/tests/test_types.py::TestLevel::test_value PASSED
      FAILED:  FAILED doorstop/core/tests/test_all.py::TestPublisher::test_lines_html
      SKIPPED: doorstop/core/tests/test_types.py::TestLevel::test_skip SKIPPED
      XFAIL:   doorstop/core/tests/test_types.py::TestLevel::test_xfail XFAIL
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    # Pattern for PASSED/SKIPPED/XFAIL at end of line
    pattern_status_after = re.compile(r"^(doorstop/.*)::(.*?) (PASSED|SKIPPED|XFAIL)")
    # Pattern for FAILED at start of line
    pattern_failed = re.compile(r"^FAILED (doorstop/.*)::(.*)")

    for line in log.splitlines():
        match_status_after = pattern_status_after.match(line)
        if match_status_after:
            test_path = match_status_after.group(1)
            test_name = match_status_after.group(2)
            status = match_status_after.group(3)
            full_test_name = f"{test_path}::{test_name}"
            if status == "PASSED" or status == "XFAIL":
                passed_tests.add(full_test_name)
            elif status == "SKIPPED":
                skipped_tests.add(full_test_name)
            continue
        match_failed = pattern_failed.match(line)
        if match_failed:
            test_path = match_failed.group(1)
            test_name = match_failed.group(2)
            full_test_name = f"{test_path}::{test_name}"
            failed_tests.add(full_test_name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("doorstop-dev", "doorstop")
class DOORSTOP_DEV_DOORSTOP(Instance):
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
        return parse_pytest_log(log)
