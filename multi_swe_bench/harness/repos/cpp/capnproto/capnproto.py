from __future__ import annotations

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


# number_interval used by this 1069 .. 1086 bundle. PRs carry
# number_interval="capnproto_1086_to_1069", and Instance.create() routes on
# f"{org}/{number_interval}" -- so the same config must be registered under both
# "capnproto/capnproto" and "capnproto/capnproto_1086_to_1069" (see bottom).
_NUMBER_INTERVAL = "capnproto_1086_to_1069"


def _select_toolchain(pr: PullRequest) -> tuple[str, str, str]:
    """Map a PR to its (base_image, tag_suffix, compiler) toolchain triple.

    Kept as a free function so the toolchain base, the shared repo image, and
    the per-PR image all agree on the same era without duplicating the rules.
    """
    # PR #1730: fix patch requires C++20 (coroutines), gcc:10 can't compile it
    _clang_overrides = {1730}
    # PR #2385: fix patch requires C++23 (#include <print>), clang-14 can't handle it
    # PR #2410: base has linker bug (missing kj-async link), fix adds it + upgrades to C++23
    #           needs gcc:latest for C++23 support; linker bug in base is expected
    _gcc_latest_overrides = {2385, 2410}

    if pr.number in _clang_overrides:
        return ("ubuntu:22.04", "clang-14", "clang")
    if pr.number in _gcc_latest_overrides:
        return ("gcc:latest", "latest", "gcc")
    if pr.number <= 1730:
        # Pin the cpp-10 era to a specific, reproducible toolchain instead of
        # the floating `gcc:10` tag. `gcc:10` is a moving tag whose Debian base
        # (and thus glibc/binutils/cmake) drifts over time, which can break the
        # 2020-era capnproto sources for PRs like #1069 once the upstream tag
        # is rebuilt on a newer base. `gcc:10.5.0-bullseye` is the exact image
        # that compiles this era cleanly (GCC 10.5.0 / Debian 11 / CMake 3.18).
        return ("gcc:10.5.0-bullseye", "cpp-10", "gcc")
    elif pr.number <= 2409:
        return ("ubuntu:22.04", "clang-14", "clang")
    return ("gcc:latest", "latest", "gcc")


class CapnprotoImageBase(Image):
    """Level 1: toolchain-only base image (shared across all PRs of an era).

    IMPORTANT: this image must NOT clone the repository. image.py's
    DockerfileEnhancer force-injects a "checkout ${BASE_COMMIT} + strip all
    history + remove origin" hardening block into ANY image whose dependency()
    is a string (an external base image) and that performs a `git clone`.
    Because this base image is shared by every PR of an era, pinning it to a
    single BASE_COMMIT and gc-pruning the rest of history would make
    `git checkout <base.sha>` fail for every other PR sharing the era. So the
    clone lives in CapnprotoImageDefault (whose dependency() is an Image, which
    the enhancer leaves untouched), where it is done per-PR. This image only
    provides the C/C++ build toolchain.
    """

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

        # Clang-based images need clang installed explicitly
        if self._compiler == "clang":
            extra_packages = "clang \\\n    git \\\n    "
            env_prefix = "ENV DEBIAN_FRONTEND=noninteractive\n"
        else:
            extra_packages = ""
            env_prefix = ""

        # No `git clone` here on purpose — see the class docstring. The string
        # dependency means DockerfileEnhancer runs over this Dockerfile, but with
        # no clone/COPY — DockerfileEnhancer injects label infra (no hardening).
        return f"""FROM {image_name}

{env_prefix}
WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    {extra_packages}cmake \\
    autoconf \\
    automake \\
    libtool \\
    pkg-config \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

"""


class CapnprotoImageDefault(Image):
    """Level 2: per-PR image. Clones the repo, checks out base.sha, and strips
    history in-image.

    Depends on CapnprotoImageBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim — no BASE_COMMIT
    pinning, no history stripping, no origin removal injected by the pipeline.
    The clone therefore lives here, per-PR: the shared toolchain base must NOT
    clone (a shared string-dependency image that clones would be pinned and
    history-stripped by the enhancer, breaking every other PR of the era), so
    the per-PR image clones full history, checks out ${BASE_COMMIT} inline, then
    the hardening block strips origin/refs/future history while keeping
    base.sha reachable.
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
        base_image, tag_suffix, compiler = _select_toolchain(self.pr)
        return CapnprotoImageBase(
            self.pr, self._config, base_image, tag_suffix, compiler
        )

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _cmake_flags(self) -> str:
        _, _, compiler = _select_toolchain(self.pr)
        if compiler == "clang":
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
                "prepare.sh",
                # Checkout is done inline in the Dockerfile (RUN git checkout
                # ${BASE_COMMIT}); prepare.sh only sets up the out-of-source
                # CMake build directory.
                """#!/bin/bash
set -e

cd /home/{pr.repo}/c++
mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}/c++/build
# Rebuild the CMake cache against the current build-directory path. A
# CMakeCache.txt left over from a configure at a different absolute path
# (e.g. the tree was built at /home/ and is now used from /workspace/) pins
# the old source/binary dirs and makes `cmake ..` abort. Removing the cache
# forces CMake to regenerate it for wherever the build dir currently lives.
rm -f CMakeCache.txt
rm -rf CMakeFiles
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
# Rebuild the CMake cache against the current path (see run.sh).
rm -f CMakeCache.txt
rm -rf CMakeFiles
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
# Rebuild the CMake cache against the current path (see run.sh). The fix
# patch (e.g. #1086) edits CMakeLists.txt, so a stale cache must not be
# reused -- regenerate it so the new build rules take effect.
rm -f CMakeCache.txt
rm -rf CMakeFiles
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

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim — the clone + hardening below are kept as written
        # (and pinning here is correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh

"""

        # Anti-reward-hacking hardening — the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + assertions, then submodule
        # strip). Concatenated raw (not via f-string) so its ${BASE_COMMIT} /
        # %(refname) tokens stay literal.
        return header + Image._HARDENING_BLOCK + '\nCMD ["/bin/bash"]\n'


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


# Route PRs that carry number_interval="capnproto_1086_to_1069" to the same
# Capnproto config (Instance.create looks up f"{org}/{number_interval}").
Instance.register("capnproto", _NUMBER_INTERVAL)(Capnproto)


# Route the dash-joined number_interval (the bundled PR/issue numbers from
# each record's resolved_issues) to the same Capnproto config.
# Instance.create() looks up f"{org}/{number_interval}".
Instance.register("capnproto", "1069-1071-1072-1073-1082-1083-1088-1091")(Capnproto)  # base PR 1069
Instance.register("capnproto", "1086-1087-1094-1096-1098-1099-1100-1102-1103-1105-1108-1110-1111-1114-1115-1117")(Capnproto)  # base PR 1086
Instance.register("capnproto", "1069-1071-1072-1073-1079-1082-1083-1085-1088-1091")(Capnproto)  # genuine dataset, base PR 1069
Instance.register("capnproto", "1075-1087-1089-1094-1096-1098-1099-1100-1102-1103-1105-1108-1110-1111-1114-1115-1117")(Capnproto)  # genuine dataset, base PR 1086
