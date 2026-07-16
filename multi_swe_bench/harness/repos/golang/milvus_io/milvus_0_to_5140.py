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
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    g++ \\
    gcc \\
    make \\
    wget \\
    curl \\
    pkg-config \\
    libssl-dev \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# Install CMake 3.24 (ubuntu:18.04 ships 3.10 which is too old for milvus).
# TARGETARCH is only set by buildx --platform; fall back to uname for native builds.
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

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
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

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

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

# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Registered so delivered records (which carry the dash-joined number_interval)
# resolve to this era class (PIPELINE §11/§11c). The era-tag key above still
# routes the build-time dataset.
# C++ era (PRs 0-5140): ubuntu:18.04 + CMake, Google Test.
_BUNDLE_NIS_MILVUS_CPP = [
    "1538-1550-1570-1585-1620-1622-1624-1626-1629-1633-1638-1639-1640-1647-1648-1652-1657-1666-1669-1670-1672-1674-1675-1676-1677-1680-1681-1684-1687-1690-1694-1695-1696-1699-1701-1707-1716-1717-1718-1720-1722-1723-1725-1727-1729-1732-1737-1739-1743-1744-1748-1755-1757-1758-1760-1771-1776-1780-1783-1790-1802-1813-1814-1815-1816-1817-1819-1837",
    "210-219-233-239-251-257-259-279-286-290-291-296-300-323",
    "2360-2364-2371-2372-2430-2443-2451-2455-2457-2458-2462",
    "2615-2618-2630-2633-2638-2641-2644-2650-2654-2657-2659-2660-2671-2677-2678-2680-2681-2701-2708-2715-2746-2748-2750-2758-2784-2786-2788-2789-2793-2804-2806-2807-2809-2827-2829-2849-2850-2886-2888-2892-2899-2905-2906-2907-2923-2933",
    "2623-2624-2625-2626-2627-2629-2631-2632-2635-2645-2646-2652",
    "2941-2953-2968-2981-2984-3006-3007-3014-3025-3064-3067-3070-3074-3169-3232-3256-3267",
    "3269-3273-3338-3469-3470-3525-3553-3558-3568-3594-3609-3617-3647-3660-3666-3677-3684-3710-3738-3744-3761-3785",
    "4353-4408-4409-4443-4455-4474-4486-4487-4491-4493-4494-4503-4505-4506-4507-4509-4526-4535-4553-4563-4565-4569-4581",
    "4627-4641-4644-4647-4651-4653-4657-4673-4677-4681-4694-4698-4702-4703-4705-4720-4728-4733",
    "75-86-132-188",
]
for _ni in _BUNDLE_NIS_MILVUS_CPP:
    Instance.register("milvus-io", _ni)(Milvus_0_to_5140)
