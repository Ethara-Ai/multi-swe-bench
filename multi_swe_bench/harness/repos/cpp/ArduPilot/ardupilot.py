import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ArdupilotImageBase(Image):

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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "ccache",
            "g++",
            "gawk",
            "python3-pip",
            "python3-dev",
            "pkg-config",
            "libexpat1-dev",
        ]

    def extra_setup(self) -> str:
        return (
            "RUN pip3 install "
            "future empy==3.3.4 pexpect dronecan pymavlink MAVProxy numpy"
        )


class ArdupilotImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return ArdupilotImageBase(self.pr, self._config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
git submodule update --init --recursive

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
python3 ./waf configure --board sitl
python3 ./waf tests || true
python3 ./waf check --check-verbose 2>&1 || true
python3 ./waf copter plane rover sub || true
python3 Tools/autotest/autotest.py test.Copter test.Plane test.Rover test.Sub --no-clean --no-configure --timeout=1400 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi

python3 ./waf configure --board sitl
python3 ./waf tests 2>&1 || true
python3 ./waf check --check-verbose 2>&1 || true
python3 ./waf copter plane rover sub || true
python3 Tools/autotest/autotest.py test.Copter test.Plane test.Rover test.Sub --no-clean --no-configure --timeout=1400 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi

python3 ./waf configure --board sitl
python3 ./waf tests 2>&1 || true
python3 ./waf check --check-verbose 2>&1 || true
python3 ./waf copter plane rover sub || true
python3 Tools/autotest/autotest.py test.Copter test.Plane test.Rover test.Sub --no-clean --no-configure --timeout=1400 2>&1 || true

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


@Instance.register("ArduPilot", "ardupilot")
class Ardupilot(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ArdupilotImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes first
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Regex: gtest via `waf check --check-verbose` → `OUT: [       OK ] Suite.Test (N ms)`
        re_gtest_pass = re.compile(
            r"^(?:\s*OUT:\s*)?\[\s+OK\s+\]\s+(\S+)"
        )
        re_gtest_fail = re.compile(
            r"^(?:\s*OUT:\s*)?\[\s+FAILED\s+\]\s+(\S+)"
        )

        # Regex: autotest → `AT-0123.4: PASSED: "TestName"`
        re_autotest_pass = re.compile(
            r"^AT-[\d.]+:\s+PASSED:\s+\"?(.+?)\"?\s*$"
        )
        re_autotest_fail = re.compile(
            r"^AT-[\d.]+:\s+FAILED:\s+\"?(.+?)\"?\s*$"
        )

        # Regex: autotest step → `>>>> PASSED STEP: StepName at timestamp`
        re_step_pass = re.compile(
            r"^>>>>\s+PASSED\s+STEP:\s+(.+?)\s+at\s+"
        )
        re_step_fail = re.compile(
            r"^>>>>\s+FAILED\s+STEP:\s+(.+?)\s+at\s+"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_gtest_pass.match(line)
            if m:
                test_name = m.group(1).strip()
                if not test_name.endswith("tests.") and not test_name[0].isdigit():
                    passed_tests.add(test_name)
                continue

            m = re_gtest_fail.match(line)
            if m:
                test_name = m.group(1).strip()
                if not test_name.endswith("tests.") and not test_name[0].isdigit():
                    failed_tests.add(test_name)
                continue

            m = re_autotest_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_autotest_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_step_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_step_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
