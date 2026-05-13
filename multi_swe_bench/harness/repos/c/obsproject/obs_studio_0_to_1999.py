import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OBS_STUDIO_0_TO_1999_ImageBase(Image):
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
        return "ubuntu:18.04"

    def image_tag(self) -> str:
        return "base-0-to-1999"

    def workdir(self) -> str:
        return "base-0-to-1999"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/obs-studio"
        else:
            code = f"COPY obs-studio /home/obs-studio"

        return f"""FROM {image_name}
{self.global_env}
WORKDIR /home/
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    build-essential \\
    cmake \\
    git \\
    pkg-config \\
    qtbase5-dev \\
    libqt5svg5-dev \\
    libqt5x11extras5-dev \\
    libavcodec-dev \\
    libavformat-dev \\
    libavutil-dev \\
    libswscale-dev \\
    libswresample-dev \\
    libavdevice-dev \\
    libavfilter-dev \\
    libjansson-dev \\
    libcurl4-openssl-dev \\
    libx264-dev \\
    libgl1-mesa-dev \\
    libwayland-dev \\
    libxkbcommon-dev \\
    libpulse-dev \\
    libasound2-dev \\
    libv4l-dev \\
    libudev-dev \\
    libspeexdsp-dev \\
    libfreetype6-dev \\
    libfontconfig-dev \\
    libx11-xcb-dev \\
    libx11-dev \\
    libxcb-randr0-dev \\
    libxcb-shm0-dev \\
    libxcb-composite0-dev \\
    libxcomposite-dev \\
    libxinerama-dev \\
    libxcb-xfixes0-dev \\
    libxcb-xinerama0-dev \\
    libfdk-aac-dev \\
    libvlc-dev \\
    libpci-dev \\
    libdrm-dev \\
    libva-dev \\
    zlib1g-dev \\
    libjack-jackd2-dev \\
    libssl-dev \\
    libmbedtls-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

RUN cd /home/obs-studio && git submodule update --init --recursive || true
{self.clear_env}
"""


class OBS_STUDIO_0_TO_1999_ImageDefault(Image):
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
        return OBS_STUDIO_0_TO_1999_ImageBase(self.pr, self.config)

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

if [[ -n $(git status --porcelain --ignore-submodules) ]]; then
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

cd /home/obs-studio
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
git clean -fdx -- plugins/ || true
git submodule update --init --recursive || true

mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Debug \\
    -DBUILD_BROWSER=OFF \\
    -DBUILD_TESTS=ON || true
cmake --build . -j$(nproc) || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/obs-studio
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Debug \
    -DBUILD_BROWSER=OFF \
    -DBUILD_TESTS=ON 2>&1
cmake --build . -j$(nproc) 2>&1

echo "[       OK ] build_complete"

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/obs-studio
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
find . -name "*.rej" -delete
find . -name "*.qrc" | while read qrc; do
    qrc_dir=$(dirname "$qrc")
    grep -oP '(?<=>)[^<]+' "$qrc" 2>/dev/null | while read f; do
        if [ ! -f "$qrc_dir/$f" ]; then mkdir -p "$(dirname "$qrc_dir/$f")" && touch "$qrc_dir/$f" 2>/dev/null; fi
    done
done || true

mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Debug \
    -DBUILD_BROWSER=OFF \
    -DBUILD_TESTS=ON 2>&1
cmake --build . -j$(nproc) 2>&1

echo "[       OK ] build_complete"

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/obs-studio
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
find . -name "*.rej" -delete
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --reject /home/fix.patch || true
find . -name "*.rej" -delete
find . -name "*.qrc" | while read qrc; do
    qrc_dir=$(dirname "$qrc")
    grep -oP '(?<=>)[^<]+' "$qrc" 2>/dev/null | while read f; do
        if [ ! -f "$qrc_dir/$f" ]; then mkdir -p "$(dirname "$qrc_dir/$f")" && touch "$qrc_dir/$f" 2>/dev/null; fi
    done
done || true

mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Debug \
    -DBUILD_BROWSER=OFF \
    -DBUILD_TESTS=ON 2>&1
cmake --build . -j$(nproc) 2>&1

echo "[       OK ] build_complete"

""",
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


@Instance.register("obsproject", "obs_studio_0_to_1999")
class OBS_STUDIO_0_TO_1999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OBS_STUDIO_0_TO_1999_ImageDefault(self.pr, self._config)

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
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass = re.compile(r"^\[\s+OK\s+\]\s+(.+)$")
        re_fail = re.compile(r"^\[\s+FAILED\s+\]\s+(.+)$")
        re_summary = re.compile(r"^\d+\s+test\(s\)")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                test_name = m.group(1).strip()
                if not re_summary.match(test_name):
                    passed_tests.add(test_name)
                continue
            m = re_fail.match(line)
            if m:
                test_name = m.group(1).strip()
                if not re_summary.match(test_name):
                    failed_tests.add(test_name)
                continue

        linker_error_pattern = re.compile(r"undefined reference to `([^']+)'")
        compile_error_pattern = re.compile(r"^(/\S+\.\w+):\d+:\d+: error:")
        make_error_pattern = re.compile(r"^make(?:\[\d+\])?: \*\*\* .+ Error \d+")
        cmake_error_pattern = re.compile(r"^CMake Error")
        patch_fail_pattern = re.compile(r"^error: patch failed: (\S+)")
        patch_no_apply_pattern = re.compile(r"^error: (\S+): patch does not apply")

        detected_failures = set()
        for line in test_log.splitlines():
            line = line.strip()

            m = linker_error_pattern.search(line)
            if m:
                detected_failures.add(f"__linker_error_{m.group(1)}")

            m = compile_error_pattern.search(line)
            if m:
                detected_failures.add(f"__compile_error_{m.group(1)}")

            m = make_error_pattern.search(line)
            if m:
                detected_failures.add("__make_error__")

            m = cmake_error_pattern.search(line)
            if m:
                detected_failures.add("__cmake_error__")

            m = patch_fail_pattern.search(line)
            if m:
                detected_failures.add(f"__patch_failed_{m.group(1)}")

            m = patch_no_apply_pattern.search(line)
            if m:
                detected_failures.add(f"__patch_no_apply_{m.group(1)}")

        if detected_failures and len(passed_tests) == 0 and len(failed_tests) == 0:
            failed_tests = detected_failures

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
