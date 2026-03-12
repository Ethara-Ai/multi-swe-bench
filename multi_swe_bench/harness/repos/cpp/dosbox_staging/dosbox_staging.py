import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DosboxStagingImageBase(Image):
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

RUN apt-get update && apt-get install -y \\
    build-essential \\
    ca-certificates \\
    ccache \\
    cmake \\
    curl \\
    g++ \\
    git \\
    libasound2-dev \\
    libatomic1 \\
    libavcodec-dev \\
    libavformat-dev \\
    libavutil-dev \\
    libdecor-0-dev \\
    libdrm-dev \\
    libfluidsynth-dev \\
    libgbm-dev \\
    libgtest-dev \\
    libjack-dev \\
    libopusfile-dev \\
    libpipewire-0.3-dev \\
    libpng-dev \\
    libpulse-dev \\
    libsamplerate0-dev \\
    libsdl2-dev \\
    libsdl2-net-dev \\
    libslirp-dev \\
    libsndio-dev \\
    libspeexdsp-dev \\
    libswresample-dev \\
    libswscale-dev \\
    libudev-dev \\
    libvulkan-dev \\
    libwayland-dev \\
    libx11-dev \\
    libxcursor-dev \\
    libxfixes-dev \\
    libxi-dev \\
    libxkbcommon-dev \\
    libxrandr-dev \\
    libxss-dev \\
    ninja-build \\
    pkg-config \\
    python3 \\
    python3-pip \\
    python3-setuptools \\
    wayland-protocols \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

# Install recent meson via pip (Ubuntu 22.04 repo version may be too old for newer commits)
RUN python3 -m pip install --break-system-packages meson || python3 -m pip install meson

{code}

{self.clear_env}

"""


class DosboxStagingImageDefault(Image):
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
        return DosboxStagingImageBase(self.pr, self._config)

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

# Build with meson - force unit_tests enabled (default buildtype=release disables them)
if ! meson setup build -Dunit_tests=enabled 2>&1; then
    echo "=== Default meson setup failed, trying with forcefallback ==="
    rm -rf build
    meson setup build -Dunit_tests=enabled --wrap-mode=forcefallback 2>&1 || true
fi

meson compile -C build 2>&1
meson test -C build --no-rebuild --print-errorlogs 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

# Build with meson - force unit_tests enabled (default buildtype=release disables them)
if ! meson setup build -Dunit_tests=enabled 2>&1; then
    echo "=== Default meson setup failed, trying with forcefallback ==="
    rm -rf build
    meson setup build -Dunit_tests=enabled --wrap-mode=forcefallback 2>&1 || true
fi

meson compile -C build 2>&1
meson test -C build --no-rebuild --print-errorlogs 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Build with meson - force unit_tests enabled (default buildtype=release disables them)
if ! meson setup build -Dunit_tests=enabled 2>&1; then
    echo "=== Default meson setup failed, trying with forcefallback ==="
    rm -rf build
    meson setup build -Dunit_tests=enabled --wrap-mode=forcefallback 2>&1 || true
fi

meson compile -C build 2>&1
meson test -C build --no-rebuild --print-errorlogs 2>&1 || true

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


@Instance.register("dosbox-staging", "dosbox-staging")
class DosboxStaging(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DosboxStagingImageDefault(self.pr, self._config)

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

        # Meson test output format:
        #   1/26 bitops                          OK              0.01s
        #   2/26 math_utils                      FAIL            0.45s
        #   3/26 setup                           SKIP            0.00s
        #   4/26 some_test                       TIMEOUT         30.00s
        #   5/26 expected                        EXPECTEDFAIL    0.10s
        re_meson_pass = re.compile(r"^\s*\d+/\d+\s+(.*?)\s+OK\s+[\d.]+s\s*$")
        re_meson_fail = re.compile(r"^\s*\d+/\d+\s+(.*?)\s+FAIL\s+[\d.]+s\s*$")
        re_meson_timeout = re.compile(r"^\s*\d+/\d+\s+(.*?)\s+TIMEOUT\s+[\d.]+s\s*$")
        re_meson_skip = re.compile(r"^\s*\d+/\d+\s+(.*?)\s+SKIP\s+[\d.]+s\s*$")
        re_meson_expectedfail = re.compile(
            r"^\s*\d+/\d+\s+(.*?)\s+EXPECTEDFAIL\s+[\d.]+s\s*$"
        )

        # CTest output format (fallback):
        #   1/10 Test  #1: test_name ................   Passed    0.01 sec
        #   2/10 Test  #2: another_test .............***Failed    0.05 sec
        re_ctest_pass = re.compile(
            r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.+\s+Passed", re.IGNORECASE
        )
        re_ctest_fail = re.compile(
            r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.+\*+Failed", re.IGNORECASE
        )
        re_ctest_skip = re.compile(
            r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.+\*+(?:Not Run|Disabled|Skipped)",
            re.IGNORECASE,
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_meson_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_meson_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_meson_timeout.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_meson_skip.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

            m = re_meson_expectedfail.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_ctest_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_ctest_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_ctest_skip.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
