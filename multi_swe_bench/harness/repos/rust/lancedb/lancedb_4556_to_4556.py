"""lancedb/lancedb -- PR 4556: graded by the Python unittest ci/tests/test_python_dependencies.py.

The test patch adds a stdlib-unittest check that python/pyproject.toml's `pylance` extra matches the Lance major version pinned in the root Cargo.toml (read with tomllib, compared with `packaging`). No Rust build is involved.

Architecture: PER-PR BASE. ImageBase.image_tag() is f"base-pr-{N}" and the base
inherits the harness default Image.dockerfile(), which clones "${REPO_URL}",
checks out ${BASE_COMMIT} and runs Image._HARDENING_BLOCK (pin + history scrub)
in the base layer. DockerfileEnhancer injects the build args / proxy / CA trust
into that base. The PR image (never enhanced) only COPYs the scripts and RUNs
prepare.sh once.

Toolchain: python:3.12-slim-bookworm (official multi-arch; tomllib needs Python >= 3.11) and packaging==25.0.

Grading scope: /home/msb_unittest.py discovers the unittest modules under ci/tests (absent before the test patch: an honest NONE) and prints one line per test; ids are "<repo-relative test file>::<Class>::<method>".
"""

import re
from typing import Optional
from multi_swe_bench.harness import pull_request as _pull_request

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "lancedb"
_REPO = "lancedb"
_REGISTER_KEY = "lancedb_4556_to_4556"
_BASE_IMAGE = "python:3.12-slim-bookworm"

# PR-range routing. The dataset rows carry no number_interval, so without this a
# row would resolve to the "{org}/{repo}" key of the older config(s) for this
# repo. Tag rows in range with this era's interval as they are parsed.
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
export MSB_TIMEOUT_SECS=1500
"""

_MSB_RUNNER = r"""#!/usr/bin/env python3
# unittest driver for run_tests.sh. Usage: msb_unittest.py <start dir> [<pattern>]
#
# Discovers the tests under <start dir> (relative to the repo, which is also the
# working directory), runs them verbosely and prints one line per test:
#     MSB_TESTCASE <tab> <repo-relative test file> <tab> <Class>::<method> <tab> PASS|FAIL|SKIP
# A test module that fails to import (a gold test using code the fix adds) is
# reported by unittest as a failing `_FailedTest`; it is mapped back to the
# module's file so it reads as a failure of that file, never as an absent test.
import os
import sys
import unittest

REPO = "/home/@@REPO@@"
start = sys.argv[1]
pattern = sys.argv[2] if len(sys.argv) > 2 else "test*.py"
start_abs = os.path.join(REPO, start)
sys.path.insert(0, start_abs)
for _p in os.environ.get("MSB_PYTHONPATH", "").split(":"):
    if _p:
        sys.path.insert(0, os.path.join(REPO, _p))

results = {}


def where(test):
    mod = sys.modules.get(type(test).__module__)
    f = getattr(mod, "__file__", None)
    cls = type(test).__name__
    meth = getattr(test, "_testMethodName", str(test))
    if cls == "_FailedTest":  # import failure of module <meth>
        f = os.path.join(start_abs, meth.replace(".", os.sep) + ".py")
        return os.path.relpath(f, REPO), "MODULE_IMPORT"
    if not f:
        return start, "%s::%s" % (cls, meth)
    return os.path.relpath(os.path.abspath(f), REPO), "%s::%s" % (cls, meth)


class Result(unittest.TextTestResult):
    def _set(self, test, status):
        key = where(test)
        if results.get(key) == "FAIL":
            return
        if results.get(key) == "PASS" and status == "SKIP":
            return
        results[key] = status

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "FAIL")

    def addError(self, test, err):
        super().addError(test, err)
        if isinstance(test, unittest.case._SubTest):
            test = test.test_case
        if type(test).__name__ == "_ErrorHolder":
            return  # setUpClass/module fixture error: its tests are reported on their own
        self._set(test, "FAIL")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set(test, "SKIP")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._set(test, "PASS")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set(test, "FAIL")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._set(test, "FAIL")


if __name__ == "__main__":
    os.chdir(REPO)
    if not os.path.isdir(start_abs):
        print("MSB_NO_TEST_TARGETS_AT_THIS_STAGE (%s does not exist)" % start)
        sys.exit(0)
    suite = unittest.defaultTestLoader.discover(start_abs, pattern=pattern, top_level_dir=start_abs)
    unittest.TextTestRunner(resultclass=Result, verbosity=2, stream=sys.stdout).run(suite)
    sys.stdout.flush()
    for (f, name), st in sorted(results.items()):
        print("MSB_TESTCASE\t%s\t%s\t%s" % (f, name, st))
    # Test failures are data; the runner itself completed.
    sys.exit(0)
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
pip install --no-cache-dir packaging==25.0

# ---- Section 3: GATE (hard, never tolerated) --------------------------------
python3 -c "import tomllib, packaging.requirements, packaging.version"
echo "DEPS_OK"
"""

_RUN_TESTS = r"""#!/bin/bash
# The single test command shared verbatim by run.sh, test-run.sh and fix-run.sh.
# Test failures are data (reported per test); a runner that does not complete
# exits non-zero.
set -o pipefail
cd /home/@@REPO@@
source /home/msb_env.sh
python3 /home/msb_unittest.py ci/tests
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


class Lancedb4556To99999ImageBase(Image):
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
        return []

    def extra_setup(self) -> str:
        return ""


class Lancedb4556To99999ImageDefault(Image):
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
        return Lancedb4556To99999ImageBase(self.pr, self._config)

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
            File(".", "msb_unittest.py", _render(_MSB_RUNNER, pr)),
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
class LANCEDB_4556_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Lancedb4556To99999ImageDefault(self.pr, self._config)

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
