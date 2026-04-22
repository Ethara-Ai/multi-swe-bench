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
        return "base_python38_poetry"

    def workdir(self) -> str:
        return "base_python38_poetry"

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

RUN pip install --upgrade "pip<23" "setuptools<58" "virtualenv<20.22"
RUN pip install "poetry>=1.0,<1.2"
RUN cd /home/{self.pr.repo} && poetry config virtualenvs.create false && poetry install --no-interaction

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

pip install --upgrade "pip<23" "setuptools<58" "virtualenv<20.22"
pip install "poetry>=1.0,<1.2"
find /usr/local/lib/python3.8/site-packages -name '~*' -exec rm -rf {{}} + 2>/dev/null || true
poetry config virtualenvs.create false
for attempt in 1 2 3; do
  echo "=== poetry install attempt $attempt ==="
  if poetry install --no-interaction; then
    echo "=== poetry install succeeded on attempt $attempt ==="
    break
  fi
  echo "=== poetry install failed on attempt $attempt, cleaning up ==="
  find /usr/local/lib/python3.8/site-packages -name '~*' -exec rm -rf {{}} + 2>/dev/null || true
  if [ "$attempt" -eq 3 ]; then
    echo "=== poetry install failed after 3 attempts ==="
    exit 1
  fi
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest -v --tb=short --override-ini="addopts=" -p no:hypothesispytest --mypy-ini-file=setup.cfg -p no:randomly tests/ typesafety/

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
pytest -v --tb=short --override-ini="addopts=" -p no:hypothesispytest --mypy-ini-file=setup.cfg -p no:randomly tests/ typesafety/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v --tb=short --override-ini="addopts=" -p no:hypothesispytest --mypy-ini-file=setup.cfg -p no:randomly tests/ typesafety/

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
    """Parse pytest verbose output for dry-python/returns tests.

    Handles test output in the format:
      PASSED:  tests/test_foo.py::test_bar PASSED
      FAILED:  FAILED tests/test_foo.py::test_bar
      SKIPPED: tests/test_foo.py::test_bar SKIPPED
      XFAIL:   tests/test_foo.py::test_bar XFAIL
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    # Pattern for PASSED/SKIPPED/XFAIL at end of line
    pattern_status_after = re.compile(r"^((?:tests|typesafety)/.*)::(.*?) (PASSED|SKIPPED|XFAIL)")
    # Pattern for FAILED at start of line
    pattern_failed = re.compile(r"^FAILED ((?:tests|typesafety)/.*)::(.*)")

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


@Instance.register("dry-python", "returns")
class DRY_PYTHON_RETURNS(Instance):
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
