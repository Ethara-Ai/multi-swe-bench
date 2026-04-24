from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PolybarImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone --recurse-submodules "
                f"https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    cmake \\
    git \\
    pkg-config \\
    libxcb-composite0-dev \\
    libxcb-ewmh-dev \\
    libxcb-icccm4-dev \\
    libxcb-image0-dev \\
    libxcb-randr0-dev \\
    libxcb-util0-dev \\
    libxcb1-dev \\
    libcairo2-dev \\
    python3-xcbgen \\
    xcb-proto \\
    libxcb-xkb-dev \\
    libxcb-cursor-dev \\
    libxcb-xrm-dev \\
    libjsoncpp-dev \\
    libasound2-dev \\
    libpulse-dev \\
    libnl-genl-3-dev \\
    libmpdclient-dev \\
    libcurl4-openssl-dev \\
    libuv1-dev \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class PolybarImageDefault(Image):
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
        return PolybarImageBase(self.pr, self._config)

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
git submodule update --init --recursive
bash /home/check_git_changes.sh

sed -i 's/GIT_TAG\\s\\+master/GIT_TAG           main/' tests/CMakeLists.txt.in || true

mkdir -p build
cd build
cmake -DBUILD_TESTS=ON -DBUILD_DOC=OFF ..
make -j$(nproc) all_unit_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}/build
ctest --output-on-failure || true

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
rm -rf build
mkdir -p build && cd build
cmake -DBUILD_TESTS=ON -DBUILD_DOC=OFF .. || true
make -j$(nproc) all_unit_tests 2>&1 || true
ctest --output-on-failure 2>&1 || true

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
rm -rf build
mkdir -p build && cd build
cmake -DBUILD_TESTS=ON -DBUILD_DOC=OFF .. || true
make -j$(nproc) all_unit_tests 2>&1 || true
ctest --output-on-failure 2>&1 || true

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


@Instance.register("polybar", "polybar")
class Polybar(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PolybarImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test = pass_match.group(1).strip()
                    passed_tests.add(test)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test = fail_match.group(1).strip()
                    failed_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
