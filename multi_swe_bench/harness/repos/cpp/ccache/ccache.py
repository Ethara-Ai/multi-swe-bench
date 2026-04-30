import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "gcc:9"

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

        # Superset of all dependencies across both eras:
        # - autotools (PR#596): autoconf, automake, libtool, libb2-dev, gperf
        # - cmake (PR#620+): cmake, ninja-build, libhiredis-dev, redis-server, redis-tools
        # - common: build-essential, pkg-config, libzstd-dev, elfutils, python3, bash, perl, git
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y \\
    build-essential \\
    cmake \\
    ninja-build \\
    pkg-config \\
    autoconf \\
    automake \\
    libtool \\
    gperf \\
    libzstd-dev \\
    libb2-dev \\
    libhiredis-dev \\
    elfutils \\
    python3 \\
    bash \\
    perl \\
    redis-server \\
    redis-tools \\
    git \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _is_autotools(self) -> bool:
        return self.pr.number <= 596

    def files(self) -> list[File]:
        check_git = File(
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
        )

        if self._is_autotools():
            return [
                File(".", "fix.patch", f"{self.pr.fix_patch}"),
                File(".", "test.patch", f"{self.pr.test_patch}"),
                check_git,
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

./autogen.sh
./configure
make -j$(nproc) || true

""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "run.sh",
                    """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
./autogen.sh
./configure
make -j$(nproc)
CC=gcc make test

""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "test-run.sh",
                    """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./autogen.sh
./configure
make -j$(nproc)
CC=gcc make test

""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "fix-run.sh",
                    """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./autogen.sh
./configure
make -j$(nproc)
CC=gcc make test

""".format(pr=self.pr),
                ),
            ]
        else:
            return [
                File(".", "fix.patch", f"{self.pr.fix_patch}"),
                File(".", "test.patch", f"{self.pr.test_patch}"),
                check_git,
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

mkdir -p build && cd build
cmake -DCMAKE_BUILD_TYPE=Release .. || true
cmake --build . -j$(nproc) || true

""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "run.sh",
                    """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
rm -rf build && mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
cmake --build . -j$(nproc)
ctest --output-on-failure

""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "test-run.sh",
                    """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
rm -rf build && mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
cmake --build . -j$(nproc)
ctest --output-on-failure

""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "fix-run.sh",
                    """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
rm -rf build && mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
cmake --build . -j$(nproc)
ctest --output-on-failure

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


@Instance.register("ccache", "ccache")
class Ccache(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

    def _is_autotools(self) -> bool:
        return self.pr.number <= 596

    def parse_log(self, test_log: str) -> TestResult:
        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        if self._is_autotools():
            self._parse_autotools_log(
                test_log, passed_tests, failed_tests, skipped_tests
            )
        else:
            self._parse_ctest_log(
                test_log, passed_tests, failed_tests, skipped_tests
            )

        # Deduplication: worst-result-wins
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    def _parse_autotools_log(
        self,
        test_log: str,
        passed_tests: set,
        failed_tests: set,
        skipped_tests: set,
    ) -> None:
        # Bash test suite output:
        #   Running test suite <name>........
        #   Skipped test suite <name> [reason]
        #   PASSED / FAILED
        re_running = re.compile(r"^Running test suite (\w+)")
        re_skipped = re.compile(r"^Skipped test suite (\w+)")

        # Catch2 unittest output:
        #   All tests passed (N assertions in N test cases)
        re_catch2_pass = re.compile(
            r"^All tests passed \(\d+ assertions? in (\d+) test cases?\)"
        )
        re_catch2_fail = re.compile(
            r"^test cases:\s*(\d+)\s*\|\s*(\d+)\s*passed\s*\|\s*(\d+)\s*failed"
        )

        current_suites = []

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_running.match(line)
            if m:
                current_suites.append(m.group(1))
                continue

            m = re_skipped.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

            if line == "PASSED":
                for suite in current_suites:
                    passed_tests.add(suite)
                current_suites = []
                continue

            if line == "FAILED":
                for suite in current_suites:
                    failed_tests.add(suite)
                current_suites = []
                continue

            m = re_catch2_pass.match(line)
            if m:
                passed_tests.add("unittest")
                continue

            m = re_catch2_fail.match(line)
            if m:
                failed_tests.add("unittest")
                continue

    def _parse_ctest_log(
        self,
        test_log: str,
        passed_tests: set,
        failed_tests: set,
        skipped_tests: set,
    ) -> None:
        # CTest output format:
        #   N/M Test  #N: test.name ........................   Passed    X.XX sec
        #   N/M Test  #N: test.name ........................***Failed    X.XX sec
        #   N/M Test  #N: test.name ........................***Skipped   X.XX sec
        re_pass = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"
        )
        re_fail = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"
        )
        re_skip = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Skipped\s+.*$"
        )
        re_exception = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"
        )
        re_not_run = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"
        )
        re_timeout = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

            m = re_exception.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_not_run.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

            m = re_timeout.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue
