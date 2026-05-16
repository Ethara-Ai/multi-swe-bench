import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImHexEra2ImageBase(Image):
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
        return "base-cxx23"

    def workdir(self) -> str:
        return "base-cxx23"

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

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        build-essential gcc-12 g++-12 lld pkg-config cmake ccache ninja-build make \\
        git ca-certificates python3 python3-dev perl autoconf automake libtool \\
        libglfw3-dev libglm-dev libmagic-dev libmbedtls-dev libfreetype-dev \\
        libdbus-1-dev libcurl4-gnutls-dev libgtk-3-dev libssl-dev libcrypto++-dev \\
        nlohmann-json3-dev libcapstone-dev libyara-dev libcli11-dev \\
        zlib1g-dev libbz2-dev liblzma-dev libzstd-dev libpsl-dev libfmt-dev \\
        libssh2-1-dev libidn2-dev libnghttp2-dev librtmp-dev libkrb5-dev libldap2-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 100 && \\
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 100

{code}

{self.clear_env}

"""


class ImHexEra2ImageDefault(Image):
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
        return ImHexEra2ImageBase(self.pr, self._config)

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
                "init_submodules.sh",
                """#!/bin/bash
# Robust submodule init.
# Standard `git submodule update --init` silently swallows failures (network
# under QEMU, deleted submodule refs, etc.). When a submodule dir stays empty
# (or worse, has only a .git gitfile pointer and no real content), downstream
# cmake fails with "External dependency ... is empty".
#
# Strategy:
#   1) git submodule sync (refresh URL state)
#   2) git submodule update --init --recursive --force --depth=1 (twice, swallow errors)
#   3) For every .gitmodules entry, check if the path is empty *of real content*
#      (any non-hidden file/dir). If empty, rm -rf and explicit clone from URL,
#      then recursively init the clone's own submodules.
set +e

# Single pass that processes a .gitmodules-bearing directory.
init_one_repo() {
  local here="$1"
  ( cd "$here" 2>/dev/null || return 0
    [ ! -f .gitmodules ] && return 0
    git submodule sync --recursive 2>&1
    git submodule update --init --recursive --force --depth=1 2>&1
    git submodule update --init --recursive --force --depth=1 2>&1
    git config --file .gitmodules --get-regexp '^submodule\\..*\\.path$' 2>/dev/null | while read key path; do
      name=$(echo "$key" | sed 's/^submodule\\.//; s/\\.path$//')
      url=$(git config --file .gitmodules --get "submodule.${name}.url" 2>/dev/null)
      # "Empty of real content" = no non-hidden entries (ls without -A ignores dotfiles).
      # A populated submodule will have CMakeLists.txt or src/ etc.; a stale .git pointer alone
      # leaves ls "" so the fallback fires.
      visible_count=$(ls -1 "$path" 2>/dev/null | wc -l)
      if [ -n "$url" ] && { [ ! -d "$path" ] || [ "$visible_count" = "0" ]; }; then
        echo "init_submodules: fallback clone $url -> $here/$path"
        rm -rf "$path"
        git clone --depth=1 --recursive "$url" "$path" 2>&1 \\
          || git clone --recursive "$url" "$path" 2>&1 \\
          || true
        # Recurse into the newly cloned dir to init ITS nested submodules.
        init_one_repo "$path"
      fi
    done
  )
}

init_one_repo "$(pwd)"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
git clean -fdx -e build
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/init_submodules.sh
bash /home/check_git_changes.sh
mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
bash /home/init_submodules.sh
cd build
cmake -GNinja \\
  -DCMAKE_BUILD_TYPE=Release \\
  -DCMAKE_C_COMPILER=gcc-12 \\
  -DCMAKE_CXX_COMPILER=g++-12 \\
  -DIMHEX_ENABLE_UNIT_TESTS=ON \\
  -DIMHEX_OFFLINE_BUILD=ON \\
  -DIMHEX_STRICT_WARNINGS=OFF \\
  -DIMHEX_IGNORE_BAD_COMPILER=ON \\
  -DIMHEX_BUNDLE_DOTNET=OFF \\
  -DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON \\
  ..
cmake --build . -j $(nproc) --target unit_tests
ctest --output-on-failure
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
bash /home/init_submodules.sh
cd build
cmake -GNinja \\
  -DCMAKE_BUILD_TYPE=Release \\
  -DCMAKE_C_COMPILER=gcc-12 \\
  -DCMAKE_CXX_COMPILER=g++-12 \\
  -DIMHEX_ENABLE_UNIT_TESTS=ON \\
  -DIMHEX_OFFLINE_BUILD=ON \\
  -DIMHEX_STRICT_WARNINGS=OFF \\
  -DIMHEX_IGNORE_BAD_COMPILER=ON \\
  -DIMHEX_BUNDLE_DOTNET=OFF \\
  -DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON \\
  .. || true
cmake --build . -j $(nproc) --target unit_tests -- -k 0 || true
ctest --output-on-failure

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
bash /home/init_submodules.sh
cd build
cmake -GNinja \\
  -DCMAKE_BUILD_TYPE=Release \\
  -DCMAKE_C_COMPILER=gcc-12 \\
  -DCMAKE_CXX_COMPILER=g++-12 \\
  -DIMHEX_ENABLE_UNIT_TESTS=ON \\
  -DIMHEX_OFFLINE_BUILD=ON \\
  -DIMHEX_STRICT_WARNINGS=OFF \\
  -DIMHEX_IGNORE_BAD_COMPILER=ON \\
  -DIMHEX_BUNDLE_DOTNET=OFF \\
  -DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON \\
  .. || true
cmake --build . -j $(nproc) --target unit_tests -- -k 0 || true
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


@Instance.register("WerWolv", "imhex_1673_to_580")
class IMHEX_1673_TO_580(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImHexEra2ImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]
        re_skip_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Skipped\s*.*$"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    passed_tests.add(pass_match.group(1).strip())

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1).strip())

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    skipped_tests.add(skip_match.group(1).strip())

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
