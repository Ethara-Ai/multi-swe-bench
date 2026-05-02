from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MilvusCppImageBase(Image):
    """Base image for milvus-io/milvus C++ era (v0.x - v1.x, PRs 0-5140).

    Pure C++ project using CMake. No Go code exists in this era.
    Tests are C++ unit tests built with CMake in core/unittest/.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "ubuntu:18.04"

    def image_tag(self) -> str:
        return "base-cpp"

    def workdir(self) -> str:
        return "base-cpp"

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

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git \\
    build-essential \\
    g++ \\
    gcc \\
    make \\
    wget \\
    curl \\
    pkg-config \\
    libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

# Install CMake 3.24 (ubuntu:18.04 ships 3.10 which is too old for milvus)
RUN wget -qO /tmp/cmake.sh https://github.com/Kitware/CMake/releases/download/v3.24.4/cmake-3.24.4-linux-$(uname -m).sh \\
    && sh /tmp/cmake.sh --skip-license --prefix=/usr/local \\
    && rm /tmp/cmake.sh

RUN apt-get update && apt-get install -y \\
    libboost-all-dev \\
    libgflags-dev \\
    libgoogle-glog-dev \\
    libgtest-dev \\
    libmysqlclient-dev \\
    libopenblas-dev \\
    liblapack-dev \\
    libtbb-dev \\
    || true && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class MilvusCppImageDefault(Image):
    """Per-PR image for milvus-io/milvus C++ era.

    Builds C++ tests using CMake. Test output uses Google Test format.
    """

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
        return MilvusCppImageBase(self.pr, self.config)

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

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# CMakeLists.txt is in core/ subdirectory for v0.x-v1.x
BUILD_DIR=""
if [ -f "core/CMakeLists.txt" ]; then
  BUILD_DIR="core"
elif [ -f "CMakeLists.txt" ]; then
  BUILD_DIR="."
else
  echo "No CMakeLists.txt found"
  exit 1
fi

cd /home/{pr.repo}

BUILD_DIR=""
if [ -f "core/CMakeLists.txt" ]; then
  BUILD_DIR="core"
elif [ -f "CMakeLists.txt" ]; then
  BUILD_DIR="."
else
  echo "No CMakeLists.txt found"
  exit 1
fi

cd "$BUILD_DIR"

# Strip CUDA from project() LANGUAGES to allow building on non-GPU machines
sed -i 's/LANGUAGES CUDA CXX/LANGUAGES CXX/g' CMakeLists.txt 2>/dev/null || true

mkdir -p cmake_build && cd cmake_build
cmake .. -DCMAKE_BUILD_TYPE=Debug -DMILVUS_GPU_VERSION=OFF -DCUSTOMIZATION=OFF 2>&1 || true
make -j$(nproc) 2>&1 || true

for tb in $(find . -name '*_test' -o -name '*Test' -o -name 'test_*' -type f -executable 2>/dev/null | sort); do
  echo "=== Running $tb ==="
  "$tb" --gtest_print_time=0 2>&1
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn /home/test.patch
fi

BUILD_DIR=""
if [ -f "core/CMakeLists.txt" ]; then
  BUILD_DIR="core"
elif [ -f "CMakeLists.txt" ]; then
  BUILD_DIR="."
else
  echo "No CMakeLists.txt found"
  exit 1
fi

cd "$BUILD_DIR"
sed -i 's/LANGUAGES CUDA CXX/LANGUAGES CXX/g' CMakeLists.txt 2>/dev/null || true

mkdir -p cmake_build && cd cmake_build
cmake .. -DCMAKE_BUILD_TYPE=Debug -DMILVUS_GPU_VERSION=OFF -DCUSTOMIZATION=OFF 2>&1 || true
make -j$(nproc) 2>&1 || true

for tb in $(find . -name '*_test' -o -name '*Test' -o -name 'test_*' -type f -executable 2>/dev/null | sort); do
  echo "=== Running $tb ==="
  "$tb" --gtest_print_time=0 2>&1
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn /home/test.patch
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn /home/fix.patch
fi

BUILD_DIR=""
if [ -f "core/CMakeLists.txt" ]; then
  BUILD_DIR="core"
elif [ -f "CMakeLists.txt" ]; then
  BUILD_DIR="."
else
  echo "No CMakeLists.txt found"
  exit 1
fi

cd "$BUILD_DIR"
sed -i 's/LANGUAGES CUDA CXX/LANGUAGES CXX/g' CMakeLists.txt 2>/dev/null || true

mkdir -p cmake_build && cd cmake_build
cmake .. -DCMAKE_BUILD_TYPE=Debug -DMILVUS_GPU_VERSION=OFF -DCUSTOMIZATION=OFF 2>&1 || true
make -j$(nproc) 2>&1 || true

for tb in $(find . -name '*_test' -o -name '*Test' -o -name 'test_*' -type f -executable 2>/dev/null | sort); do
  echo "=== Running $tb ==="
  "$tb" --gtest_print_time=0 2>&1
done

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


@Instance.register("milvus-io", "milvus_0_to_5140")
class Milvus_0_to_5140(Instance):
    """Instance for milvus-io/milvus C++ era (v0.x - v1.x).

    Build system: CMake
    Test framework: Google Test (C++)
    PRs 0-5140: Pure C++ project, no Go code.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return MilvusCppImageDefault(self.pr, self._config)

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
        """Parse Google Test output format.

        Google Test output:
          [       OK ] TestSuite.TestName (0 ms)
          [  FAILED  ] TestSuite.TestName (0 ms)
          [  SKIPPED ] TestSuite.TestName (0 ms)
          [ RUN      ] TestSuite.TestName
        """
        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"\[\s+OK\s+\]\s+(\S+\.\S+)")
        re_fail = re.compile(r"\[\s+FAILED\s+\]\s+(\S+\.\S+)")
        re_skip = re.compile(r"\[\s+SKIPPED\s+\]\s+(\S+\.\S+)")

        for line in test_log.splitlines():
            stripped = line.strip()

            match = re_pass.match(stripped)
            if match:
                test_name = match.group(1)
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            match = re_fail.match(stripped)
            if match:
                test_name = match.group(1)
                passed_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            match = re_skip.match(stripped)
            if match:
                test_name = match.group(1)
                if test_name not in failed_tests and test_name not in passed_tests:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
