import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SCRCPY_1000_TO_99999_ImageBase(Image):
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
        return "ubuntu:24.04"

    def image_tag(self) -> str:
        return "base-1000-to-99999"

    def workdir(self) -> str:
        return "base-1000-to-99999"

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
ENV LC_ALL=C.UTF-8
RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    git \\
    meson \\
    ninja-build \\
    pkg-config \\
    libsdl2-dev \\
    libavcodec-dev \\
    libavdevice-dev \\
    libavformat-dev \\
    libavutil-dev \\
    libswresample-dev \\
    libusb-1.0-0-dev \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SCRCPY_1000_TO_99999_ImageDefault(Image):
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
        return SCRCPY_1000_TO_99999_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-1000-to-99999"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}-1000-to-99999"

    def files(self) -> list[File]:
        build_cmd = "meson setup build --buildtype=debug -Dcompile_server=false"
        test_cmd = "meson test -C build --print-errorlogs"

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
set -eo pipefail

cd /home/{repo}
{build_cmd}
{test_cmd}
""".format(repo=self.pr.repo, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{build_cmd}
{test_cmd}

""".format(repo=self.pr.repo, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{build_cmd}
{test_cmd}

""".format(repo=self.pr.repo, build_cmd=build_cmd, test_cmd=test_cmd),
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


@Instance.register("Genymobile", "scrcpy_1000_to_99999")
class SCRCPY_1000_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SCRCPY_1000_TO_99999_ImageDefault(self.pr, self._config)

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

        re_result = re.compile(
            r"^\s*\d+/\d+\s+(.+?)\s+(OK|FAIL|SKIP|EXPECTEDFAIL|TIMEOUT|ERROR)\s+[\d.]+s"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            match = re_result.match(line)
            if match:
                test_name = match.group(1)
                status = match.group(2)
                if status == "OK":
                    passed_tests.add(test_name)
                elif status in ("FAIL", "TIMEOUT", "ERROR"):
                    failed_tests.add(test_name)
                elif status in ("SKIP", "EXPECTEDFAIL"):
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
