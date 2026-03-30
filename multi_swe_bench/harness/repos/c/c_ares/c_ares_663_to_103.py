import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class C_ARES_663_TO_103_ImageBase(Image):
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
        return "debian:latest"

    def image_tag(self) -> str:
        return "base-663-to-103"

    def workdir(self) -> str:
        return "base-663-to-103"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/c-ares"
        else:
            code = f"COPY c-ares /home/c-ares"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y \\
    build-essential \\
    cmake \\
    git \\
    autoconf \\
    automake \\
    libtool \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class C_ARES_663_TO_103_ImageDefault(Image):
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
        return C_ARES_663_TO_103_ImageBase(self.pr, self.config)

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

cd /home/c-ares
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

mkdir -p cmakebld && cd cmakebld
cmake -DCMAKE_BUILD_TYPE=DEBUG -DCARES_STATIC=ON -DCARES_STATIC_PIC=ON -DCARES_BUILD_TESTS=ON ..
cmake --build . -j$(nproc) || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/c-ares
rm -rf cmakebld && mkdir cmakebld && cd cmakebld
cmake -DCMAKE_BUILD_TYPE=DEBUG -DCARES_STATIC=ON -DCARES_STATIC_PIC=ON -DCARES_BUILD_TESTS=ON ..
cmake --build . -j$(nproc)
./bin/arestest -4 -v --gtest_filter=-*LiveSearchTXT*:*LiveSearchANY*:*LiveSearchSOA*

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/c-ares
git apply --whitespace=nowarn /home/test.patch
rm -rf cmakebld && mkdir cmakebld && cd cmakebld
cmake -DCMAKE_BUILD_TYPE=DEBUG -DCARES_STATIC=ON -DCARES_STATIC_PIC=ON -DCARES_BUILD_TESTS=ON ..
cmake --build . -j$(nproc)
./bin/arestest -4 -v --gtest_filter=-*LiveSearchTXT*:*LiveSearchANY*:*LiveSearchSOA*

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/c-ares
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
rm -rf cmakebld && mkdir cmakebld && cd cmakebld
cmake -DCMAKE_BUILD_TYPE=DEBUG -DCARES_STATIC=ON -DCARES_STATIC_PIC=ON -DCARES_BUILD_TESTS=ON ..
cmake --build . -j$(nproc)
./bin/arestest -4 -v --gtest_filter=-*LiveSearchTXT*:*LiveSearchANY*:*LiveSearchSOA*

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


@Instance.register("c-ares", "c_ares_663_to_103")
class C_ARES_663_TO_103(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return C_ARES_663_TO_103_ImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass = re.compile(r"^\[\s*OK\s*\]\s+(\S+\.\S+)")
        re_fail = re.compile(r"^\[\s*FAILED\s*\]\s+(\S+\.\S+)")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))

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
