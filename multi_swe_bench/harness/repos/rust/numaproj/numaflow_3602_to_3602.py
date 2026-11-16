"""numaproj/numaflow -- PR 3602: graded by the React UI's Jest tests (ui/).

CI's ui job uses Node 20 and `make ui-test` (yarn install + react-scripts test --ci). Only the test files the test patch touches are run, through react-scripts' own Jest config.

Architecture: PER-PR BASE. ImageBase.image_tag() is f"base-pr-{N}" and the base
inherits the harness default Image.dockerfile(), which clones "${REPO_URL}",
checks out ${BASE_COMMIT} and runs Image._HARDENING_BLOCK (pin + history scrub)
in the base layer. DockerfileEnhancer injects the build args / proxy / CA trust
into that base. The PR image (never enhanced) only COPYs the scripts and RUNs
prepare.sh once.

Toolchain: node:20-bookworm (official multi-arch; CI's node-version) with the yarn 1 it ships and ui/yarn.lock installed --frozen-lockfile.

Grading scope: /home/msb_jest.py runs `react-scripts test --ci --json --runTestsByPath` on the touched test files that exist at the stage; ids are "<repo-relative test file>::<describe > ... > test>". A test file that fails to load reports one failing "<file load>" entry.
"""

import re
from typing import Optional
from multi_swe_bench.harness import pull_request as _pull_request

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "numaproj"
_REPO = "numaflow"
_REGISTER_KEY = "numaflow_3602_to_3602"
_BASE_IMAGE = "node:20-bookworm"

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
# Jest (react-scripts) driver for run_tests.sh.
#   msb_jest.py <package dir> -- runs the test files the test patch touches under
#   <package dir> that exist at this stage, with --json, and prints one line per test:
#       MSB_TESTCASE <tab> <repo-relative test file> <tab> <describe > ... > it> <tab> PASS|FAIL|SKIP
#   A test file that fails to load (a gold test importing code the fix adds)
#   has no assertions; it is reported as one failing "<file load>" entry.
import json
import os
import re
import subprocess
import sys

REPO = "/home/@@REPO@@"
pkg = sys.argv[1]
TEST_RE = re.compile(r"\.(test|spec)\.[jt]sx?$")


def patch_files(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r"^diff --git a/.* b/(.*?)\s*$", line)
            if m:
                out.append(m.group(1))
    return out


def main():
    os.chdir(os.path.join(REPO, pkg))
    files = [f for f in patch_files("/home/test.patch")
             if f.startswith(pkg + "/") and TEST_RE.search(f) and os.path.isfile(os.path.join(REPO, f))]
    print("MSB_TEST_FILES=" + ",".join(files), flush=True)
    if not files:
        print("MSB_NO_TEST_TARGETS_AT_THIS_STAGE")
        return 0
    out_json = "/tmp/msb-jest.json"
    if os.path.exists(out_json):
        os.remove(out_json)
    rel = [os.path.relpath(os.path.join(REPO, f), os.path.join(REPO, pkg)) for f in files]
    cmd = ["yarn", "--silent", "react-scripts", "test", "--ci", "--watchAll=false", "--no-cache",
           "--json", "--outputFile=" + out_json, "--runTestsByPath"] + rel
    print("+ " + " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, env=dict(os.environ, CI="true")).returncode
    if not os.path.exists(out_json):
        print("MSB_TEST_RUNNER_DID_NOT_COMPLETE exit=%d" % rc)
        return rc or 1
    data = json.load(open(out_json))
    results = {}
    for tr in data.get("testResults", []):
        f = os.path.relpath(tr["name"], REPO)
        if not tr.get("assertionResults"):
            if tr.get("status") == "failed":
                results[(f, "<file load>")] = "FAIL"
            continue
        for a in tr["assertionResults"]:
            name = " > ".join(list(a.get("ancestorTitles") or []) + [a.get("title", "")])
            st = {"passed": "PASS", "failed": "FAIL"}.get(a.get("status"), "SKIP")
            key = (f, name)
            if results.get(key) != "FAIL":
                results[key] = st
    for (f, name), st in sorted(results.items()):
        print("MSB_TESTCASE\t%s\t%s\t%s" % (f, name, st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
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
(cd ui && yarn install --frozen-lockfile --non-interactive --network-timeout 600000)

# ---- Section 3: GATE (hard, never tolerated) --------------------------------
test -x ui/node_modules/.bin/react-scripts
(cd ui && yarn install --frozen-lockfile --offline --non-interactive)
echo "DEPS_OK"
"""

_RUN_TESTS = r"""#!/bin/bash
# The single test command shared verbatim by run.sh, test-run.sh and fix-run.sh.
# Test failures are data (reported per test); a runner that does not complete
# exits non-zero.
set -o pipefail
cd /home/@@REPO@@
source /home/msb_env.sh
python3 /home/msb_jest.py ui
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


class Numaflow3602To3602ImageBase(Image):
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


class Numaflow3602To3602ImageDefault(Image):
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
        return Numaflow3602To3602ImageBase(self.pr, self._config)

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
            File(".", "msb_jest.py", _render(_MSB_RUNNER, pr)),
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
class NUMAFLOW_3602_TO_3602(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Numaflow3602To3602ImageDefault(self.pr, self._config)

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
