from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch_text: str) -> str:
    if not patch_text:
        return patch_text

    header = re.compile(r"(?m)^diff --git a/(.+?) b/(.+?)$")
    marks = [m.start() for m in header.finditer(patch_text)]
    if not marks:
        return patch_text

    marks.append(len(patch_text))
    kept = [patch_text[: marks[0]]]
    for index in range(len(marks) - 1):
        section = patch_text[marks[index] : marks[index + 1]]
        if "GIT binary patch" in section:
            continue
        if re.search(r"(?m)^Binary files .* differ$", section):
            continue
        if re.search(r"(?m)^diff --git a/(?:build|dist)/", section):
            continue
        kept.append(section)
    return "".join(kept)


class Openj924450To24450ImageBase(Image):
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
        return "base-24450-to-24450"

    def workdir(self) -> str:
        return "base-24450-to-24450"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl wget unzip zip \\
    autoconf make gcc g++ libx11-dev libxext-dev libxrender-dev libxrandr-dev \\
    libxtst-dev libxt-dev libcups2-dev libasound2-dev libfreetype6-dev \\
    libelf-dev libdwarf-dev libnuma-dev libfontconfig1-dev libssl-dev \\
    pkg-config file ant ant-contrib \\
    openjdk-21-jdk python3 python3-pip nasm \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    case "$(dpkg --print-architecture)" in \\
      amd64) CMAKE_ARCH=x86_64 ;; \\
      arm64) CMAKE_ARCH=aarch64 ;; \\
      *) CMAKE_ARCH="$(uname -m)" ;; \\
    esac; \\
    curl -sSL -o /tmp/cmake.tar.gz \\
      "https://github.com/Kitware/CMake/releases/download/v3.28.3/cmake-3.28.3-linux-${{CMAKE_ARCH}}.tar.gz"; \\
    tar -xzf /tmp/cmake.tar.gz -C /opt; \\
    rm /tmp/cmake.tar.gz; \\
    ln -sf "/opt/cmake-3.28.3-linux-${{CMAKE_ARCH}}/bin/cmake" /usr/local/bin/cmake

RUN set -eux; \\
    case "$(dpkg --print-architecture)" in \\
      arm64) BOOT_ARCH=aarch64 ;; \\
      amd64) BOOT_ARCH=x64 ;; \\
      *) echo "unsupported arch" >&2; exit 1 ;; \\
    esac; \\
    BOOT_URL="$(curl -sSL --retry 8 --retry-all-errors --connect-timeout 30 \\
        "https://api.adoptium.net/v3/assets/latest/27/hotspot?architecture=${{BOOT_ARCH}}&image_type=jdk&os=linux" \\
      | tr -d ' ' | grep -o '"link":"[^"]*OpenJDK27U-jdk_[^"]*[.]tar[.]gz"' \\
      | head -1 | cut -d'"' -f4)"; \\
    test -n "$BOOT_URL"; \\
    curl -sSL --retry 8 --retry-all-errors --connect-timeout 60 --max-time 1800 \\
        -o /tmp/bootjdk.tar.gz "$BOOT_URL"; \\
    mkdir -p /opt/boot-jdk-27; \\
    tar -xzf /tmp/bootjdk.tar.gz -C /opt/boot-jdk-27 --strip-components=1; \\
    rm -f /tmp/bootjdk.tar.gz; \\
    ln -sfn /opt/boot-jdk-27 /opt/boot-jdk; \\
    /opt/boot-jdk/bin/javac -version; \\
    /opt/boot-jdk/bin/java -version

ENV JAVA_HOME=/opt/boot-jdk
ENV BOOT_JDK=/opt/boot-jdk

{self.clear_env}

"""


class Openj924450To24450ImageDefault(Image):
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
        return Openj924450To24450ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{_sanitize_patch(self.pr.fix_patch)}",
            ),
            File(
                ".",
                "test.patch",
                f"{_sanitize_patch(self.pr.test_patch)}",
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
                "scrub_history.sh",
                """#!/bin/bash
set -eux

REPO_DIR="$1"
PIN_SHA="${{2:-}}"

cd "$REPO_DIR"
git config --global --add safe.directory "$REPO_DIR"

if [ -n "$PIN_SHA" ]; then
  git checkout --detach "$PIN_SHA"
else
  git checkout --detach HEAD
fi

git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0

test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
if [ -n "$PIN_SHA" ]; then
  test "$(git rev-parse HEAD)" = "$(git rev-parse "$PIN_SHA")"
fi

""".format(),
            ),
            File(
                ".",
                "build_jdk.sh",
                """#!/bin/bash
set -e

export JAVA_HOME=/opt/boot-jdk
export BOOT_JDK=/opt/boot-jdk
export PATH=$JAVA_HOME/bin:$PATH

EXT_DIR=/home/openj9-openjdk-jdk
if [ ! -d "$EXT_DIR" ]; then
  git clone --depth=1 https://github.com/ibmruntimes/openj9-openjdk-jdk.git "$EXT_DIR"
fi

cd "$EXT_DIR"

# Pin OMR to the state it was in when the graded openj9 commit was made.
#
# get_source.sh clones OMR from the tip of its tracking branch. The openj9 side is
# pinned to {sha}, so an unpinned OMR can be months newer, and the
# openj9<->OMR C++ API is not stable across that gap: the VM build then fails inside
# openj9's own headers (e.g. ObjectHash.hpp calling fieldOffsetsStartDo with an
# argument the newer OMR signature no longer declares).
#
# The pin is derived at build time from the openj9 commit's own committer date --
# no OMR SHA is hardcoded here -- using OMR's last commit at or before that instant.
# The option really is spelled "-omr-sha=<value>"; get_source.sh matches
# -<key>-<branch|reference|repo|sha>=<value> and rejects the flag if the "=" is absent.
if [ ! -d "$EXT_DIR/omr" ]; then
  OMR_URL="https://github.com/eclipse-openj9/openj9-omr.git"
  OMR_BRANCH="openj9"
  OMR_PIN=""

  OPENJ9_DATE="$(git -C /home/{repo} show -s --format=%cI {sha} 2>/dev/null || true)"
  if [ -n "$OPENJ9_DATE" ]; then
    OMR_PROBE="$(mktemp -d)"
    if git clone -q --filter=blob:none --no-checkout --single-branch --branch "$OMR_BRANCH" "$OMR_URL" "$OMR_PROBE/omr" 2>/dev/null; then
      OMR_PIN="$(git -C "$OMR_PROBE/omr" rev-list -1 --before="$OPENJ9_DATE" HEAD 2>/dev/null || true)"
    fi
    rm -rf "$OMR_PROBE"
  fi

  if [ -n "$OMR_PIN" ]; then
    echo "build_jdk: pinning OMR to $OMR_PIN (openj9 {sha} dated $OPENJ9_DATE)"
    bash get_source.sh "-omr-sha=$OMR_PIN"
    ACTUAL="$(git -C "$EXT_DIR/omr" rev-parse HEAD 2>/dev/null || true)"
    if [ "$ACTUAL" != "$OMR_PIN" ]; then
      echo "build_jdk: ERROR: OMR is at ${{ACTUAL:-<missing>}}, expected $OMR_PIN" >&2
      exit 1
    fi
  else
    echo "build_jdk: WARNING: could not derive an OMR pin; falling back to branch tip" >&2
    bash get_source.sh
  fi

  if [ ! -d "$EXT_DIR/omr" ]; then
    echo "build_jdk: ERROR: omr was not cloned" >&2
    exit 1
  fi
fi

rm -rf "$EXT_DIR/openj9"
git clone --no-checkout --no-tags "file:///home/{repo}" "$EXT_DIR/openj9"
cd "$EXT_DIR/openj9"
git fetch --no-tags origin {sha} 2>/dev/null || true
git checkout --detach {sha}
cd "$EXT_DIR"

bash /home/scrub_history.sh "$EXT_DIR/openj9" {sha}
bash /home/scrub_history.sh "$EXT_DIR/omr"

# --enable-inline-types is REQUIRED for this PR, not optional tuning.
#
# openj9's runtime/oti/j9nonbuilder.h and vm_api.h declare fieldOffsetsStartDo in two
# shapes behind a preprocessor guard:
#
#   #if defined(J9VM_OPT_VALHALLA_FLATTENABLE_VALUE_TYPES)
#     ... fieldOffsetsStartDo(vm, romClass, superClazz, state, flags, flattenedClassCache)  /* 6 args */
#   #else
#     ... fieldOffsetsStartDo(vm, romClass, superClazz, state, flags)                       /* 5 args */
#   #endif
#
# At the graded commit, runtime/oti/ObjectHash.hpp:310 calls the SIX-argument form with
# no #if around the call site, so the tree only compiles when that macro is defined.
# The macro comes from closed/OpenJ9.gmk in the openj9-openjdk-jdk extension repo:
#
#   ifeq (true,$(OPENJ9_ENABLE_ENHANCED_INLINE_TYPES))
#     CMAKE_ARGS += -DJ9VM_OPT_VALHALLA_FLATTENABLE_VALUE_TYPES=ON
#   else
#     CMAKE_ARGS += -DJ9VM_OPT_VALHALLA_FLATTENABLE_VALUE_TYPES=OFF
#   endif
#
# and OPENJ9_ENABLE_ENHANCED_INLINE_TYPES is set only by --enable-inline-types, which
# closed/autoconf/custom-hook.m4 (OPENJ9_CONFIGURE_INLINE_TYPES) defaults to FALSE
# ("no (default)"). Without the flag the build reaches ~22% and then dies with
#   ObjectHash.hpp:310: error: too many arguments to function
# which is a configuration mismatch, not source or dependency skew.
#
# This also matches what the PR is: "Add new options to control whether flattening is
# enabled", graded by BUILD_LIST=functional/Valhalla.
rm -rf "$EXT_DIR/build"
bash configure --with-boot-jdk="$BOOT_JDK" --disable-warnings-as-errors --enable-inline-types

# Fail loudly if the resulting spec did not actually enable it, rather than letting the
# build run for ~20 minutes and die at 22% again.
SPEC_FILE="$(grep -rlE '^OPENJ9_ENABLE_ENHANCED_INLINE_TYPES[[:space:]]*:?=' "$EXT_DIR/build" --include='*spec*.gmk' 2>/dev/null | head -1)"
[ -n "$SPEC_FILE" ] || SPEC_FILE="$(find "$EXT_DIR/build" -maxdepth 2 -name 'custom-spec.gmk' 2>/dev/null | head -1)"
if [ -n "$SPEC_FILE" ]; then
  if grep -qE '^OPENJ9_ENABLE_ENHANCED_INLINE_TYPES[[:space:]]*:?=[[:space:]]*true' "$SPEC_FILE"; then
    echo "build_jdk: OPENJ9_ENABLE_ENHANCED_INLINE_TYPES=true (flattenable value types ON)"
  else
    echo "build_jdk: ERROR: --enable-inline-types did not take effect; ObjectHash.hpp will not compile" >&2
    grep -E 'OPENJ9_ENABLE_ENHANCED_INLINE_TYPES|VALHALLA' "$SPEC_FILE" >&2 || true
    exit 1
  fi
fi
make JOBS="$(nproc)" images

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
set -o pipefail

EXT_DIR=/home/openj9-openjdk-jdk
export JAVA_HOME=/opt/boot-jdk
export PATH=$JAVA_HOME/bin:$PATH
export LC_ALL=C.UTF-8

bash /home/build_jdk.sh > /tmp/build.log 2>&1 || {{
  echo "BUILD_FAILED: see tail below" >&2
  tail -60 /tmp/build.log >&2
}}

TEST_JDK=$(find "$EXT_DIR/build" -maxdepth 3 -type d -name 'jdk' -path '*images*' 2>/dev/null | head -1)
if [ -z "$TEST_JDK" ]; then
  echo "ERROR: no built JDK image found; cannot run tests" >&2
  echo "===== BEGIN TEST RESULTS ====="
  echo ""
  echo "===== END TEST RESULTS ====="
  exit 1
fi

export TEST_JDK_HOME="$TEST_JDK"
cd /home/{repo}/test
export BUILD_LIST=functional/Valhalla

make -f run_configure.mk > /tmp/cfg.log 2>&1 || true
make compile > /tmp/compile.log 2>&1 || true
make _sanity > /tmp/tests.log 2>&1 || true
cat /tmp/tests.log

echo "===== BEGIN TEST RESULTS ====="
find /home/{repo}/test -path '*TestTargetResult*' -name '*.xml' -exec cat {{}} \; 2>/dev/null
find /home/{repo}/test -path '*test-results*' -name 'TEST-*.xml' -exec cat {{}} \; 2>/dev/null
echo ""
echo "===== END TEST RESULTS ====="

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout --detach {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

bash /home/build_jdk.sh || true

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh

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

        if self.config.need_clone:
            clone = (
                f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" '
                f"/home/{self.pr.repo}"
            )
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{clone}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}
ARG BASE_COMMIT="{self.pr.base.sha}"
RUN git config --global --add safe.directory /home/{self.pr.repo}
{Image._HARDENING_BLOCK}
WORKDIR /home

{self.clear_env}

"""


@Instance.register("eclipse-openj9", "openj9_24450_to_24450")
class OPENJ9_24450_TO_24450(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Openj924450To24450ImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        testcase_re = re.compile(
            r'<testcase\b(?=[^>]*\bname="([^"]*)")(?=[^>]*\bclassname="([^"]*)")'
            r'[^>]*?(/>|>(.*?)</testcase>)',
            re.DOTALL,
        )

        for match in testcase_re.finditer(clean_log):
            name = match.group(1)
            classname = match.group(2)
            closing = match.group(3)
            inner = match.group(4) or ""
            test_id = f"{classname}.{name}"

            if closing == "/>":
                passed_tests.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed_tests.add(test_id)
            elif "<skipped" in inner:
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

        for line in clean_log.splitlines():
            line = line.strip()
            match = re.match(r"^(\S+)_\d+\s+(PASSED|FAILED|SKIPPED)$", line)
            if match:
                name = match.group(1)
                status = match.group(2)
                if status == "FAILED":
                    failed_tests.add(name)
                elif status == "SKIPPED":
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("eclipse-openj9", "openj9")(OPENJ9_24450_TO_24450)
