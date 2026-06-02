import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# hashcat is a C project built with plain GNU make (`include src/Makefile`).
#
# This file is the LEGACY era: PR number < 1436  (hashcat v3.30 .. v3.6,
# PRs 961, 1219, 1288).  The modern era (PR >= 1436, hashcat v4.0.1 ..
# v7.1.x) lives in hashcat_1436_to_99999.py and uses debian:bookworm.
#
# Discovery (interactive Docker, all 3 legacy base commits + every
# test_patch/fix_patch verified to build run/test/fix == PASS):
#
#   * Era boundary.  hashcat <= v3.6 (PR 961/1219/1288) has
#       include/affinity.h:  #if defined(__linux__)  #include <sys/sysctl.h>
#     unconditionally.  glibc dropped <sys/sysctl.h> in 2.32, so those commits
#     do NOT compile on debian:bookworm (glibc 2.36); they DO compile on
#     ubuntu:20.04 (glibc 2.31, which still ships <sys/sysctl.h>).  From
#     v4.0.1 (PR 1436) affinity.h no longer pulls sys/sysctl.h on Linux, so
#     PR >= 1436 is handled by the modern (bookworm) config instead.
#       Verified run/test-run/fix-run `make` == PASS for PR 961,1219,1288.
#
#   * OpenCL headers.  Up to ~v5.0 the OpenCL/xxHash headers are git
#     submodules (deps/OpenCL-Headers/CL, deps/git/*); v5.1+ vendors them.
#     `git submodule update --init --recursive` (run once at image build,
#     restored from cache afterwards) + the system `opencl-headers` package
#     covers every commit.  hashcat dlopen()s the OpenCL ICD at runtime, so
#     no OpenCL library is needed to *build*.
#
#   * No unit tests.  The dataset carries no captured test results
#     (run/fix/test_patch_result all 0; f2p/s2p/p2p/n2p empty) and hashcat's
#     perl test suite (tools/test.sh) needs a runtime compute backend.  As
#     with the htop / ventoy configs the meaningful signal for this dataset
#     is "does the patched C source compile" -> one synthetic test
#     `hashcat_build` driven by an unambiguous result sentinel.
#
#   * Serial make.  The bundled v5.1 patch adds deps/zlib/contrib/minizip
#     sources; under `make -j` the obj/contrib/minizip/ dir loses a mkdir
#     race and the build dies ("can't create obj/contrib/minizip/..o").
#     Plain serial `make` is reliable (~17s on CI hardware) and is used.
#
#   * Patch application.  hashcat PRs are heavily bundled (prs_in_bundle)
#     and carry binary test fixtures (tools/vc_tests/*.vc, tools/pdf_tests/
#     *.pdf, tools/2hashcat_tests/**/*.kdbx, deps/**/*.pdf|*.pk|*.chm|*.raw,
#     hashcat.hcstat*) emitted as "Binary files .. differ" / "GIT binary
#     patch" with no full index line -- `git apply` aborts the whole patch
#     on those even though none of them are compiled.  STRIP_BINARY_AWK
#     drops every binary file-section, then
#       git apply --whitespace=nowarn --exclude=deps/unrar/dll.rc
#     applies the text remainder atomically.  deps/unrar/dll.rc is a
#     Windows-only resource file whose context mismatches in the v7 bundles
#     and is never part of the Linux build.  Verified test-run AND fix-run
#     == PASS for PR 1436,1799,2877,3439,4363,4428,4435 (spanning the
#     v6.2.6->v7.0.0 and v7.0->v7.1 major-version bundles).
# ---------------------------------------------------------------------------

# awk filter: drop every "diff --git" section that contains a binary hunk
# ("GIT binary patch" or "Binary files ... differ"); keep all text sections.
STRIP_BINARY_AWK = r"""/^diff --git / {
  if (n > 0) flush()
  n = 1; buf[1] = $0; isbin = 0; next
}
{
  if (n > 0) {
    n++; buf[n] = $0
    if ($0 ~ /^GIT binary patch/ || $0 ~ /^Binary files /) isbin = 1
  } else {
    print
  }
}
function flush(   i) {
  if (!isbin) for (i = 1; i <= n; i++) print buf[i]
  n = 0
}
END { if (n > 0) flush() }
"""

# Shared build helper.  Emits exactly one result sentinel that parse_log
# keys on (build success == the synthetic test passes).
BUILD_HASHCAT = r"""
strip_binary() { awk -f /home/strip_binary.awk "$1" > "$2"; }

apply_hc_patches() {
    # $@ = original patch files; binary sections stripped before applying.
    local args=() p stripped
    for p in "$@"; do
        stripped="/tmp/$(basename "$p").txt"
        strip_binary "$p" "$stripped"
        args+=("$stripped")
    done
    if git apply --whitespace=nowarn --exclude=deps/unrar/dll.rc "${args[@]}"; then
        echo "HASHCAT_PATCH=applied"
    else
        echo "HASHCAT_BUILD_RESULT=FAIL stage=apply-patch"
        return 1
    fi
}

build_hashcat() {
    cd /home/hashcat || { echo "HASHCAT_BUILD_RESULT=FAIL stage=no-repo"; return 0; }
    make >/tmp/make.log 2>&1
    local rc=$?
    tail -n 40 /tmp/make.log
    if [ -x ./hashcat ] && ./hashcat --version >/dev/null 2>&1; then
        echo "HASHCAT_BUILD_RESULT=PASS"
    elif [ -x ./hashcat.bin ] && ./hashcat.bin --version >/dev/null 2>&1; then
        echo "HASHCAT_BUILD_RESULT=PASS"
    else
        echo "HASHCAT_BUILD_RESULT=FAIL stage=make rc=${rc}"
    fi
    return 0
}

reset_tree() {
    # $1 = base commit sha
    cd /home/hashcat || { echo "HASHCAT_BUILD_RESULT=FAIL stage=no-repo"; exit 0; }
    git reset --hard >/dev/null 2>&1
    git clean -ffdx >/dev/null 2>&1
    git checkout -f "$1" 2>/dev/null || { echo "HASHCAT_BUILD_RESULT=FAIL stage=checkout"; exit 0; }
    git submodule update --init --recursive >/dev/null 2>&1 || true
}
"""


class HashcatImageBase(Image):
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
        return "ubuntu:20.04"

    def image_tag(self) -> str:
        return "hashcat-0-to-1435-base"

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
RUN apt-get update && \\
    apt-get install -y \\
    git ca-certificates build-essential make pkg-config \\
    opencl-headers gawk \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class HashcatImageDefault(Image):
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
        return HashcatImageBase(self.pr, self._config)

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
                "strip_binary.awk",
                STRIP_BINARY_AWK,
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

if [[ -n $(git status --porcelain --ignore-submodules=all) ]]; then
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
git checkout {pr.base.sha}
git submodule update --init --recursive || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
{build_hashcat}
reset_tree {sha}
build_hashcat
""".format(build_hashcat=BUILD_HASHCAT, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
{build_hashcat}
reset_tree {sha}
apply_hc_patches /home/test.patch || exit 0
build_hashcat
""".format(build_hashcat=BUILD_HASHCAT, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
{build_hashcat}
reset_tree {sha}
apply_hc_patches /home/test.patch /home/fix.patch || exit 0
build_hashcat
""".format(build_hashcat=BUILD_HASHCAT, sha=self.pr.base.sha),
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


@Instance.register("hashcat", "hashcat_0_to_1435")
class HASHCAT_0_TO_1435(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HashcatImageDefault(self.pr, self._config)

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
        # Strip ANSI color codes before parsing.
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_re.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        name = "hashcat_build"

        re_pass = re.compile(r"^HASHCAT_BUILD_RESULT=PASS\b")
        re_fail = re.compile(r"^HASHCAT_BUILD_RESULT=FAIL\b")

        result = None
        for line in clean_log.splitlines():
            line = line.strip()
            if re_pass.match(line):
                result = "pass"
            elif re_fail.match(line):
                result = "fail"

        if result == "pass":
            passed_tests.add(name)
        else:
            # No success sentinel (build failed, crashed, or patch did not
            # apply) -> the synthetic build test failed.
            failed_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
