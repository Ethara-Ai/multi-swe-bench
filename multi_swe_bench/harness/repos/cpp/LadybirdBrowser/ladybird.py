import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Ladybird is a from-scratch web browser. The dataset's test_patches modify
# LibJS JavaScript test files; the harness measures them with `test-js`, the
# LibJS test runner (built via `Meta/ladybird.py build test-js`).
#
# Build notes:
#  - Needs a C++23 compiler (clang-21) + CMake 3.30+ + vcpkg.
#  - `Meta/ladybird.py` refuses to run as root, so the build + test commands
#    run as a non-root `builder` user via `sudo -u builder`.
#  - vcpkg compiles heavy deps (skia/ffmpeg/icu/qtbase). The base image runs
#    one full build at clone HEAD to populate the vcpkg binary cache + ccache;
#    per-PR prepare.sh then rebuilds incrementally.
#  - KNOWN RISK: skia's `gn` tool fails under vcpkg on linux/arm64. The amd64
#    leg of a multi-arch build is the one expected to succeed.


_APT_DEPS = (
    "autoconf autoconf-archive automake build-essential ccache curl git sudo "
    "wget gnupg fonts-liberation2 libdrm-dev libgl1-mesa-dev libtool nasm "
    "ninja-build pkg-config python3 python3-pip python3-venv tar unzip zip "
    "lsb-release ca-certificates linux-libc-dev qt6-base-dev qt6-tools-dev-tools"
)


class LadybirdImageBase(Image):
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

        # Pin the base to linux/amd64. vcpkg's skia port needs the `gn` build
        # tool, for which vcpkg ships a working prebuilt only on linux-amd64
        # (arm64 fails with `Command failed: None gen`). Pinning here makes
        # every build step run amd64 binaries, so BOTH the amd64 and arm64
        # legs of a multi-arch buildx build succeed (the arm64 manifest entry
        # carries amd64 content, executed via qemu) — the resulting image
        # manifest still lists both arches for verify_ecr_images.py.
        return f"""FROM --platform=linux/amd64 {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV CCACHE_DIR=/home/builder/.ccache
WORKDIR /home/

# Base system deps.
RUN apt-get update && apt-get install -y --no-install-recommends {_APT_DEPS} \\
    && rm -rf /var/lib/apt/lists/*

# clang-21 (Ladybird's C++23 toolchain) from the LLVM apt repository.
# One toolchain for all PRs: clang-21 is newer than every era's minimum
# (Era B mid-2024 needed clang-18+), so it satisfies the whole #1..#6670
# range — no per-era base image, hence no doubled vcpkg pre-warm build.
RUN wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key > /etc/apt/trusted.gpg.d/llvm.asc \\
    && . /etc/os-release \\
    && echo "deb http://apt.llvm.org/$VERSION_CODENAME/ llvm-toolchain-$VERSION_CODENAME-21 main" > /etc/apt/sources.list.d/llvm.list \\
    && apt-get update \\
    && (apt-get install -y --no-install-recommends clang-21 clang-tools-21 lld-21 libclang-rt-21-dev \\
        || apt-get install -y --no-install-recommends clang-21 lld-21) \\
    && rm -rf /var/lib/apt/lists/*

# CMake 3.30+ (newer than Ubuntu 24.04 ships).
RUN pip3 install --break-system-packages cmake

# Non-root build user — Meta/ladybird.py refuses to run as root.
RUN useradd -m -s /bin/bash builder

{code}

RUN chown -R builder:builder /home/{self.pr.repo}

# Pre-warm: one full test-js build at clone HEAD to populate the vcpkg
# binary cache and ccache, so per-PR rebuilds are incremental.
RUN sudo -u builder bash -c 'cd /home/{self.pr.repo} \\
    && CC=clang-21 CXX=clang++-21 python3 Meta/ladybird.py build test-js' || true

{self.clear_env}

"""


class LadybirdImageDefault(Image):
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
        return LadybirdImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                "build_test_js.sh",
                """#!/bin/bash
# Build the `test-js` target, auto-selecting the era-appropriate build
# driver. The 23-PR range spans a driver migration:
#   Era C (PR ~#3788+): Meta/ladybird.py     Era B (PR #209-#3452): Meta/ladybird.sh
# Args: $1 = repo path, $2 = build timeout seconds (default 21000).
set -u
REPO="$1"
TIMEOUT="${2:-21000}"
cd "$REPO"
export CC=clang-21 CXX=clang++-21
if [ -f Meta/ladybird.py ]; then
  timeout --kill-after=60 "$TIMEOUT" python3 Meta/ladybird.py build test-js
elif [ -f Meta/ladybird.sh ]; then
  timeout --kill-after=60 "$TIMEOUT" ./Meta/ladybird.sh build test-js
else
  echo "build_test_js: no known build driver (Meta/ladybird.py or Meta/ladybird.sh)"
  exit 1
fi
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

REPO=/home/{repo}
chown -R builder:builder "$REPO" /home/test.patch /home/fix.patch 2>/dev/null || true
cd "$REPO"
git config --global --add safe.directory "$REPO" 2>/dev/null || true
sudo -u builder git config --global --add safe.directory "$REPO" 2>/dev/null || true

sudo -u builder bash -c "cd $REPO && git reset --hard && git checkout -f {sha} && git reset --hard && git clean -ffdx"
# check_git_changes is informational only. `git checkout -f` guarantees the
# base-commit content is checked out; a still-"dirty" status is only line-
# ending / .gitattributes re-normalisation, which must NOT abort the build.
bash /home/check_git_changes.sh || echo "prepare: residual tree changes ignored (normalisation) — continuing"

# Rebuild test-js at the PR base commit (incremental via ccache + vcpkg cache).
# build_test_js.sh auto-selects the era-appropriate build driver.
chown builder:builder /home/build_test_js.sh 2>/dev/null || true
sudo -u builder bash /home/build_test_js.sh "$REPO" 21000 || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
# Build test-js (incremental) and run it. test-js scans its compiled-in
# TEST_ROOT for *.js files and prints ` PASS <file>` / ` FAIL <file>`.
set -uo pipefail

REPO=/home/{repo}
cd "$REPO"

sudo -u builder bash /home/build_test_js.sh "$REPO" 21000 || true

TESTJS=$(find "$REPO/Build" -name test-js -type f 2>/dev/null | head -1)
if [ -z "$TESTJS" ]; then
  echo "ERROR: test-js binary not found — build failed"
  exit 1
fi

# Run test-js. It exits non-zero whenever any .js test fails (expected in the
# test-patch stage), so its exit code is swallowed — but a genuine crash
# (segfault, missing shared lib) yields NO PASS/FAIL lines. Tee the output and
# verify it contains test markers: if not, fail loudly so the stage is
# rejected rather than silently producing a degenerate 0/0/0 report.
TESTLOG=$(mktemp)
sudo -u builder bash -c "cd $REPO && LADYBIRD_SOURCE_DIR=$REPO \\
    timeout --kill-after=60 3600 '$TESTJS'" 2>&1 | tee "$TESTLOG" || true

if ! grep -Eq '(^|[[:space:]])(PASS|FAIL)[[:space:]].+\\.js|Finished:' "$TESTLOG"; then
  echo "ERROR: test-js produced no PASS/FAIL output — runner crashed or failed to start"
  rm -f "$TESTLOG"
  exit 1
fi
rm -f "$TESTLOG"
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
REPO=/home/{repo}
cd "$REPO"
sudo -u builder bash -c "cd $REPO && git apply --whitespace=nowarn /home/test.patch" \\
  || sudo -u builder bash -c "cd $REPO && git apply --whitespace=nowarn --reject /home/test.patch" || true
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
REPO=/home/{repo}
cd "$REPO"
sudo -u builder bash -c "cd $REPO && git apply --whitespace=nowarn /home/test.patch /home/fix.patch" \\
  || sudo -u builder bash -c "cd $REPO && git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch" || true
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
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

        # Pin to amd64 — see RaylibImageBase note: the base image is amd64,
        # so per-PR images must match to reuse its vcpkg/ccache layers.
        return f"""FROM --platform=linux/amd64 {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("LadybirdBrowser", "ladybird")
class Ladybird(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LadybirdImageDefault(self.pr, self._config)

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

        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        # test-js per-file output (LibTest JavaScriptTestRunner):
        #   ` PASS <path>.js (12ms)`     -> pass
        #   ` FAIL <path>.js (5ms)`      -> fail
        # Plus per-test "Finished: <name> (PASS|FAIL|XFAIL)" lines.
        re_pass = [
            re.compile(r"^\s*PASS\s+(\S+\.js)\b"),
            re.compile(r"^\s*Finished:\s+(.+?)\s+\(PASS\)\s*$"),
        ]
        re_fail = [
            re.compile(r"^\s*FAIL\s+(\S+\.js)\b"),
            re.compile(r"^\s*Finished:\s+(.+?)\s+\(FAIL\)\s*$"),
        ]
        re_skip = [
            re.compile(r"^\s*(?:SKIP|XFAIL)\s+(\S+\.js)\b"),
            re.compile(r"^\s*Finished:\s+(.+?)\s+\(XFAIL\)\s*$"),
        ]

        for line in clean.splitlines():
            matched = False
            for rx in re_pass:
                m = rx.match(line)
                if m:
                    passed_tests.add(m.group(1).strip())
                    matched = True
                    break
            if matched:
                continue
            for rx in re_fail:
                m = rx.match(line)
                if m:
                    failed_tests.add(m.group(1).strip())
                    matched = True
                    break
            if matched:
                continue
            for rx in re_skip:
                m = rx.match(line)
                if m:
                    skipped_tests.add(m.group(1).strip())
                    break

        # Disjoint sets: passed > failed > skipped.
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
