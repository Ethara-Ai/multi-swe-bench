import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FaissImageBase(Image):
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
        return "ubuntu:20.04"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc-10 \
    g++-10 \
    libopenblas-dev \
    liblapack-dev \
    python3 \
    python3-pip \
    python3-numpy \
    swig \
    curl \
    git \
    autoconf \
    automake \
    libtool \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-10 100 \
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-10 100 \
    && pip3 install cmake

{code}

{self.clear_env}

"""


class FaissImageDefault(Image):
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
        return FaissImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @staticmethod
    def _arm_makefile_fixup() -> str:
        """Strip x86-specific compiler flags from makefile.inc on ARM."""
        return r"""
# Fix makefile.inc for ARM: remove x86-only flags
if [ "$(uname -m)" = "aarch64" ]; then
  if [ -f makefile.inc ]; then
    sed -i 's/-m64//g; s/-mavx2//g; s/-mavx//g; s/-msse4//g; s/-mpopcnt//g; s/-mf16c//g' makefile.inc
  fi
fi
"""

    @staticmethod
    def _arm_source_fixup() -> str:
        """On aarch64: install sse2neon.h shim, create stub immintrin.h, fix rdtsc asm."""
        return r"""
# ARM source-level fixup: sse2neon for SSE/AVX intrinsics, rdtsc replacement
if [ "$(uname -m)" = "aarch64" ]; then
  mkdir -p /home/arm_compat
  # Download sse2neon.h (translates SSE/AVX intrinsics to NEON)
  if [ ! -f /home/arm_compat/sse2neon.h ]; then
    curl -sL https://raw.githubusercontent.com/DLTcollab/sse2neon/master/sse2neon.h \
      -o /home/arm_compat/sse2neon.h
  fi
  # Create stub immintrin.h that includes sse2neon
  cat > /home/arm_compat/immintrin.h << 'STUBEOF'
#ifndef _IMMINTRIN_H_ARM_SHIM
#define _IMMINTRIN_H_ARM_SHIM
#include "sse2neon.h"
#endif
STUBEOF
  # Patch any rdtsc inline asm (x86-only) to return 0
  # Replace multi-line asm volatile("rdtsc ...") blocks with stub assignments
  find . -name '*.cpp' -o -name '*.h' | xargs grep -l 'rdtsc' 2>/dev/null | while read f; do
    python3 -c "
import re, sys
with open(sys.argv[1], 'r') as fh:
    content = fh.read()
# Replace asm volatile(\"rdtsc...\") multi-line blocks with stub
content = re.sub(
    r'asm\s+volatile\s*\(\s*\"rdtsc[^;]*;',
    'low = 0; high = 0; // rdtsc stubbed for ARM',
    content,
    flags=re.DOTALL
)
with open(sys.argv[1], 'w') as fh:
    fh.write(content)
" "$f"
  done
  # Add -I/home/arm_compat to makefile.inc CFLAGS/CXXFLAGS so stub immintrin.h is found first
  if [ -f makefile.inc ]; then
    sed -i 's|^CFLAGS\s*=|CFLAGS = -I/home/arm_compat |' makefile.inc
    sed -i 's|^CXXFLAGS\s*=|CXXFLAGS = -I/home/arm_compat |' makefile.inc
    # If no separate CXXFLAGS line, also try the combined flags
    grep -q 'I/home/arm_compat' makefile.inc || \
      sed -i 's|^FLAGS\s*=|FLAGS = -I/home/arm_compat |' makefile.inc
  fi
fi
"""

    def _build_commands_autotools(self) -> str:
        """Build commands for Era 1 (manual makefile.inc) and Era 2 (autotools)."""
        arm_makefile_fix = self._arm_makefile_fixup()
        arm_source_fix = self._arm_source_fixup()

        if self.pr.number < 516:
            # Era 1: No configure script, use manual makefile.inc
            return """cd /home/{repo}
cp example_makefiles/makefile.inc.Linux makefile.inc
sed -i 's|BLASLDFLAGS=.*|BLASLDFLAGS=-lopenblas -llapack|' makefile.inc
{arm_makefile_fix}
{arm_source_fix}
make -j$(nproc)
cd tests && make gtest && make tests
""".format(repo=self.pr.repo, arm_makefile_fix=arm_makefile_fix, arm_source_fix=arm_source_fix)
        else:
            # Era 2: Autotools-based build
            return """cd /home/{repo}
if [ ! -f configure ]; then
  autoreconf -iv
fi
./configure --without-cuda
{arm_makefile_fix}
{arm_source_fix}
make -j$(nproc)
cd tests && make gtest && make tests
""".format(repo=self.pr.repo, arm_makefile_fix=arm_makefile_fix, arm_source_fix=arm_source_fix)

    @staticmethod
    def _build_commands_cmake(repo: str) -> str:
        """Build commands for CMake-based build (PR 1190+, post fix_patch)."""
        return """cd /home/{repo}
cmake -B build \
  -DFAISS_ENABLE_GPU=OFF \
  -DFAISS_ENABLE_PYTHON=OFF \
  -DBUILD_TESTING=ON \
  -DFAISS_OPT_LEVEL=generic \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build build -j$(nproc)
""".format(repo=repo)

    def _test_command_autotools(self) -> str:
        return "cd /home/{repo}/tests && ./tests".format(repo=self.pr.repo)

    @staticmethod
    def _test_command_cmake(repo: str) -> str:
        return "cd /home/{repo}/build && ctest --output-on-failure".format(repo=repo)

    def _detect_and_build(self) -> str:
        autotools_build = self._build_commands_autotools()
        cmake_build = self._build_commands_cmake(self.pr.repo)
        autotools_test = self._test_command_autotools()
        cmake_test = self._test_command_cmake(self.pr.repo)

        return """
if [ ! -f /home/{repo}/configure.ac ] && [ -f /home/{repo}/CMakeLists.txt ]; then
  echo "=== Using CMake build system ==="
  {cmake_build}
  {cmake_test} || true
else
  echo "=== Using autotools build system ==="
  {autotools_build}
  {autotools_test} || true
fi
""".format(
            repo=self.pr.repo,
            cmake_build=cmake_build,
            cmake_test=cmake_test,
            autotools_build=autotools_build,
            autotools_test=autotools_test,
        )

    def files(self) -> list[File]:
        # run.sh always uses autotools (base checkout, no patches)
        autotools_build = self._build_commands_autotools()
        autotools_test = self._test_command_autotools()

        # test-run.sh and fix-run.sh use detect-and-build (patches may change build system)
        detect_build = self._detect_and_build()

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

{autotools_build}
{autotools_test}
""".format(autotools_build=autotools_build, autotools_test=autotools_test),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
{detect_build}

""".format(repo=self.pr.repo, detect_build=detect_build),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
{detect_build}

""".format(repo=self.pr.repo, detect_build=detect_build),
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


@Instance.register("facebookresearch", "faiss")
class Faiss(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FaissImageDefault(self.pr, self._config)

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

        # GTest output: [       OK ] TestSuite.TestName (xxx ms)
        re_pass = re.compile(r"^\[\s+OK\s+\]\s+(\S+\.\S+)")
        re_fail = re.compile(r"^\[\s+FAILED\s+\]\s+(\S+\.\S+)")

        # CTest output: N/N Test #N: TestName ...   Passed X.XX sec
        re_ctest_pass = re.compile(
            r"^\d+/\d+\s+Test\s+#\d+:\s+(.*?)\s*\.+\s+Passed"
        )
        re_ctest_fail = re.compile(
            r"^\d+/\d+\s+Test\s+#\d+:\s+(.*?)\s*\.+\s+\*\*\*Failed"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass.match(line)
            if pass_match:
                test = pass_match.group(1)
                passed_tests.add(test)
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                test = fail_match.group(1)
                failed_tests.add(test)
                continue

            ctest_pass = re_ctest_pass.match(line)
            if ctest_pass:
                test = ctest_pass.group(1)
                passed_tests.add(test)
                continue

            ctest_fail = re_ctest_fail.match(line)
            if ctest_fail:
                test = ctest_fail.group(1)
                failed_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
