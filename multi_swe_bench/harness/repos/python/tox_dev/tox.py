import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ToxImageBase(Image):
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
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install setuptools wheel "setuptools-scm[toml]" hatchling hatch-vcs

WORKDIR /home/
RUN git clone https://github.com/tox-dev/tox.git /home/tox
"""


class ToxImagePR(Image):
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
        return ToxImageBase(self.pr, self.config)

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
                "check_git_changes.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi
echo "check_git_changes: No uncommitted changes"
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fd || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

pip install -e ".[testing]" || \
    pip install -e ".[test]" || \
    pip install -e ".[tests]" || \
    pip install -e "." || true

pip install pytest pytest-mock pytest-xdist pytest-cov pytest-timeout \
    flaky psutil devpi-process distlib re-assert detect-test-pollution \
    covdefaults diff-cover time-machine argcomplete \
    freezegun pathlib2 \
    2>/dev/null || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pytest -v --continue-on-collection-errors --timeout=300 --ignore=tests/session/cmd/test_parallel.py tests/ 2>&1; exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -- . || true
git clean -fd || true
git apply --whitespace=nowarn /home/test.patch || true
pip install -e "." 2>/dev/null || true
pytest -v --continue-on-collection-errors --timeout=300 --ignore=tests/session/cmd/test_parallel.py tests/ 2>&1; exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -- . || true
git clean -fd || true
git apply --whitespace=nowarn /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch || true
pip install -e "." 2>/dev/null || true
pytest -v --continue-on-collection-errors --timeout=300 --ignore=tests/session/cmd/test_parallel.py tests/ 2>&1; exit 0
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

        prepare_commands = "RUN bash /home/prepare.sh; exit 0"

        return """
FROM {name}:{tag}

{copy_commands}
{prepare_commands}
""".format(name=name, tag=tag, copy_commands=copy_commands, prepare_commands=prepare_commands)


@Instance.register("tox-dev", "tox")
class Tox(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ToxImagePR(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest verbose output: tests/unit/test_foo.py::test_bar PASSED [ 5%]
        pattern1 = re.compile(r"^(tests/.*?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\s+\[")
        # Alternative: FAILED tests/unit/test_foo.py::test_bar - ...
        pattern2 = re.compile(r"^(PASSED|FAILED|SKIPPED|ERROR)\s+(tests/.*?)(\s+-.*)?$")

        for line in test_log.split("\n"):
            line = line.strip()
            match1 = pattern1.match(line)
            if match1:
                test_name = match1.group(1).strip()
                status = match1.group(2)
            else:
                match2 = pattern2.match(line)
                if match2:
                    status = match2.group(1)
                    test_name = match2.group(2).strip()
                else:
                    continue

            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
