import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RustdeskImageBase(Image):
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
        return "rust:1.75"

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

RUN apt-get update && apt-get install -y \
    clang \
    cmake \
    g++ \
    pkg-config \
    nasm \
    curl \
    zip \
    unzip \
    tar \
    libpam0g-dev \
    libasound2-dev \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgtk-3-dev \
    libpulse-dev \
    libva-dev \
    libvdpau-dev \
    libxcb-randr0-dev \
    libxcb-shape0-dev \
    libxcb-xfixes0-dev \
    libxdo-dev \
    libxfixes-dev \
    libunwind-dev \
    libopus-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/microsoft/vcpkg /opt/vcpkg \
    && /opt/vcpkg/bootstrap-vcpkg.sh \
    && /opt/vcpkg/vcpkg install libvpx libyuv opus aom

ENV VCPKG_ROOT=/opt/vcpkg

{code}

RUN cd /home/{self.pr.repo} && git submodule update --init --recursive || true

{self.clear_env}

"""


class RustdeskImageDefault(Image):
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
        return RustdeskImageBase(self.pr, self.config)

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

git submodule update --init --recursive || true

# Pre-fetch orphaned git dependency commits — loop until all resolved
for _attempt in 1 2 3 4 5; do
  if cargo fetch 2>&1; then
    break
  fi
  echo "cargo fetch attempt $_attempt failed, fixing orphaned commits..."
  ( set +e
    for line in $(grep '^source = "git+' Cargo.lock | sort -u | sed 's/source = "git+//;s/"//'); do
      _url=$(echo "$line" | sed 's/#.*//;s/?[^#]*//')
      _commit=$(echo "$line" | sed 's/.*#//')
      _name=$(echo "$_url" | sed 's|.*/||;s/\\.git$//' | tr 'A-Z' 'a-z')
      _dir=$(ls -d /usr/local/cargo/git/db/${{_name}}-* 2>/dev/null | head -1)
      if [ -n "$_dir" ] && ! git -C "$_dir" cat-file -t "$_commit" >/dev/null 2>&1; then
        git -C "$_dir" fetch "$_url" "$_commit" 2>&1 || true
      fi
    done
  )
done

cargo test --workspace --no-fail-fast 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
cargo test --workspace --no-fail-fast

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Fix: Create vcpkg x64-linux symlink for old scrap build.rs that hardcodes x64-linux
if [ -d /opt/vcpkg/installed/arm64-linux ] && [ ! -d /opt/vcpkg/installed/x64-linux ]; then
  ln -s /opt/vcpkg/installed/arm64-linux /opt/vcpkg/installed/x64-linux
fi

# Apply test patch: --reject allows partial apply, no --exclude='*.ico' so binary resources apply
git apply --whitespace=nowarn --reject --exclude='*.png' --exclude='*.svg' --exclude='*.gif' --exclude='*.ttf' --exclude='*.wasm' --exclude='*.icns' --exclude='*.rc' /home/test.patch 2>&1 || true

# Update submodules after patch
git submodule update --init --recursive 2>&1 || true

# Fetch orphaned git dependency commits — loop until all resolved
for _attempt in 1 2 3 4 5; do
  if cargo fetch 2>&1; then
    break
  fi
  echo "cargo fetch attempt $_attempt failed, fixing orphaned commits..."
  ( set +e
    for line in $(grep '^source = "git+' Cargo.lock | sort -u | sed 's/source = "git+//;s/"//'); do
      _url=$(echo "$line" | sed 's/#.*//;s/?[^#]*//')
      _commit=$(echo "$line" | sed 's/.*#//')
      _name=$(echo "$_url" | sed 's|.*/||;s/\\.git$//' | tr 'A-Z' 'a-z')
      _dir=$(ls -d /usr/local/cargo/git/db/${{_name}}-* 2>/dev/null | head -1)
      if [ -n "$_dir" ] && ! git -C "$_dir" cat-file -t "$_commit" >/dev/null 2>&1; then
        git -C "$_dir" fetch "$_url" "$_commit" 2>&1 || true
      fi
    done
  )
done

cargo test --workspace --no-fail-fast

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Fix: Create vcpkg x64-linux symlink for old scrap build.rs that hardcodes x64-linux
if [ -d /opt/vcpkg/installed/arm64-linux ] && [ ! -d /opt/vcpkg/installed/x64-linux ]; then
  ln -s /opt/vcpkg/installed/arm64-linux /opt/vcpkg/installed/x64-linux
fi

# Apply patches: --reject allows partial apply, no --exclude='*.ico' so binary resources apply
git apply --whitespace=nowarn --reject --exclude='*.png' --exclude='*.svg' --exclude='*.gif' --exclude='*.ttf' --exclude='*.wasm' --exclude='*.icns' --exclude='*.rc' /home/test.patch /home/fix.patch 2>&1 || true

# Fix binary .ico files that git apply --reject can't handle
if [ -f src/tray-icon.ico ] && [ ! -f res/tray-icon.ico ]; then
  mkdir -p res && cp src/tray-icon.ico res/tray-icon.ico
fi

# Update submodules after patch (fix_patch may change submodule commits)
git submodule update --init --recursive 2>&1 || true

# Fetch orphaned git dependency commits — loop until all resolved
for _attempt in 1 2 3 4 5; do
  if cargo fetch 2>&1; then
    break
  fi
  echo "cargo fetch attempt $_attempt failed, fixing orphaned commits..."
  ( set +e
    for line in $(grep '^source = "git+' Cargo.lock | sort -u | sed 's/source = "git+//;s/"//'); do
      _url=$(echo "$line" | sed 's/#.*//;s/?[^#]*//')
      _commit=$(echo "$line" | sed 's/.*#//')
      _name=$(echo "$_url" | sed 's|.*/||;s/\\.git$//' | tr 'A-Z' 'a-z')
      _dir=$(ls -d /usr/local/cargo/git/db/${{_name}}-* 2>/dev/null | head -1)
      if [ -n "$_dir" ] && ! git -C "$_dir" cat-file -t "$_commit" >/dev/null 2>&1; then
        git -C "$_dir" fetch "$_url" "$_commit" 2>&1 || true
      fi
    done
  )
done

cargo test --workspace --no-fail-fast

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


@Instance.register("rustdesk", "rustdesk")
class Rustdesk(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RustdeskImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"test (\S+) \.\.\. ok")]
        re_fail_tests = [re.compile(r"test (\S+) \.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) \.\.\. ignored")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        # Deduplicate: ensure no test appears in multiple categories
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
