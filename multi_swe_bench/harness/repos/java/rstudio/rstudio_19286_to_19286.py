"""rstudio/rstudio -- GoogleTest unit tests of the C++ core library (rstudio-core-tests); PR 19286.

Of the Dataset 3 rstudio PRs only 19286 has tests that run without a full RStudio build (GWT front end, R session, server): src/cpp/core/system/PosixSystemTests.cpp, compiled into the `rstudio-core-tests` GoogleTest binary. src/cpp is configured with RSTUDIO_CONFIG_CORE_DEV=ON (ext + shared_core + core only, no R) and RSTUDIO_UNIT_TESTS_ENABLED=ON. Boost 1.91.0 is the version src/cpp/CMakeLists.txt requests (RSTUDIO_BOOST_REQUESTED_VERSION); it is built from the official boost.io source tarball (sha256-checked) with the components the CMake list names, under the plain `boost` namespace. SOCI 4.0.3 (the version RStudio's own install-soci uses) and yaml-cpp come from Debian. lsof is installed for PosixTests.WorkingDirLsof. The Win32 tests and test-rig.R in the test patch do not run on Linux / without R.

Architecture: PER-PR BASE. ImageBase.image_tag() is f"base-pr-{N}" and the base
inherits the harness default Image.dockerfile(), which clones "${REPO_URL}",
checks out ${BASE_COMMIT} and runs Image._HARDENING_BLOCK (pin + history scrub)
in the base layer. DockerfileEnhancer injects the build args / proxy / CA trust
into that base. The PR image (never enhanced) only COPYs the scripts and RUNs
prepare.sh once.

Toolchain: debian:trixie (official multi-arch; GCC 14, CMake 3.31), Boost 1.91.0 built in the base layer, Debian's libsoci-dev / libyaml-cpp-dev and the system libraries core links (OpenSSL, zlib, SQLite, libpq, uuid, acl, PAM, bzip2, libcap).

Grading scope: /home/msb_gtest.py rebuilds rstudio-core-tests (Ninja, build tree /home/msb-build, outside the repo) for each stage's sources and runs it with --gtest_output=xml; the tests of the C++ test files the test patch touches are graded, ids "<repo-relative test file>::<Suite>.<Name>". A stage whose sources do not compile reports the TEST cases the test patch adds in those files as FAIL and never runs a stale binary.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "rstudio"
_REPO = "rstudio"
_REGISTER_KEY = "rstudio_19286_to_19286"

# Single-PR routing (DS3 multi-arch set). Dataset rows carry no number_interval, so without
# this the row would resolve to the "{org}/{repo}" catch-all config. Only this exact PR
# number of this repo is tagged with this file's interval; every other PR is unaffected.
from multi_swe_bench.harness import pull_request as _pull_request

_INTERVAL = _REGISTER_KEY
_BOUNDS = re.search(r"_(\d+)_to_(\d+)$", _INTERVAL)
_PR_MIN = int(_BOUNDS.group(1))
_PR_MAX = int(_BOUNDS.group(2))
_ROUTING_FLAG = "_" + _INTERVAL + "_routed"

if not getattr(_pull_request.PullRequest, _ROUTING_FLAG, False):
    _orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _routed_from_json(cls, json_str):
        pr = _orig_from_json(cls, json_str)
        if (
            pr.org == _ORG
            and pr.repo == _REPO
            and not pr.number_interval
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return pr

    _pull_request.PullRequest.from_json = classmethod(_routed_from_json)
    setattr(_pull_request.PullRequest, _ROUTING_FLAG, True)
_BASE_IMAGE = "debian:trixie"


_CHECK_GIT_CHANGES = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_MSB_ENV = r"""# Sourced by prepare.sh and run_tests.sh -- one definition of the toolchain.
export CI=true
export MSB_TIMEOUT_SECS=2400
"""

_MSB_RUNNER = r"""#!/usr/bin/env python3
# CMake + GoogleTest driver for prepare.sh / run_tests.sh.
#   msb_gtest.py build  -- configure (once) and build the test target (provision / gate)
#   msb_gtest.py run    -- rebuild the test target for this stage's sources, run it with
#                          --gtest_output=xml and print one line per test of the graded files:
#       MSB_TESTCASE <tab> <repo-relative test file> <tab> <Suite>.<Name> <tab> PASS|FAIL|SKIP
# The graded files are the C++ test files the test patch touches that the test
# binary defines tests in (gtest's XML carries each test's file).
# A stage whose sources do not build (a gold test calling an API the fix adds)
# never runs the stale binary: the TEST/TEST_F/TEST_P cases the test patch adds
# or edits in a graded file are reported as FAIL, and nothing else.
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

REPO = "/home/@@REPO@@"
CFG = json.loads(r'''{"build_dir": "/home/msb-build", "source_dir": "src/cpp", "target": "rstudio-core-tests", "binary": "core/rstudio-core-tests", "record_files": true, "test_re": "^src/cpp/.*Tests\\.cpp$", "cmake_args": ["-DCMAKE_BUILD_TYPE=Debug", "-DRSTUDIO_CONFIG_CORE_DEV=ON", "-DRSTUDIO_UNIT_TESTS_ENABLED=ON", "-DRSTUDIO_USE_SYSTEM_SOCI=ON", "-DBoost_ROOT=/opt/boost", "-DRSTUDIO_TARGET=Server"]}''')
BUILD = CFG["build_dir"]
TIMEOUT = int(os.environ.get("MSB_TIMEOUT_SECS", "1500"))
FILES_SEEN = os.path.join(BUILD, "msb-test-files.txt")
XML = "/tmp/msb-gtest.xml"
TEST_RE = re.compile(r"^\+?\s*(?:TEST|TEST_F|TEST_P)\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)")


def patch_files(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r"^diff --git a/.* b/(.*?)\s*$", line)
            if m:
                out.append(m.group(1))
    return out


def call(cmd, cwd):
    print("+ " + " ".join(cmd), flush=True)
    try:
        return subprocess.run(cmd, cwd=cwd, timeout=TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        print("MSB_TIMEOUT " + " ".join(cmd), flush=True)
        return 124


def build():
    if not os.path.exists(os.path.join(BUILD, "build.ninja")):
        os.makedirs(BUILD, exist_ok=True)
        rc = call(["cmake", "-G", "Ninja", os.path.join(REPO, CFG["source_dir"])] + CFG["cmake_args"], BUILD)
        if rc:
            return rc
    return call(["ninja", "-j%d" % int(os.environ.get("MSB_JOBS", os.cpu_count() or 4)), CFG["target"]], BUILD)


def run_binary():
    if os.path.exists(XML):
        os.remove(XML)
    exe = os.path.join(BUILD, CFG["binary"])
    rc = call([exe, "--gtest_output=xml:" + XML], os.path.dirname(exe))
    results = {}
    if not os.path.exists(XML):
        print("MSB_TEST_RUNNER_DID_NOT_COMPLETE exit=%d" % rc)
        return None
    for tc in ET.parse(XML).getroot().iter("testcase"):
        f = tc.get("file") or ""
        f = os.path.relpath(f, REPO) if f.startswith("/") else f
        name = "%s.%s" % (tc.get("classname"), tc.get("name"))
        if tc.find("failure") is not None or tc.find("error") is not None:
            st = "FAIL"
        elif tc.get("result") == "skipped" or tc.get("status") == "notrun" or tc.find("skipped") is not None:
            st = "SKIP"
        else:
            st = "PASS"
        results[(f, name)] = st
    return results


def gold_tests():
    out, cur = [], None
    if not os.path.exists("/home/test.patch"):
        return out
    for line in open("/home/test.patch", encoding="utf-8", errors="replace"):
        m = re.match(r"^diff --git a/.* b/(.*?)\s*$", line)
        if m:
            cur = m.group(1)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            t = TEST_RE.match(line[1:])
            if t:
                out.append((cur, "%s.%s" % (t.group(1), t.group(2))))
    return out


def main(mode):
    os.chdir(REPO)
    if mode == "build":
        rc = build()
        if rc == 0 and CFG.get("record_files"):
            res = run_binary() or {}
            with open(FILES_SEEN, "w") as fh:
                fh.write("\n".join(sorted({f for f, _ in res})) + "\n")
            print("MSB_TEST_FILES_RECORDED=%d" % len({f for f, _ in res}))
        return rc
    seen = set(open(FILES_SEEN).read().split()) if os.path.exists(FILES_SEEN) else set()
    graded = [f for f in patch_files("/home/test.patch") if re.search(CFG["test_re"], f)]
    print("MSB_GRADED_FILES=" + ",".join(graded), flush=True)
    rc = build()
    if rc != 0:
        print("MSB_TEST_TARGET_BUILD_FAILED exit=%d" % rc)
        for f, name in gold_tests():
            if f in graded and f in seen:
                print("MSB_TESTCASE\t%s\t%s\tFAIL" % (f, name))
        return 0
    res = run_binary()
    if res is None:
        return 1
    for (f, name), st in sorted(res.items()):
        if f in graded:
            print("MSB_TESTCASE\t%s\t%s\t%s" % (f, name, st))
    # Failing tests are data; the runner itself completed.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "run"))
"""

_PREPARE = r"""#!/bin/bash
set -e

# ---- Section 1: PIN ---------------------------------------------------------
cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

# ---- Section 2: PROVISION (the tree may get dirty from here on) -------------
source /home/msb_env.sh
python3 /home/msb_gtest.py build

# ---- Section 3: GATE (hard, never tolerated) --------------------------------
test -x /home/msb-build/core/rstudio-core-tests
test -s /home/msb-build/msb-test-files.txt
grep -qx 'src/cpp/core/system/PosixSystemTests.cpp' /home/msb-build/msb-test-files.txt
echo "DEPS_OK"
"""

_RUN_TESTS = r"""#!/bin/bash
# The single test command shared verbatim by run.sh, test-run.sh and fix-run.sh.
# Test failures are data (reported per test); a runner that does not complete
# exits non-zero.
set -o pipefail
cd /home/@@REPO@@
source /home/msb_env.sh
python3 /home/msb_gtest.py run
"""

_APPLY_FN = r"""# Apply one patch file. Each file gets its own context ladder: full context
# first; only if the base commit moved under the patch's context (the PR
# branch was cut from an older parent than base.sha) retry with 2, then 1,
# lines. git apply is atomic per file, so a retry never meets half-applied
# hunks of the same file; if no attempt applies, the stage fails loudly.
# Entries of the form "Binary files A and B differ" (the patch was exported
# without --binary) carry no bytes at all and can never apply; only those
# paths are excluded, every other hunk is applied as-is.
_msb_apply() {
    local _ex=() _f
    while IFS= read -r _f; do
        [ -n "$_f" ] && _ex+=("--exclude=$_f")
    done < <(sed -nE 's#^Binary files (.*) and (.*) differ$#\1\n\2#p' "$1" \
                 | grep -v '^/dev/null$' | sed -E 's#^[ab]/##' | sort -u)
    git apply --whitespace=nowarn "${_ex[@]}" "$1" \
        || git apply --whitespace=nowarn -C2 "${_ex[@]}" "$1" \
        || git apply --whitespace=nowarn -C1 "${_ex[@]}" "$1"
}
"""


def _apply(patches: str) -> str:
    return "\n".join(f"_msb_apply {p}" for p in patches.split())


def _stage(apply_line: str) -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "export CI=true\n"
        "\n"
        + (_APPLY_FN + "\n" if apply_line else "")
        + "cd /home/@@REPO@@\n"
        + (apply_line + "\n" if apply_line else "")
        + "bash /home/run_tests.sh\n"
    )


_RUN = _stage("")
_TEST_RUN = _stage(_apply("/home/test.patch"))
_FIX_RUN = _stage(_apply("/home/test.patch /home/fix.patch"))


def _render(body: str, pr: PullRequest) -> str:
    return body.replace("@@REPO@@", pr.repo).replace("@@SHA@@", pr.base.sha)


class RstudioImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        # PR-scoped: the default dockerfile() pins ${BASE_COMMIT} and scrubs
        # history in this layer, so it must never be shared across PRs.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["cmake", "ninja-build", "bzip2", "lsof", "libssl-dev", "zlib1g-dev", "libsoci-dev", "libyaml-cpp-dev", "libsqlite3-dev", "libpq-dev", "uuid-dev", "libacl1-dev", "libpam0g-dev", "libbz2-dev", "libcap-dev"]

    def extra_setup(self) -> str:
        return 'RUN mkdir -p /tmp/boost && cd /tmp/boost && curl -fsSL -o boost.tar.bz2 https://archives.boost.io/release/1.91.0/source/boost_1_91_0.tar.bz2 && echo \'de5e6b0e4913395c6bdfa90537febd9028ea4c0735d2cdb0cd9b45d5f51264f5  boost.tar.bz2\' | sha256sum -c - && tar xjf boost.tar.bz2 && cd boost_1_91_0 && ./bootstrap.sh --prefix=/opt/boost >/dev/null && ./b2 -j"$(nproc)" -d0 link=static variant=release cxxflags=-fPIC cflags=-fPIC --with-atomic --with-chrono --with-date_time --with-filesystem --with-iostreams --with-program_options --with-random --with-regex --with-thread --with-url install && cd / && rm -rf /tmp/boost'


class RstudioImageDefault(Image):
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
        return RstudioImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "msb_env.sh", _render(_MSB_ENV, pr)),
            File(".", "msb_gtest.py", _render(_MSB_RUNNER, pr)),
            File(".", "prepare.sh", _render(_PREPARE, pr)),
            File(".", "run_tests.sh", _render(_RUN_TESTS, pr)),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_TEST_RUN, pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN, pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


_CASE_RE = re.compile(r"^MSB_TESTCASE\t([^\t\n]+)\t([^\t\n]+)\t(PASS|FAIL|SKIP)[ \t\r]*$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_DIFF_B_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)


@Instance.register(_ORG, _REGISTER_KEY)
class Rstudio(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RstudioImageDefault(self.pr, self._config)

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
        clean = _ANSI_RE.sub("", test_log)

        # Tests defined in a file the FIX patch writes (Rust keeps unit tests in
        # the source file they cover) are the fix's own tests, not the gold
        # test patch's: crediting them would grade the fix against itself, so
        # they are not reported at any stage. Gold test files are never in the
        # fix patch.
        fix_files = set(_DIFF_B_RE.findall(self.pr.fix_patch or "")) - set(
            _DIFF_B_RE.findall(self.pr.test_patch or "")
        )

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # "<repo-relative source file>::<test path>": report.py splits on the
        # first "::" to map a credited test back to the file that defines it.
        for path, name, status in _CASE_RE.findall(clean):
            if path in fix_files:
                continue
            test_id = f"{path}::{name}"
            if status == "FAIL":
                failed_tests.add(test_id)
            elif status == "SKIP":
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

        # Failure wins over pass, pass wins over skip; the buckets never overlap.
        passed_tests -= failed_tests
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
