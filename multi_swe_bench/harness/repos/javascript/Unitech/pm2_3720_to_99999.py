"""PM2 harness for Era 2 (PRs 3720-5971): mocha ^5+, test/unit.sh.

Uses ubuntu:latest with Node.js from apt (v18).
"""

from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TAG_SUFFIX = "3720_to_99999"


class _ImageBase(Image):
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
        return "ubuntu:latest"

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return f"base-{_TAG_SUFFIX}"

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
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt update && apt install -y git nodejs npm bc

{code}

{self.clear_env}

"""


class _ImageDefault(Image):
    """Era 2 image: runs test/unit.sh with mocha ^5+."""

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
        return _ImageBase(self.pr, self._config)

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
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
NODE_ENV=test bash test/unit.sh 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
NODE_ENV=test bash test/unit.sh 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
NODE_ENV=test bash test/unit.sh 2>&1 || true

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


def _parse_mocha_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Strip ANSI escape codes
    ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    # Mocha spec reporter patterns (era 2 uses ✔):
    #   ✔ test name
    #   ✔ test name (282ms)
    #   ✓ test name             (both accepted)
    re_pass = re.compile(r"^[✔✓]\s+(.+?)(?:\s+\(\d+(?:ms|s)\))?$")
    re_fail = re.compile(r"^\d+\)\s+(.+?)(?:\s+\(\d+(?:ms|s)\))?$")

    # unit.sh success marker: [V] test/programmatic/foo.mocha.js succeeded
    re_unit_pass = re.compile(r"^\[V\]\s+(\S+)\s+succeeded")
    # unit.sh failure marker: ######## TEST ✘ test/... FAILED
    re_unit_fail = re.compile(r"^#{4,}\s+TEST\s+[✘]\s+(\S+)\s+FAILED")

    for line in test_log.splitlines():
        clean = ansi_re.sub("", line).strip()
        if not clean:
            continue

        # Skip PM2 daemon log lines
        if re.match(r"^\[\d{4}-\d{2}-\d{2}", clean):
            continue

        # Check mocha spec pass/fail
        pass_match = re_pass.match(clean)
        if pass_match:
            passed_tests.add(pass_match.group(1).strip())
            continue

        fail_match = re_fail.match(clean)
        if fail_match:
            test_name = fail_match.group(1).strip()
            if test_name and not test_name.endswith(":"):
                failed_tests.add(test_name)
            continue

        # Check unit.sh level markers
        unit_pass_match = re_unit_pass.match(clean)
        if unit_pass_match:
            passed_tests.add(unit_pass_match.group(1))
            continue

        unit_fail_match = re_unit_fail.match(clean)
        if unit_fail_match:
            failed_tests.add(unit_fail_match.group(1))
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("Unitech", "pm2_3720_to_99999")
class PM2_3720_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

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
        return (
            "bash -c '"
            "cd /home/pm2 && "
            "git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch; "
            "npm install 2>&1 && "
            "export PATH=$PATH:/home/pm2/node_modules/.bin && "
            "NODE_ENV=test bash test/unit.sh 2>&1 || true"
            "'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_mocha_log(test_log)
