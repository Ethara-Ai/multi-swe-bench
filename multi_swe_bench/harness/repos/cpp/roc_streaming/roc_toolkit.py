import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Binary diffs cause 'cannot apply binary patch without full index line'
    errors with git apply.
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


class RocToolkitImageBase(Image):
    """Base image for roc-streaming/roc-toolkit.

    Uses gcc:12-bookworm with all system dependencies pre-installed.
    Builds OpenFEC and CppUTest from source via scons --build-3rdparty.
    The initial scons build is done at base image time so per-PR images
    only need incremental rebuilds.

    MUST set CXX=g++ CC=gcc to avoid version mismatch between
    g++-12 (12.2.0, Debian) and g++ (12.5.0, gcc image default).
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

    def dependency(self) -> Union[str, "Image"]:
        return "gcc:12-bookworm"

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
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}\n"
                f"RUN cd /home/{self.pr.repo} && git fetch origin '+refs/heads/*:refs/remotes/origin/*' '+refs/pull/*/merge:refs/remotes/origin/pr/*/merge'"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git \\
    scons \\
    ragel \\
    gengetopt \\
    pkg-config \\
    cmake \\
    g++ \\
    libuv1-dev \\
    libunwind-dev \\
    libpulse-dev \\
    libasound2-dev \\
    libsndfile1-dev \\
    libspeexdsp-dev \\
    libssl-dev \\
    libtool \\
    intltool \\
    autoconf \\
    automake \\
    libcpputest-dev \\
    libsox-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

RUN cd /home/{self.pr.repo} && \\
    CXX=g++ CC=gcc scons -Q --enable-tests --build-3rdparty=openfec,cpputest test || true

{self.clear_env}

"""


class RocToolkitImageDefault(Image):
    """Per-PR image for roc-streaming/roc-toolkit.

    All PRs use the same gcc:12-bookworm base. Build system is SCons.
    Test framework is CppUTest with verbose output via -v flag.

    Test binaries are at build/src/<triple>/<compiler>-release/roc-test-*
    where triple varies by architecture (e.g. aarch64-pc-linux-gnu,
    x86_64-pc-linux-gnu). Scripts use find to locate them.
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
        return RocToolkitImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _filter_binary_patches(self.pr.fix_patch)
        filtered_test_patch = _filter_binary_patches(self.pr.test_patch)

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
CXX=g++ CC=gcc scons -Q --enable-tests --build-3rdparty=openfec,cpputest 2>&1

for tb in $(find build/src -name 'roc-test-*' -type f 2>/dev/null | sort); do
  echo "=== Running $tb ==="
  "$tb" -v 2>&1 || true
done
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

CXX=g++ CC=gcc scons -Q --enable-tests --build-3rdparty=openfec,cpputest 2>&1 || true

for tb in $(find build/src -name 'roc-test-*' -type f 2>/dev/null | sort); do
  echo "=== Running $tb ==="
  "$tb" -v 2>&1 || true
done

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

CXX=g++ CC=gcc scons -Q --enable-tests --build-3rdparty=openfec,cpputest 2>&1 || true

for tb in $(find build/src -name 'roc-test-*' -type f 2>/dev/null | sort); do
  echo "=== Running $tb ==="
  "$tb" -v 2>&1 || true
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


@Instance.register("roc-streaming", "roc-toolkit")
class RocStreamingRocToolkit(Instance):
    """Instance for roc-streaming/roc-toolkit.

    Build system: SCons with --build-3rdparty=openfec,cpputest
    Test framework: CppUTest with verbose output (-v flag)
    Single era: all 38 PRs use identical build/test commands.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RocToolkitImageDefault(self.pr, self._config)

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
        """Parse CppUTest verbose output.

        Verbose format per test (timing may be on same line or separate):
          TEST(group_name, test_name) - Xms
          TEST(group_name, test_name)<debug noise>\\n ... \\n - Xms
          IGNORE_TEST(group_name, test_name) - Xms

        Failure format:
          /path/file.cpp:123: error: Failure in TEST(GroupName, TestName)

        Summary line (per binary):
          OK (N tests, N ran, N checks, N ignored, 0 filtered out, Xms)
          Errors (N failures, N tests, N ran, ...)
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_test_start = re.compile(r"^TEST\((\w+),\s*(\w+)\)")
        re_ignore_start = re.compile(r"^IGNORE_TEST\((\w+),\s*(\w+)\)")
        re_timing = re.compile(r"^\s*-\s+\d+\s*ms\s*$")
        re_fail = re.compile(r"error:\s+Failure in TEST\((\w+),\s*(\w+)\)")

        current_test = None

        for line in test_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            fail_match = re_fail.search(stripped)
            if fail_match:
                test_name = f"TEST({fail_match.group(1)}, {fail_match.group(2)})"
                failed_tests.add(test_name)
                if current_test == test_name:
                    current_test = None
                continue

            ignore_match = re_ignore_start.match(stripped)
            if ignore_match:
                test_name = f"IGNORE_TEST({ignore_match.group(1)}, {ignore_match.group(2)})"
                skipped_tests.add(test_name)
                current_test = None
                continue

            test_match = re_test_start.match(stripped)
            if test_match:
                current_test = f"TEST({test_match.group(1)}, {test_match.group(2)})"
                if re.search(r"-\s+\d+\s*ms\s*$", stripped):
                    if current_test not in failed_tests:
                        passed_tests.add(current_test)
                    current_test = None
                continue

            if current_test and re_timing.match(stripped):
                if current_test not in failed_tests:
                    passed_tests.add(current_test)
                current_test = None

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
