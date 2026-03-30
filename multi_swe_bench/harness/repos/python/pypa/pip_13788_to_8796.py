import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PipImageBase13788(Image):
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

    def image_tag(self) -> str:
        return "base13788"

    def workdir(self) -> str:
        return "base13788"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""\
FROM {self.dependency()}
{self.global_env}
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential libssl-dev libffi-dev && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN pip install --upgrade pip setuptools
{self.clear_env}
"""


class PipImageDefault13788(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[Image, None]:
        return PipImageBase13788(self.pr, self._config)

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
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
# Install the old pip version first (replaces system pip)
pip install -e . || true
# Reinstall setuptools/wheel so we have working build tools
pip install --force-reinstall setuptools wheel || true
# Now install test deps with working pip+setuptools
pip install -r tests/requirements.txt || true
# Ensure pytest and missing test deps are available
pip install pytest pytest-xdist installer || true
# Reinstall setuptools after all installs (belt and suspenders)
pip install --force-reinstall setuptools || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
python -m pytest -v -n auto
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --whitespace=nowarn --allow-binary-replacement /home/test.patch
python -m pytest -v -n auto
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --whitespace=nowarn --allow-binary-replacement /home/test.patch /home/fix.patch
python -m pytest -v -n auto
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


@Instance.register("pypa", "pip_13788_to_8796")
class PIP_13788_TO_8796(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PipImageDefault13788(self.pr, self._config)

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

    def parse_log(self, test_log: str) -> TestResult:
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.splitlines():
            line = line.strip()
            # pytest -rA format: PASSED/FAILED/SKIPPED tests/path::test_name
            match = re.match(
                r"^(PASSED|FAILED|SKIPPED|ERROR|XFAIL(?:ED)?|XPASS(?:ED)?)\s+(tests/[\w/:.+\-=]+(?:\[[^\]]*\])?)",
                line,
            )
            if match:
                status = match.group(1)
                test_name = match.group(2)
                if status in ("PASSED", "XPASSED"):
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(test_name)
                elif status in ("SKIPPED", "XFAIL", "XFAILED"):
                    skipped_tests.add(test_name)
                continue

            # pytest-xdist format: [gwNN] [ NN%] PASSED/FAILED tests/path::test_name
            match = re.match(
                r"^\[gw\d+\]\s+\[\s*\d+%\]\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL(?:ED)?|XPASS(?:ED)?)\s+(tests/[\w/:.+\-=]+(?:\[[^\]]*\])?)",
                line,
            )
            if match:
                status = match.group(1)
                test_name = match.group(2)
                if status in ("PASSED", "XPASSED"):
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(test_name)
                elif status in ("SKIPPED", "XFAIL", "XFAILED"):
                    skipped_tests.add(test_name)
                continue

            # pytest verbose format: tests/path::test_name PASSED [  5%]
            match = re.match(
                r"^(tests/[\w/:.+\-=]+(?:\[[^\]]*\])?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL)\s+\[",
                line,
            )
            if match:
                test_name = match.group(1)
                status = match.group(2)
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(test_name)
                elif status in ("SKIPPED", "XFAIL"):
                    skipped_tests.add(test_name)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
