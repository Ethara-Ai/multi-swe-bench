import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
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
#    inputtino, ...) — initialized at the PR image's base-commit checkout and
#    refreshed in run_tests.sh.
#
# Two-level image layout
# ----------------------
#   level 1  <prefix>/lizardbyte_m_sunshine:base   — ONE image, shared by every
#            PR. Ubuntu + the full apt build-dependency set. Deliberately holds
#            NO source: it is PR-independent, so it builds once and every
#            pr-<n> image layers on top of it.
#   level 2  <prefix>/lizardbyte_m_sunshine:pr-<n> — per PR. Clones the repo,
#            checks out that PR's base commit, syncs submodules, then runs the
#            hardening block.
#
# Why the fetch + hardening live at level 2 rather than being inherited from
# Image.dockerfile(): that contract puts the clone, the ${BASE_COMMIT} checkout
# and the hardening block in the *base* image. Hardening deletes every ref and
# prunes every object unreachable from ${BASE_COMMIT}, which pins the image to
# exactly one commit — irreconcilable with a base shared across PRs that each
# have a different base.sha. So the base stays source-free and each PR image
# does its own fetch + checkout + harden. The hardening text itself is still
# the single canonical copy from image.py (Image._HARDENING_BLOCK) — referenced,
# not forked, so it cannot drift from the harness.


_LB = (
    "bash scripts/linux_build.sh --skip-cleanup --skip-package --ubuntu-test-repo "
    "--publisher-name=msb --publisher-website=https://example.com "
    "--publisher-issue-url=https://example.com"
)

# Mirrors Image.dockerfile()'s own default package set. The base image builds
# its apt layer by hand (see SunshineImageBase.dockerfile), so it has to supply
# these itself rather than getting them from the inherited skeleton.
_BASE_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
]


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

    def extra_packages(self) -> list[str]:
        # scripts/linux_build.sh apt-installs build deps per commit, but it was
        # only added in PR #2946 -- earlier base commits don't have it. So
        # install the full Sunshine Debian/Ubuntu build-dependency set here,
        # making the build work for every PR regardless of whether that script
        # exists. Image.dockerfile() already provides ca-certificates, curl,
        # build-essential, git, gnupg, make, python3, sudo and wget.
        return [
            "lsb-release",
            "software-properties-common",
            "pkg-config",
            "bison",
            "flex",
            "cmake",
            "ninja-build",
            "doxygen",
            "graphviz",
            "npm",
            "udev",
            "xvfb",
            "gcc-13",
            "g++-13",
            "gcc-14",
            "g++-14",
            "libboost-all-dev",
            "libavcodec-dev",
            "libavdevice-dev",
            "libavfilter-dev",
            "libavformat-dev",
            "libavutil-dev",
            "libswscale-dev",
            "libcap-dev",
            "libcurl4-openssl-dev",
            "libdrm-dev",
            "libevdev-dev",
            # gbm.h, included by src/platform/linux/wayland.cpp from ~#2186 on.
            # libdrm-dev does not provide it; without this the wayland TU fails
            # with "fatal error: gbm.h: No such file or directory".
            "libgbm-dev",
            "libminiupnpc-dev",
            "libnotify-dev",
            "libnuma-dev",
            "libopus-dev",
            "libpulse-dev",
            "libssl-dev",
            "libvdpau-dev",
            "libva-dev",
            "libwayland-dev",
            "libx11-dev",
            "libxcb-shm0-dev",
            "libxcb-xfixes0-dev",
            "libxcb1-dev",
            "libxfixes-dev",
            "libxrandr-dev",
            "libxtst-dev",
            "libayatana-appindicator3-dev",
        ]

    def dockerfile(self) -> str:
        # Source-free by design: this image is shared by every PR, so it must
        # not contain a commit. It also must not contain the strings
        # "git clone" / "git fetch" / "git remote add" / "COPY <repo> ...",
        # or DockerfileEnhancer would standardize the fetch and inject the
        # hardening block here (image.py `_standardize_repo_fetch` /
        # `_inject_final_sanitize`) — re-pinning the shared base to a single
        # ${BASE_COMMIT}. The PR image does the fetch and the hardening.
        base_img = self.dependency()
        assert isinstance(base_img, str)

        packages_str = " \\\n    ".join(_BASE_PACKAGES + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        sections = [f"FROM {base_img}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(apt_command)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


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

    def dependency(self) -> Union[str, "Image"]:
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

# The image already cloned, checked out __SHA__ and ran the hardening block,
# so the tree sits at the base commit with no refs or remotes left. Assert
# that rather than re-checking-out: after hardening there is nothing to
# check out *to*, and a silent drift here would poison every test result.
test "$(git rev-parse HEAD)" = "$(git rev-parse __SHA__)"
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

# Submodules: put each at the commit the PR *intends*. git apply patches the
# working tree, not the index, so any gitlink a patch ADDs or BUMPs never reaches
# the index -- `git submodule update` would then use the stale base pin (a BUMP,
# e.g. #1090's moonlight-common-c) or nothing at all (an ADD, e.g. #1090's
# third-party/build-deps). We therefore take each submodule's target pin from the
# patch's post-image (`+Subproject commit` under `+++ b/<path>`), fall back to the
# index gitlink, and force that commit -- cloning when the submodule is absent,
# fetching the exact sha when a shallow clone lacks it. Force-pushed pins that no
# longer exist upstream fall through to whatever is already checked out.
git submodule update --init --recursive 2>/dev/null || true
git config -f .gitmodules --get-regexp '\\.path$' 2>/dev/null | while read -r k p; do
  n=${k%.path}; n=${n#submodule.}
  u=$(git config -f .gitmodules --get "submodule.${n}.url" 2>/dev/null)
  [ -z "$u" ] && continue
  # Target pin: post-image of the patches APPLIED IN THIS STAGE only, else the
  # index gitlink (--verify -q so an unresolved path yields "" not an echo).
  # MSB_PATCHES is set by the stage wrapper (empty for the unpatched run stage);
  # reading test/fix.patch unconditionally would check out post-fix submodules
  # during the base run and destroy the fail->pass signal.
  pin=""
  for pf in ${MSB_PATCHES:-}; do
    [ -f "$pf" ] || continue
    s=$(awk -v hdr="+++ b/$p" '$0==hdr{f=1;next} /^\\+\\+\\+ /{f=0} f&&/^\\+Subproject commit /{v=$3} END{if(v)print v}' "$pf")
    [ -n "$s" ] && pin="$s"
  done
  [ -z "$pin" ] && pin=$(git rev-parse --verify -q ":$p" 2>/dev/null || true)
  [ -z "$pin" ] && continue
  cur=$(git -C "$p" rev-parse --verify -q HEAD 2>/dev/null || true)
  [ "$cur" = "$pin" ] && continue
  if [ -z "$(ls -A "$p" 2>/dev/null)" ]; then
    rm -rf "$p"; git clone "$u" "$p" 2>/dev/null || continue
  fi
  # Fetch from the .gitmodules URL, not "origin": submodules materialised by
  # `git submodule update` often have no origin remote configured here.
  ( cd "$p" \\
    && (git checkout -q "$pin" 2>/dev/null \\
        || (git fetch --depth 1 "$u" "$pin" 2>/dev/null && git checkout -q "$pin" 2>/dev/null) \\
        || (git fetch "$u" 2>/dev/null && git checkout -q "$pin" 2>/dev/null) \\
        || true) \\
    && git submodule update --init --recursive 2>/dev/null || true )
  echo "submodule: $p @ $(git -C "$p" rev-parse --verify -q HEAD 2>/dev/null || echo "$pin")"
done

# Prefer the project's own build script (only exists from PR #2946 onward).
timeout --kill-after=60 5400 __LB__ || true

# Otherwise configure + build directly (deps are baked into the base image).
BUILD_RC=1
if find build -name test_sunshine -type f 2>/dev/null | grep -q .; then
  BUILD_RC=0
else
  # Force-include a few standard headers. Pre-gcc-11 Sunshine (e.g. #321, v0.14)
  # relied on libstdc++ pulling <string>/<cctype>/<algorithm>/<cstdint> in
  # transitively; gcc-13 on ubuntu:24.04 no longer does, so src/utility.h fails
  # with "std::string is incomplete" / "isdigit not declared". -include injects
  # them into every C++ TU without touching the source under test (CXX-only, so
  # the C submodules are unaffected); redundant on newer commits, harmless there.
  # BUILD_DOCS=OFF: Sunshine from ~#2186 onward requires Doxygen >= 1.10, but
  # ubuntu:24.04 ships 1.9.8 (newest in apt), so `find_package(Doxygen 1.10)`
  # aborts configure -> no build.ninja -> the whole build fails before a single
  # object compiles. Docs are irrelevant to the build/test signal, and the option
  # gates only the docs target (cmake/prep/options.cmake), so turning it off
  # restores the build without touching the source under test.
  cmake -B build -G Ninja -S . -DBUILD_TESTS=ON -DBUILD_WERROR=OFF \\
      -DCMAKE_BUILD_TYPE=Release -DSUNSHINE_ENABLE_CUDA=OFF -DBUILD_DOCS=OFF \\
      -DCMAKE_CXX_FLAGS="-include cstdint -include string -include cctype -include algorithm" \\
      2>&1 | tail -25 || true
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
# No patches applied in this stage: submodules must stay at their base pins.
export MSB_PATCHES=""
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
export MSB_PATCHES="/home/test.patch"
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
export MSB_PATCHES="/home/test.patch /home/fix.patch"
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
        dep = self.dependency()
        assert isinstance(dep, Image)

        # This image's dependency is an Image, so DockerfileEnhancer.enhance()
        # returns our text verbatim and build_dataset passes no REPO_URL /
        # BASE_COMMIT build args (both are gated on a str dependency). We
        # therefore declare those ARGs ourselves, defaulted to this PR's own
        # values, and keep the same names the hardening block expects.
        # Validate before interpolating into RUN/WORKDIR paths, exactly as
        # image.py does, so a name carrying shell metacharacters cannot inject
        # build commands.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        sha = _safe_path_component(self.pr.base.sha, "base commit")

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        sections = [
            f"FROM {dep.image_name()}:{dep.image_tag()}",
            f'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
            f'ARG BASE_COMMIT="{sha}"',
        ]
        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

        # Submodules are era-pinned: init them *after* the base-commit checkout
        # so they match this PR's pins rather than the default branch's.
        # Non-fatal — a few pinned commits were force-pushed away upstream, and
        # run_tests.sh re-clones whatever is left empty.
        sections.append("RUN git submodule update --init --recursive || true")

        # The canonical hardening block from image.py, referenced rather than
        # copied so it stays in lockstep with the harness.
        sections.append(Image._HARDENING_BLOCK.rstrip("\n"))

        sections.append(copy_commands.rstrip("\n"))
        sections.append("RUN bash /home/prepare.sh")

        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


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
