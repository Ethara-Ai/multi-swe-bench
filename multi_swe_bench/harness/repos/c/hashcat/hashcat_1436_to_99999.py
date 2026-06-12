import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# hashcat is a C project built with plain GNU make (`include src/Makefile`).
#
# This file is the MODERN era: PR number >= 1436  (hashcat v4.0.1 .. v7.1.x).
# The legacy era (PR < 1436, hashcat v3.30 .. v3.6) lives in
# hashcat_0_to_1435.py and uses an older base image (glibc < 2.32 still ships
# <sys/sysctl.h>, which include/affinity.h needs there).
#
#   * OpenCL headers.  Up to ~v5.0 the OpenCL/xxHash headers are git
#     submodules (deps/OpenCL-Headers/CL, deps/git/*); v5.1+ vendors them.
#     `git submodule update --init --recursive` + the system `opencl-headers`
#     package covers every commit.  hashcat dlopen()s the OpenCL ICD at
#     runtime, so no OpenCL library is needed to *build*.
#
#   * No unit tests.  The meaningful signal for this dataset is "does the
#     patched C source compile" -> one synthetic test `hashcat_build` driven
#     by an unambiguous result sentinel.
#
#   * Serial make.  The bundled v5.1 patch adds deps/zlib/contrib/minizip
#     sources; under `make -j` the obj/contrib/minizip/ dir loses a mkdir
#     race.  Plain serial `make` is reliable and is used.
#
#   * Patch application.  hashcat PRs are heavily bundled and carry binary
#     test fixtures emitted as "Binary files .. differ" / "GIT binary patch"
#     with no full index line -- `git apply` aborts the whole patch on those
#     even though none of them are compiled.  STRIP_BINARY_AWK drops every
#     binary file-section, then
#       git apply --whitespace=nowarn --exclude=deps/unrar/dll.rc
#     applies the text remainder atomically (deps/unrar/dll.rc is a
#     Windows-only resource file never part of the Linux build).
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
    mkdir -p /home/hashcat
    cd /home/hashcat || { echo "HASHCAT_BUILD_RESULT=FAIL stage=no-repo"; exit 0; }
    git reset --hard >/dev/null 2>&1
    git clean -ffdx >/dev/null 2>&1
    git checkout -f "$1" 2>/dev/null || { echo "HASHCAT_BUILD_RESULT=FAIL stage=checkout"; exit 0; }
    git submodule update --init --recursive >/dev/null 2>&1 || true
}
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

    def dependency(self) -> str:
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", runs extra_setup(), and appends the
        # _HARDENING_BLOCK that strips every other ref/commit so the fix can't be
        # read out of git history. DockerfileEnhancer then injects the proxy/cert
        # infra and the final sanitize pass. None of that fires when dockerfile()
        # is overridden, which is why the previous two-stage build bypassed it.
        return "debian:bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        # build-essential + make + git + ca-certificates are in image.py's
        # default package set; these are the hashcat-specific build deps.
        return ["pkg-config", "opencl-headers", "gawk"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Stages the patches + awk filter + helper scripts into /home/
        # and runs prepare.sh (which warms the git submodules so the eval runs
        # offline). The copied files live outside /home/{repo}, so the hardening
        # pass leaves them untouched.
        return (
            "ENV LC_ALL=C.UTF-8\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY strip_binary.awk /home/strip_binary.awk\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "strip_binary.awk", STRIP_BINARY_AWK),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(); this only resets the tree and warms the git submodules.
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git reset --hard || true
git submodule update --init --recursive || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
mkdir -p /home/hashcat
{build_hashcat}
reset_tree {sha}
build_hashcat
""".format(build_hashcat=BUILD_HASHCAT, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
mkdir -p /home/hashcat
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
mkdir -p /home/hashcat
{build_hashcat}
reset_tree {sha}
apply_hc_patches /home/test.patch /home/fix.patch || exit 0
build_hashcat
""".format(build_hashcat=BUILD_HASHCAT, sha=self.pr.base.sha),
            ),
        ]


@Instance.register("hashcat", "hashcat_1436_to_99999")
class HASHCAT_1436_TO_99999(Instance):
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
