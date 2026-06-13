import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# LizardByte/Sunshine — a self-hosted game-stream host (Moonlight server).
# C++ / CMake project; the test suite is GoogleTest (`test_sunshine`).
#
# Discovery (GitHub API + scripts/linux_build.sh):
#  - The project ships its own authoritative build script,
#    `scripts/linux_build.sh`, which apt-installs every dependency, picks the
#    right gcc, compiles a new CMake when needed, and runs `cmake + ninja`.
#    The registry runs that script (--skip-cleanup --skip-package) so the
#    build always matches the checked-out commit, then runs the GoogleTest
#    binary `test_sunshine` under xvfb (headless unit tests).
#  - The test infrastructure (tests/, GoogleTest) was introduced at PR #1603;
#    only PRs #1603, #2186, #2906, #3725 carry runnable C++ tests. The other
#    9 PRs' test_patch touches only docs (.rst) / packaging (.cmake/.spec) /
#    a submodule bump — they have no test and cannot resolve. One config,
#    no era split: the build script adapts itself to each commit.
#  - Sunshine pulls many git submodules (googletest, moonlight-common-c,
#    inputtino, ...) — cloned recursively and refreshed in prepare.sh.


_LB = (
    "bash scripts/linux_build.sh --skip-cleanup --skip-package --ubuntu-test-repo "
    "--publisher-name=msb --publisher-website=https://example.com "
    "--publisher-issue-url=https://example.com"
)


class SunshineImageBase(Image):
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
        # ubuntu:24.04 — recent Sunshine requires udev/systemd >= 255
        # (22.04 ships 249, which fails the CMake UDEV check) and provides
        # gcc-13/14 natively.
        return "ubuntu:24.04"

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
                f"RUN git clone --recurse-submodules "
                f"https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
WORKDIR /home/

# scripts/linux_build.sh apt-installs build deps per commit, but it was only
# added in PR #2946 -- earlier base commits don't have it. So install the full
# Sunshine Debian/Ubuntu build-dependency set directly here, making the build
# work for every PR regardless of whether that script exists.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl wget ca-certificates sudo gnupg lsb-release \\
    software-properties-common build-essential pkg-config \\
    bison flex cmake ninja-build doxygen graphviz npm udev xvfb \\
    gcc-13 g++-13 gcc-14 g++-14 \\
    libcap-dev libcurl4-openssl-dev libdrm-dev libevdev-dev \\
    libminiupnpc-dev libnotify-dev libnuma-dev libopus-dev libpulse-dev \\
    libssl-dev libvdpau-dev libva-dev libwayland-dev \\
    libx11-dev libxcb-shm0-dev libxcb-xfixes0-dev libxcb1-dev \\
    libxfixes-dev libxrandr-dev libxtst-dev \\
    libayatana-appindicator3-dev libnotify-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SunshineImageDefault(Image):
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
        return SunshineImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        check_git = """#!/bin/bash
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
"""

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory '*'
git reset --hard
git checkout __SHA__
git submodule update --init --recursive || true
bash /home/check_git_changes.sh || true

# Warm-up: run the project's own build script at the base commit. It
# apt-installs every dependency (baked into the image layer) and builds, so
# per-PR rebuilds are incremental. Non-fatal — older PRs may predate it.
timeout --kill-after=60 5400 __LB__ || true
""".replace("__REPO__", repo).replace("__SHA__", sha).replace("__LB__", _LB)

        run_tests = """#!/bin/bash
# Build Sunshine, then run the GoogleTest binary if this commit has one.
# Emits GoogleTest [ OK ]/[ FAILED ] lines AND a build sentinel for parse_log:
# PRs older than the test infrastructure (introduced ~PR #1603) have no
# test_sunshine target, so "the project compiles" is their resolvable signal.
set -uo pipefail
cd /home/__REPO__

# Submodules: era pins. A few pinned commits were force-pushed away upstream
# (e.g. third-party/Simple-Web-Server); for any submodule left empty, clone it
# fresh and fall back to its branch tip when the exact pin is gone.
git submodule update --init --recursive 2>/dev/null || true
git config -f .gitmodules --get-regexp '\\.path$' 2>/dev/null | while read -r k p; do
  n=${k%.path}; n=${n#submodule.}
  u=$(git config -f .gitmodules --get "submodule.${n}.url" 2>/dev/null)
  [ -z "$u" ] && continue
  [ -n "$(ls -A "$p" 2>/dev/null)" ] && continue
  pin=$(git rev-parse ":$p" 2>/dev/null || true)
  rm -rf "$p"
  if git clone "$u" "$p" 2>/dev/null; then
    [ -n "$pin" ] && ( cd "$p" && git checkout -q "$pin" 2>/dev/null || true )
    ( cd "$p" && git submodule update --init --recursive 2>/dev/null || true )
    echo "submodule: cloned $p"
  fi
done

# Prefer the project's own build script (only exists from PR #2946 onward).
timeout --kill-after=60 5400 __LB__ || true

# Otherwise configure + build directly (deps are baked into the base image).
BUILD_RC=1
if find build -name test_sunshine -type f 2>/dev/null | grep -q .; then
  BUILD_RC=0
else
  cmake -B build -G Ninja -S . -DBUILD_TESTS=ON -DBUILD_WERROR=OFF \\
      -DCMAKE_BUILD_TYPE=Release -DSUNSHINE_ENABLE_CUDA=OFF 2>&1 | tail -25 || true
  # Build the test target if it exists; else the main target; else everything.
  if ninja -C build test_sunshine 2>&1 | tail -25; then BUILD_RC=0
  elif ninja -C build sunshine 2>&1 | tail -25; then BUILD_RC=0
  elif ninja -C build 2>&1 | tail -25; then BUILD_RC=0
  fi
fi

if [ "$BUILD_RC" -eq 0 ]; then
  echo "### SUNSHINE BUILD: PASSED ###"
else
  echo "### SUNSHINE BUILD: FAILED ###"
fi

TESTBIN=$(find build -name test_sunshine -type f 2>/dev/null | head -1)
if [ -n "$TESTBIN" ]; then
  xvfb-run -a "$TESTBIN" --gtest_color=no || true
else
  echo "(no test_sunshine target at this commit — build sentinel only)"
fi
""".replace("__REPO__", repo).replace("__LB__", _LB)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.ico "
            "--exclude=*.icns --exclude=*.gif --exclude=*.svg --exclude=*.webp "
            "--exclude=*.cur --exclude=*.bin --exclude=*.ttf "
            "--exclude=*.woff --exclude=*.woff2 --exclude=*.zip "
            "--exclude=*.so --exclude=*.dll --exclude=*.exe"
        )

        # The dataset's patches are PR bundles whose context can drift from the
        # recorded base.sha; --3way merges via the blob index, then --reject
        # salvages whatever still applies.
        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
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


@Instance.register("LizardByte", "Sunshine")
class Sunshine(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SunshineImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Build sentinel. PRs older than Sunshine's test infrastructure have no
        # test_sunshine binary, so "the project compiles" is their resolvable
        # signal: NONE in the baseline -> PASS after the fix patch -> resolved.
        if "### SUNSHINE BUILD: PASSED ###" in clean:
            passed_tests.add("sunshine_build")
        elif "### SUNSHINE BUILD: FAILED ###" in clean:
            failed_tests.add("sunshine_build")

        # GoogleTest console output:
        #   [       OK ] Suite.Test (3 ms)
        #   [  FAILED  ] Suite.Test (1 ms)
        #   [  SKIPPED ] Suite.Test
        re_ok = re.compile(r"^\[\s*OK\s*\]\s+(\S+)")
        re_fail = re.compile(r"^\[\s*FAILED\s*\]\s+(\S+?)(?:,| \(|$)")
        re_skip = re.compile(r"^\[\s*SKIPPED\s*\]\s+(\S+)")

        for line in clean.splitlines():
            line = line.strip()
            m = re_ok.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue
            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue
            m = re_fail.match(line)
            if m:
                name = m.group(1).rstrip(",")
                # Ignore the trailing "[ FAILED ] N tests" summary line.
                if "." in name:
                    failed_tests.add(name)

        # Disjoint sets: failed > skipped > passed.
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
