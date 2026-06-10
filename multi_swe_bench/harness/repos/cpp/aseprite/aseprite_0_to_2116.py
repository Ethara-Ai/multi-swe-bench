import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AsepriteAllegroImageBase(Image):
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
        return "base-0-to-2116"

    def workdir(self) -> str:
        return "base-0-to-2116"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates git cmake ninja-build pkg-config make \\
        g++-10 gcc-10 python3 curl unzip \\
        liballegro4-dev libtinyxml-dev \\
        libpixman-1-dev libfreetype6-dev libharfbuzz-dev zlib1g-dev \\
        libx11-dev libxcursor-dev libxi-dev libxrandr-dev libgl1-mesa-dev \\
        libfontconfig1-dev libcurl4-openssl-dev libgif-dev libjpeg-dev \\
        libpng-dev libwebp-dev \\
    && rm -rf /var/lib/apt/lists/* \\
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-10 100 \\
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-10 100 \\
    && update-alternatives --install /usr/bin/cc cc /usr/bin/gcc-10 100 \\
    && update-alternatives --install /usr/bin/c++ c++ /usr/bin/g++-10 100

{code}

RUN cd /home/{self.pr.repo} \\
    && git submodule update --init --recursive --depth 1 || true \\
    && if [ ! -d third_party/gtest/include ]; then \\
         rm -rf third_party/gtest \\
         && git clone --depth 1 https://github.com/aseprite/gtest.git third_party/gtest; \\
       fi

{self.clear_env}

"""


class AsepriteAllegroImageDefault(Image):
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
        return AsepriteAllegroImageBase(self.pr, self._config)

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
git submodule deinit --all -f 2>/dev/null || true
git reset --hard
git clean -ffdx
git checkout {pr.base.sha}
git submodule sync --recursive
git submodule update --init --recursive --depth 1 --force 2>/dev/null || true
if [ ! -d third_party/gtest/include ]; then
  rm -rf third_party/gtest
  git clone --depth 1 https://github.com/aseprite/gtest.git third_party/gtest
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CC=gcc-10
export CXX=g++-10
export CFLAGS="-Wno-error -Wno-implicit-fallthrough -Wno-array-parameter -Wno-maybe-uninitialized -Wno-stringop-overflow -Wno-stringop-truncation -Wno-stringop-overread -Wno-dangling-pointer -Wno-deprecated-declarations -Wno-restrict"
export CXXFLAGS="$CFLAGS"
rm -rf build
mkdir -p build
cd build
cmake \\
  -DCMAKE_BUILD_TYPE=Debug \\
  -DENABLE_TESTS=ON \\
  -DUSE_ALLEG4_BACKEND=on \\
  -DUSE_SKIA_BACKEND=off \\
  -DUSE_SHARED_CURL=ON \\
  -DUSE_SHARED_TINYXML=ON \\
  -DUSE_SHARED_GIFLIB=ON \\
  -DUSE_SHARED_JPEGLIB=ON \\
  -DUSE_SHARED_ZLIB=ON \\
  -DUSE_SHARED_LIBPNG=ON \\
  -DUSE_SHARED_FREETYPE=ON \\
  -DUSE_SHARED_PIXMAN=ON \\
  .. 2>&1 || true
find . \\( -name "flags.make" -o -name "build.make" -o -name "*.cmake" \\) -exec sed -i 's/-Werror\\b/-Wno-error/g' {{}} \\; 2>/dev/null
make -j4 2>&1 || make -j4 2>&1 || true
ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --binary --reject /home/test.patch 2>&1 || true
fi
export CC=gcc-10
export CXX=g++-10
export CFLAGS="-Wno-error -Wno-implicit-fallthrough -Wno-array-parameter -Wno-maybe-uninitialized -Wno-stringop-overflow -Wno-stringop-truncation -Wno-stringop-overread -Wno-dangling-pointer -Wno-deprecated-declarations -Wno-restrict"
export CXXFLAGS="$CFLAGS"
rm -rf build
mkdir -p build
cd build
cmake \\
  -DCMAKE_BUILD_TYPE=Debug \\
  -DENABLE_TESTS=ON \\
  -DUSE_ALLEG4_BACKEND=on \\
  -DUSE_SKIA_BACKEND=off \\
  -DUSE_SHARED_CURL=ON \\
  -DUSE_SHARED_TINYXML=ON \\
  -DUSE_SHARED_GIFLIB=ON \\
  -DUSE_SHARED_JPEGLIB=ON \\
  -DUSE_SHARED_ZLIB=ON \\
  -DUSE_SHARED_LIBPNG=ON \\
  -DUSE_SHARED_FREETYPE=ON \\
  -DUSE_SHARED_PIXMAN=ON \\
  .. 2>&1 || true
find . \\( -name "flags.make" -o -name "build.make" -o -name "*.cmake" \\) -exec sed -i 's/-Werror\\b/-Wno-error/g' {{}} \\; 2>/dev/null
make -j4 2>&1 || make -j4 2>&1 || true
ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --binary --reject /home/test.patch 2>&1 || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --binary --reject /home/fix.patch 2>&1 || true
fi
if [ -f .gitmodules ]; then
  PATHS=$(git config -f .gitmodules --get-regexp '^submodule\\..*\\.path$' 2>/dev/null | awk '{{print $2}}')
  for path in $PATHS; do
    [ -z "$path" ] && continue
    if [ -d "$path/.git" ] || [ -f "$path/CMakeLists.txt" ]; then continue; fi
    name=$(git config -f .gitmodules --name-only --get-regexp '\\.path$' "^$path$" 2>/dev/null | sed 's/^submodule\\.//;s/\\.path$//')
    url=$(git config -f .gitmodules --get "submodule.${{name}}.url" 2>/dev/null)
    [ -z "$url" ] && continue
    ref_sha=""
    for p in /home/test.patch /home/fix.patch; do
      [ -s "$p" ] || continue
      ref_sha=$(awk -v target="b/$path" '/^diff --git/{{n=split($0,a," ");f=a[n];sub(/^b\\//,"",f);cf=f;next}} cf==target && /^index /{{s=$2;sub(/.*\\.\\./,"",s);print s;exit}}' "$p")
      [ -n "$ref_sha" ] && break
    done
    rm -rf "$path" 2>/dev/null
    mkdir -p "$(dirname "$path")"
    timeout 90 git clone "$url" "$path" 2>&1 >/dev/null || true
    if [ -n "$ref_sha" ] && [ -d "$path/.git" ]; then
      ( cd "$path" && git fetch origin "$ref_sha" 2>&1 >/dev/null; git checkout "$ref_sha" 2>&1 >/dev/null ) || true
    fi
  done
fi
export CC=gcc-10
export CXX=g++-10
export CFLAGS="-Wno-error -Wno-implicit-fallthrough -Wno-array-parameter -Wno-maybe-uninitialized -Wno-stringop-overflow -Wno-stringop-truncation -Wno-stringop-overread -Wno-dangling-pointer -Wno-deprecated-declarations -Wno-restrict"
export CXXFLAGS="$CFLAGS"
rm -rf build
mkdir -p build
cd build
cmake \\
  -DCMAKE_BUILD_TYPE=Debug \\
  -DENABLE_TESTS=ON \\
  -DUSE_ALLEG4_BACKEND=on \\
  -DUSE_SKIA_BACKEND=off \\
  -DUSE_SHARED_CURL=ON \\
  -DUSE_SHARED_TINYXML=ON \\
  -DUSE_SHARED_GIFLIB=ON \\
  -DUSE_SHARED_JPEGLIB=ON \\
  -DUSE_SHARED_ZLIB=ON \\
  -DUSE_SHARED_LIBPNG=ON \\
  -DUSE_SHARED_FREETYPE=ON \\
  -DUSE_SHARED_PIXMAN=ON \\
  .. 2>&1 || true
find . \\( -name "flags.make" -o -name "build.make" -o -name "*.cmake" \\) -exec sed -i 's/-Werror\\b/-Wno-error/g' {{}} \\; 2>/dev/null
make -j4 2>&1 || make -j4 2>&1 || true
ctest --output-on-failure 2>&1 || true

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


@Instance.register("aseprite", "aseprite_0_to_2116")
class ASEPRITE_0_TO_2116(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AsepriteAllegroImageDefault(self.pr, self._config)

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        line_re = re.compile(
            r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(\S+)\s+\.+\s*(\*{3})?(Passed|Failed|Not Run|Timeout|Skipped|Subprocess aborted)"
        )

        for raw_line in test_log.splitlines():
            line = ansi_re.sub("", raw_line)
            m = line_re.search(line)
            if not m:
                continue
            name = m.group(1)
            status = m.group(3)
            if status == "Passed":
                passed_tests.add(name)
            elif status in ("Failed", "Timeout", "Subprocess aborted"):
                failed_tests.add(name)
            elif status in ("Not Run", "Skipped"):
                skipped_tests.add(name)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
