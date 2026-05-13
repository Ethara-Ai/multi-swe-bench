import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# =============================================================================
# Genymobile/scrcpy — C (meson), early era (PRs #28–#542, v1.0–v1.8)
# ubuntu:22.04, meson 0.61 + ninja, debug build
#
# Early versions use the ffmpeg 4.x API (AVCodecContext, avcodec_decode_video2,
# LIBAVCODEC_VERSION_MINOR etc.) which is incompatible with ffmpeg 6.x on
# ubuntu:24.04.  ubuntu:22.04 ships ffmpeg 4.x and meson 0.61 which supports
# the older meson.build syntax (input: '.' in custom_target).
#
# The server (Java/Android) is replaced by a dummy jar via the prebuilt_server
# meson option — the Android SDK is not available in the harness Docker
# environment.  Only the C unit tests under app/tests/ are executed.
#
# Verified interactively in Docker against:
#   - PR #28  base 727d1ef1 (scrcpy "undefined", earliest dataset entry)
#   - PR #542 base 1323e3c4 (scrcpy 1.8, latest early-era dataset entry)
# Both build and test successfully.
# =============================================================================


class SCRCPY_0_TO_999_ImageBase(Image):
    """scrcpy early era base: ubuntu:22.04, meson + ninja, ffmpeg 4.x / SDL2."""

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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-0-to-999"

    def workdir(self) -> str:
        return "base-0-to-999"

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
    && apt-get clean && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SCRCPY_0_TO_999_ImageDefault(Image):
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
        return SCRCPY_0_TO_999_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-0-to-999"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}-0-to-999"

    def files(self) -> list[File]:
        # Early scrcpy doesn't have compile_server — it uses build_server.
        # The server is an Android Java project requiring the Android SDK, so
        # we stub it with a prebuilt dummy jar.
        build_cmd = (
            "touch /tmp/dummy.jar && "
            "meson setup build --buildtype=debug -Dprebuilt_server=/tmp/dummy.jar && "
            "ninja -C build"
        )
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


# =============================================================================
# Instance — early era handler for Genymobile/scrcpy (PRs #0–#999)
# =============================================================================


@Instance.register("Genymobile", "scrcpy_0_to_999")
class SCRCPY_0_TO_999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SCRCPY_0_TO_999_ImageDefault(self.pr, self._config)

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

        # Meson test output format (captured from real Docker run):
        #  1/3 test_control_event_queue     OK              0.01s
        #  2/3 test_control_event_serialize OK              0.01s
        #  3/3 test_strutil                 OK              0.00s
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
