from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Binary diffs cause 'cannot apply binary patch without full index line'
    errors with git apply. These are not needed for compilation or testing.
    """
    if not patch_content:
        return patch_content

    lines = patch_content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('diff --git'):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith('diff --git'):
                if lines[i].startswith('GIT binary patch') or lines[i].startswith('Binary files'):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)


def _select_toolchain(pr: PullRequest) -> tuple[str, str, str]:
    """Map a PR to its (base_image, tag_suffix, compiler) toolchain triple.

    Kept as a free function so the toolchain base, the shared repo image, and
    the per-PR image all agree on the same era without duplicating the rules.
    """
    # PR #1730: fix patch requires C++20 (coroutines), gcc:10 can't compile it
    _clang_overrides = {1730}
    # PR #2385: fix patch requires C++23 (#include <print>), clang-14 can't handle it
    # PR #2410: base has linker bug (missing kj-async link), fix adds it + upgrades to C++23
    #           needs gcc:latest for C++23 support; linker bug in base is expected
    _gcc_latest_overrides = {2385, 2410}

    if pr.number in _clang_overrides:
        return ("ubuntu:22.04", "clang-14", "clang")
    if pr.number in _gcc_latest_overrides:
        return ("gcc:latest", "latest", "gcc")
    if pr.number <= 1730:
        return ("gcc:10", "cpp-10", "gcc")
    elif pr.number <= 2409:
        return ("ubuntu:22.04", "clang-14", "clang")
    return ("gcc:latest", "latest", "gcc")


class CapnprotoToolchainBase(Image):
    """Level 1: toolchain-only base image (shared across all PRs of an era).

    IMPORTANT: this image must NOT clone the repository. image.py's
    DockerfileEnhancer force-injects a "checkout ${BASE_COMMIT} + strip all
    history + remove origin" hardening block into ANY image whose dependency()
    is a string (an external base image) and that performs a `git clone`.
    Because this base image is shared by every PR of an era, pinning it to a
    single BASE_COMMIT and gc-pruning the rest of history would make
    `git checkout <base.sha>` fail for every other PR sharing the era. So the
    clone lives in CapnprotoImageRepo (whose dependency() is an Image, which the
    enhancer leaves untouched), preserving full history. This image only
    provides the C/C++ build toolchain.
    """

    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        base_image: str,
        tag_suffix: str,
        compiler: str = "gcc",
    ):
        self._pr = pr
        self._config = config
        self._base_image = base_image
        self._tag_suffix = tag_suffix
        self._compiler = compiler

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return self._base_image

    def image_tag(self) -> str:
        return f"base-{self._tag_suffix}"

    def workdir(self) -> str:
        return f"base-{self._tag_suffix}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Clang-based images need clang installed explicitly
        if self._compiler == "clang":
            extra_packages = "clang \\\n    git \\\n    "
            env_prefix = "ENV DEBIAN_FRONTEND=noninteractive\n"
        else:
            extra_packages = ""
            env_prefix = ""

        # No `git clone` here on purpose — see the class docstring. The string
        # dependency means DockerfileEnhancer runs over this Dockerfile, but with
        # no clone/COPY — DockerfileEnhancer injects label infra (no hardening).
        return f"""FROM {image_name}

{env_prefix}
WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    {extra_packages}cmake \\
    autoconf \\
    automake \\
    libtool \\
    pkg-config \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

"""


class CapnprotoImageRepo(Image):
    """Level 2: shared full-clone image (one per era, built once).

    Depends on CapnprotoToolchainBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim — no BASE_COMMIT
    pinning, no history stripping, no origin removal. The repository is cloned
    once with its complete history at master HEAD, which keeps every PR's
    base.sha reachable for the per-PR checkout done in prepare.sh.
    """

    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        base_image: str,
        tag_suffix: str,
        compiler: str = "gcc",
    ):
        self._pr = pr
        self._config = config
        self._base_image = base_image
        self._tag_suffix = tag_suffix
        self._compiler = compiler

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return CapnprotoToolchainBase(
            self.pr,
            self._config,
            self._base_image,
            self._tag_suffix,
            self._compiler,
        )

    def image_tag(self) -> str:
        return f"repo-{self._tag_suffix}"

    def workdir(self) -> str:
        return f"repo-{self._tag_suffix}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_full_name()

        # Full-history clone, left at master HEAD. The per-PR prepare.sh checks
        # out the exact base.sha; harden.sh then strips history per-PR.
        clone = (
            f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git "
            f"/home/{self.pr.repo}"
        )

        return f"""FROM {name}

WORKDIR /home/

{clone}

"""


class CapnprotoImageDefault(Image):
    """Level 3: per-PR image. Checks out base.sha and strips history in-image."""

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
        base_image, tag_suffix, compiler = _select_toolchain(self.pr)
        return CapnprotoImageRepo(
            self.pr, self._config, base_image, tag_suffix, compiler
        )

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _cmake_flags(self) -> str:
        _, _, compiler = _select_toolchain(self.pr)
        if compiler == "clang":
            return "-DBUILD_TESTING=ON -DCMAKE_CXX_COMPILER=clang++"
        if self.pr.number <= 1730:
            return '-DBUILD_TESTING=ON -DCMAKE_CXX_FLAGS="-Wno-narrowing"'
        return "-DBUILD_TESTING=ON"

    def files(self) -> list[File]:
        filtered_fix_patch = _filter_binary_patches(self.pr.fix_patch)
        filtered_test_patch = _filter_binary_patches(self.pr.test_patch)
        cmake_flags = self._cmake_flags()

        return [
            File(
                ".",
                "fix.patch",
                f"{filtered_fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{filtered_test_patch}",
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

cd c++
mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}/c++/build
cmake .. {cmake_flags}
make -j$(nproc)
cd src && ctest --output-on-failure
""".format(pr=self.pr, cmake_flags=cmake_flags),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
cd c++/build
cmake .. {cmake_flags} || true
make -j$(nproc) -k 2>&1 || true
cd src && ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr, cmake_flags=cmake_flags),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
cd c++/build
cmake .. {cmake_flags} || true
make -j$(nproc) -k 2>&1 || true
cd src && ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr, cmake_flags=cmake_flags),
            ),
            File(
                ".",
                "harden.sh",
                # NOTE: raw content — NOT .format()ed — so ${VAR} / ^{commit}
                # braces stay literal. base.sha arrives as $1 from the Dockerfile.
                #
                # Anti-reward-hacking hardening applied to the per-PR image AFTER
                # prepare.sh. image.py's DockerfileEnhancer normally injects this,
                # but only for string-dependency images that clone — doing that
                # here would re-pin and history-strip the SHARED toolchain/repo
                # image and break every other PR's base.sha checkout. So the clone
                # lives in the shared repo image (full history, needed so every
                # era's base.sha stay reachable) and the per-PR image strips
                # history here instead. prepare.sh leaves HEAD detached exactly at
                # base.sha, so base.sha stays reachable (test-run/fix-run still
                # apply patches against it) while the real fix — every commit after
                # base.sha on master, plus the origin remote and all branch/tag
                # refs — is removed.
                """#!/bin/bash
set -e
cd /home/capnproto

BASE_SHA="$1"
# Record the real-fix tip (master) BEFORE cutting history, to assert removal.
FUTURE_SHA="$(git rev-parse origin/master 2>/dev/null || true)"

git checkout --detach HEAD
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
  | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all || true
git reflog expire --expire-unreachable=now --all || true
git gc --prune=now --aggressive || true
git repack -a -d -l --quiet || true
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false || true
git config --local remote.pushDefault "" || true

# --- Assertions: fail the build if the image is still cheatable -------------
test -z "$(git remote)"                                                       # no origin to fetch
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"  # no branch/tag/remote refs
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"          # no history beyond HEAD
git cat-file -e "${BASE_SHA}^{commit}"                                         # base.sha must remain reachable
if [ -n "$FUTURE_SHA" ] && [ "$FUTURE_SHA" != "$BASE_SHA" ]; then
  if git cat-file -e "${FUTURE_SHA}^{commit}" 2>/dev/null; then
    echo "HARDENING FAILED: future commit ${FUTURE_SHA} still present" >&2
    exit 1
  fi
fi
echo "HARDENING OK: origin & refs removed, future history pruned, base.sha reachable"
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
        # Strip origin/refs/future-history AFTER prepare.sh so the per-PR eval
        # image cannot be reward-hacked via `git log`/`git show`/`git fetch`.
        harden_commands = f"RUN bash /home/harden.sh {self.pr.base.sha}"

        return f"""FROM {name}:{tag}

{copy_commands}

{prepare_commands}

{harden_commands}

"""


@Instance.register("capnproto", "capnproto")
class Capnproto(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CapnprotoImageDefault(self.pr, self._config)

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

        # CTest executable-level patterns (e.g. "1/5 Test #1: kj-tests-run ... Passed")
        re_ctest_pass = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"
        )
        re_ctest_fail = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]

        # Sub-test-level patterns from --output-on-failure
        # e.g. "[ PASS ] async-test.c++:31: legacy test: Async/GetFunctorStartAddress"
        # e.g. "[ FAIL ] filesystem-disk-test.c++:825: DiskFile holes"
        re_subtest_pass = re.compile(
            r"^\[\s*PASS\s*\]\s+(.+)$"
        )
        re_subtest_fail = re.compile(
            r"^\[\s*FAIL\s*\]\s+(.+)$"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            # Check CTest-level pass
            m = re_ctest_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            # Check CTest-level fail
            for pat in re_ctest_fail:
                m = pat.match(line)
                if m:
                    failed_tests.add(m.group(1).strip())
                    break
            else:
                # Check sub-test-level pass/fail
                m = re_subtest_pass.match(line)
                if m:
                    passed_tests.add(m.group(1).strip())
                    continue

                m = re_subtest_fail.match(line)
                if m:
                    failed_tests.add(m.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
