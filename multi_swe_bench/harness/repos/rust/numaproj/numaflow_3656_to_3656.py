"""numaproj/numaflow -- PR 3656: graded by Go unit tests of pkg/apis/numaflow/v1alpha1.

go.mod requires go 1.25.10 at base commit e356ad485e; the Rust workspace under rust/ is not involved.

Architecture: PER-PR BASE. ImageBase.image_tag() is f"base-pr-{N}" and the base
inherits the harness default Image.dockerfile(), which clones "${REPO_URL}",
checks out ${BASE_COMMIT} and runs Image._HARDENING_BLOCK (pin + history scrub)
in the base layer. DockerfileEnhancer injects the build args / proxy / CA trust
into that base. The PR image (never enhanced) only COPYs the scripts and RUNs
prepare.sh once.

Toolchain: golang:1.25.10-bookworm (official multi-arch), go.mod's exact version.

Grading scope: /home/msb_gotest.py runs `go test -v -count=1` on the packages owning a _test.go file the test patch touches and prints one line per test (subtests included); ids are "<repo-relative _test.go file>::<TestName[/subtest]>". A package that does not build at a stage reports the Test functions the test patch adds or edits as FAIL.
"""

import re
from typing import Optional
from multi_swe_bench.harness import pull_request as _pull_request

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "numaproj"
_REPO = "numaflow"
_REGISTER_KEY = "numaflow_3656_to_3656"
_BASE_IMAGE = "golang:1.25.10-bookworm"

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
export GOFLAGS=-mod=mod
export GOTOOLCHAIN=local
"""

_MSB_RUNNER = r"""#!/usr/bin/env python3
# `go test` driver for run_tests.sh / prepare.sh.
#   msb_gotest.py run   -- go test -v the packages owning a *_test.go file the
#                          test patch touches; one line per test:
#       MSB_TESTCASE <tab> <repo-relative _test.go file> <tab> <TestName[/subtest]> <tab> PASS|FAIL|SKIP
#                          A package that does not build at this stage (a gold
#                          test using an API the fix adds) reports the Test
#                          functions the test patch adds or edits as FAIL.
#   msb_gotest.py build -- go test -run '^$' over the same packages (provision / gate).
import os
import re
import subprocess
import sys

REPO = "/home/@@REPO@@"
TIMEOUT = int(os.environ.get("MSB_TIMEOUT_SECS", "1500"))


def patch_files(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r"^diff --git a/.* b/(.*?)\s*$", line)
            if m:
                out.append(m.group(1))
    return out


def packages():
    pk = []
    for rel in patch_files("/home/test.patch"):
        if rel.endswith("_test.go"):
            d = os.path.dirname(rel) or "."
            if os.path.isdir(os.path.join(REPO, d)) and d not in pk:
                pk.append(d)
    return pk


FUNC_RE = re.compile(r"^func (Test[A-Za-z0-9_]*)\s*\(")


def tests_by_file(pkg_dir):
    out = {}
    d = os.path.join(REPO, pkg_dir)
    for f in sorted(os.listdir(d)):
        if f.endswith("_test.go"):
            for line in open(os.path.join(d, f), encoding="utf-8", errors="replace"):
                m = FUNC_RE.match(line)
                if m:
                    out[m.group(1)] = os.path.join(pkg_dir, f)
    return out


def gold_tests(pkg_dir):
    # Test functions the test patch adds or whose body it edits, per file.
    out, cur, keep = {}, None, False
    if not os.path.exists("/home/test.patch"):
        return out
    for line in open("/home/test.patch", encoding="utf-8", errors="replace"):
        m = re.match(r"^diff --git a/.* b/(.*?)\s*$", line)
        if m:
            cur = m.group(1)
            keep = cur.endswith("_test.go") and os.path.dirname(cur) == pkg_dir
            continue
        if not keep:
            continue
        if line.startswith("@@"):
            h = re.search(r"@@[^@]*@@\s*func (Test\w*)\s*\(", line)
            if h:
                out.setdefault(h.group(1), cur)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            m2 = FUNC_RE.match(line[1:])
            if m2:
                out[m2.group(1)] = cur
    return out


RESULT_RE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
PKG_FAIL_RE = re.compile(r"^FAIL\s+(\S+)\s+\[(build failed|setup failed)\]")


def main(mode):
    os.chdir(REPO)
    pk = packages()
    print("MSB_PACKAGES=" + ",".join(pk), flush=True)
    if not pk:
        print("MSB_NO_TEST_TARGETS_AT_THIS_STAGE")
        return 0 if mode == "run" else 1
    args = ["./" + p for p in pk]
    if mode != "run":
        cmd = ["go", "test", "-count=1", "-run", "^$"] + (["-mod=mod"] if False else []) + args
        print("+ " + " ".join(cmd), flush=True)
        return subprocess.run(cmd).returncode
    mod = subprocess.run(["go", "list", "-m"], stdout=subprocess.PIPE, text=True).stdout.strip()
    cmd = ["go", "test", "-v", "-count=1", "-timeout", "%ds" % max(60, TIMEOUT - 120)] + args
    print("+ " + " ".join(cmd), flush=True)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    results, broken = {}, []
    files = {d: tests_by_file(d) for d in pk}
    for line in p.stdout:
        sys.stdout.write(line)
        m = RESULT_RE.match(line)
        if m:
            st, name = m.group(1), m.group(2)
            top = name.split("/", 1)[0]
            f = next((files[d][top] for d in pk if top in files[d]), None) or pk[0]
            key = (f, name)
            if results.get(key) != "FAIL":
                results[key] = st
            continue
        m = PKG_FAIL_RE.match(line)
        if m:
            path = m.group(1)
            rel = path[len(mod) + 1:] if path.startswith(mod + "/") else path
            broken.append(rel)
    p.wait()
    for (f, name), st in sorted(results.items()):
        print("MSB_TESTCASE\t%s\t%s\t%s" % (f, name, st))
    for d in broken:
        for name, f in sorted(gold_tests(d).items()):
            if (f, name) not in results:
                print("MSB_TESTCASE\t%s\t%s\tFAIL" % (f, name))
        print("MSB_PACKAGE_BUILD_FAILED " + d)
    # go test exits 1 for failing tests or a broken package: both are data here.
    return 0 if p.returncode in (0, 1) else p.returncode


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
go mod download
python3 /home/msb_gotest.py build

# ---- Section 3: GATE (hard, never tolerated) --------------------------------
GOPROXY=off python3 /home/msb_gotest.py build
echo "DEPS_OK"
"""

_RUN_TESTS = r"""#!/bin/bash
# The single test command shared verbatim by run.sh, test-run.sh and fix-run.sh.
# Test failures are data (reported per test); a runner that does not complete
# exits non-zero.
set -o pipefail
cd /home/@@REPO@@
source /home/msb_env.sh
python3 /home/msb_gotest.py run
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


class Numaflow3656To3656ImageBase(Image):
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
        return ["python3"]

    def extra_setup(self) -> str:
        return ""


class Numaflow3656To3656ImageDefault(Image):
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
        return Numaflow3656To3656ImageBase(self.pr, self._config)

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
            File(".", "msb_gotest.py", _render(_MSB_RUNNER, pr)),
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
class NUMAFLOW_3656_TO_3656(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Numaflow3656To3656ImageDefault(self.pr, self._config)

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
