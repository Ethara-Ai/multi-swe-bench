import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Binary diffs cause 'cannot apply binary patch without full index line'
    errors with git apply. These are not needed for compilation or testing.
    """
    if not patch_content:
        return patch_content

    lines = patch_content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('diff --git'):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith('diff --git'):
                if lines[i].startswith('GIT binary patch') or lines[i].startswith('Binary files'):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)


class CapnprotoImageBase(Image):
    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        base_image: str,
        tag_suffix: str,
        compiler: str = "gcc",
    ):
        self._pr = pr
        self._config = config
        self._base_image = base_image
        self._tag_suffix = tag_suffix
        self._compiler = compiler

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return self._base_image

    def image_tag(self) -> str:
        return f"base-{self._tag_suffix}"

    def workdir(self) -> str:
        return f"base-{self._tag_suffix}"

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

        # Clang-based images need clang installed explicitly
        if self._compiler == "clang":
            extra_packages = "clang \\\n    git \\\n    "
            env_prefix = "ENV DEBIAN_FRONTEND=noninteractive\n"
        else:
            extra_packages = ""
            env_prefix = ""

        return f"""FROM {image_name}

{env_prefix}{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    {extra_packages}cmake \\
    autoconf \\
    automake \\
    libtool \\
    pkg-config \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class CapnprotoImageDefault(Image):
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
        # PR #1730: fix patch requires C++20 (coroutines), gcc:10 can't compile it
        _clang_overrides = {1730}
        # PR #2385: fix patch requires C++23 (#include <print>), clang-14 can't handle it
        # PR #2410: base has linker bug (missing kj-async link), fix adds it + upgrades to C++23
        #           needs gcc:latest for C++23 support; linker bug in base is expected (causes run/test failures)
        _gcc_latest_overrides = {2385, 2410}

        if self.pr.number in _clang_overrides:
            return CapnprotoImageBase(
                self.pr,
                self._config,
                base_image="ubuntu:22.04",
                tag_suffix="clang-14",
                compiler="clang",
            )
        if self.pr.number in _gcc_latest_overrides:
            return CapnprotoImageBase(
                self.pr, self._config, base_image="gcc:latest", tag_suffix="latest"
            )
        if self.pr.number <= 1730:
            return CapnprotoImageBase(
                self.pr, self._config, base_image="gcc:10", tag_suffix="cpp-10"
            )
        elif self.pr.number <= 2409:
            return CapnprotoImageBase(
                self.pr,
                self._config,
                base_image="ubuntu:22.04",
                tag_suffix="clang-14",
                compiler="clang",
            )
        return CapnprotoImageBase(
            self.pr, self._config, base_image="gcc:latest", tag_suffix="latest"
        )

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _cmake_flags(self) -> str:
        base = self.dependency()
        if isinstance(base, CapnprotoImageBase) and base._compiler == "clang":
            return "-DBUILD_TESTING=ON -DCMAKE_CXX_COMPILER=clang++"
        if self.pr.number <= 1730:
            return '-DBUILD_TESTING=ON -DCMAKE_CXX_FLAGS="-Wno-narrowing"'
        return "-DBUILD_TESTING=ON"

    def files(self) -> list[File]:
        filtered_fix_patch = _filter_binary_patches(self.pr.fix_patch)
        filtered_test_patch = _filter_binary_patches(self.pr.test_patch)
        cmake_flags = self._cmake_flags()

        return [
            File(
                ".",
                "fix.patch",
                f"{filtered_fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{filtered_test_patch}",
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

cd c++
mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}/c++/build
cmake .. {cmake_flags}
make -j$(nproc)
cd src && ctest --output-on-failure
""".format(pr=self.pr, cmake_flags=cmake_flags),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
cd c++/build
cmake .. {cmake_flags} || true
make -j$(nproc) -k 2>&1 || true
cd src && ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr, cmake_flags=cmake_flags),
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
cd c++/build
cmake .. {cmake_flags} || true
make -j$(nproc) -k 2>&1 || true
cd src && ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr, cmake_flags=cmake_flags),
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


@Instance.register("capnproto", "capnproto")
class Capnproto(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CapnprotoImageDefault(self.pr, self._config)

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

        # CTest executable-level patterns (e.g. "1/5 Test #1: kj-tests-run ... Passed")
        re_ctest_pass = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"
        )
        re_ctest_fail = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]

        # Sub-test-level patterns from --output-on-failure
        # e.g. "[ PASS ] async-test.c++:31: legacy test: Async/GetFunctorStartAddress"
        # e.g. "[ FAIL ] filesystem-disk-test.c++:825: DiskFile holes"
        re_subtest_pass = re.compile(
            r"^\[\s*PASS\s*\]\s+(.+)$"
        )
        re_subtest_fail = re.compile(
            r"^\[\s*FAIL\s*\]\s+(.+)$"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            # Check CTest-level pass
            m = re_ctest_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            # Check CTest-level fail
            for pat in re_ctest_fail:
                m = pat.match(line)
                if m:
                    failed_tests.add(m.group(1).strip())
                    break
            else:
                # Check sub-test-level pass/fail
                m = re_subtest_pass.match(line)
                if m:
                    passed_tests.add(m.group(1).strip())
                    continue

                m = re_subtest_fail.match(line)
                if m:
                    failed_tests.add(m.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
