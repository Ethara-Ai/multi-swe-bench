import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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


# apply_patch.sh -- plain `git apply` first, `--3way` only as an ANNOUNCED
# fallback. FLOW VERDICT DISCIPLINE requires a failure to be counted from the
# PRIMARY apply, so a silent --3way rescue would hide a genuinely broken patch.
# Measured on this dataset: both patches apply cleanly, 0 binary hunks.
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/ko
EXCL=/tmp/excl.$$
restore_binaries() {
  local patch="$1" path="" new=""
  : > "$EXCL"
  while IFS= read -r line; do
    case "$line" in
      "diff --git "*) path="${line#*" b/"}" ;;
      "index "*)      new="${line#*..}"; new="${new%% *}" ;;
      "Binary files "*)
        printf -- '--exclude=%s\\n' "$path" >> "$EXCL"
        if [[ "$new" =~ ^0+$ ]]; then rm -f "$path"
        elif git cat-file -e "$new" 2>/dev/null; then
          mkdir -p "$(dirname "$path")"; git cat-file blob "$new" > "$path"
        else
          echo "apply_patch: WARNING blob $new for $path not available"
        fi ;;
    esac
  done < "$patch"
}
for patch in "$@"; do
  restore_binaries "$patch"
  EX=()
  if [ -s "$EXCL" ]; then mapfile -t EX < "$EXCL"; fi
  if ! git apply --whitespace=nowarn "${EX[@]}" "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "${EX[@]}" "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
rm -f "$EXCL"
"""


# go_test_report.py -- convert `go test -json` into the trailing-keyword lines
# parse_log reads.
#
# Why -json rather than scraping `go test -v`: without -v, `go test` prints one
# line per PACKAGE and nothing per test, so the run yields no per-test evidence
# at all (FLOW GATE 2). Adding -v instead means parsing
# "--- PASS: TestFoo (0.00s)" -- which carries a DURATION that differs between
# acts and would manufacture false transitions (audit 4B).
#
# Three properties guaranteed by construction, each a real defect found in
# another Go config in this registry (OMNI_CHANGES.md 2.5):
#   1. only events carrying a "Test" field are emitted, so a PACKAGE-level
#      pass/fail can NEVER be recorded as a test;
#   2. names are package-qualified -- a Go test name is unique only within its
#      package, and this graded scope spans 6 packages;
#   3. subtests are kept whole, not truncated at the last "/".
_GO_TEST_REPORT_PY = '''"""Turn one `go test -json` stream into per-test results.

usage: go_test_report.py <events.json>

    go:pkg/build > TestGoBuildQualifyImport PASSED

Exit 0 all passed, 1 at least one failed, 2 the stream contained no tests at all
(the runner never started -- the case FLOW GATE 0 exists to catch).
"""
import json
import sys

MODULE = "github.com/google/ko/"

STATUS = {"pass": "PASSED", "fail": "FAILED", "skip": "SKIPPED"}


def main():
    path = sys.argv[1]
    results, pkgs_with_tests, pkg_level = {}, set(), {}

    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.stderr.write("go_test_report: cannot read %s: %s\\n" % (path, exc))
        return 2

    with fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("Action") not in STATUS:
                continue
            pkg = ev.get("Package", "")
            if pkg.startswith(MODULE):
                pkg = pkg[len(MODULE):]
            test = ev.get("Test")
            if not test:
                # PACKAGE-level event: recorded for the stderr summary ONLY.
                pkg_level[pkg] = ev["Action"]
                continue
            results[(pkg, test)] = ev["Action"]
            pkgs_with_tests.add(pkg)

    for pkg, action in sorted(pkg_level.items()):
        if action == "fail" and pkg not in pkgs_with_tests:
            sys.stderr.write(
                "go_test_report: PACKAGE PRODUCED NO TESTS (build failure?): %s\\n" % pkg)

    sys.stderr.write(
        "go_test_report: packages with tests=%d | package-level events=%d | tests=%d\\n"
        % (len(pkgs_with_tests), len(pkg_level), len(results)))

    if not results:
        sys.stderr.write(
            "go_test_report: the run reported no tests; the runner never started\\n")
        return 2

    failed = False
    for (pkg, test), action in sorted(results.items()):
        print("go:%s > %s %s" % (pkg, test, STATUS[action]))
        if action == "fail":
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


# run_tests.sh -- the graded command, identical in all three acts.
#
# SCOPE IS `./...`, THE WHOLE MODULE, AND THAT IS DELIBERATE -- it is what keeps
# this entry out of FLOW **Issue 28**.
#
# Every test the PR touches lives in the single package `pkg/build`, so the
# obvious scope is `./pkg/build/...`. Measured, that scope produces:
#
#     run 59 passed | test 0 tests | fix 66 passed
#
# because the test patch calls `NewGo(..., test.dir)` against a signature only
# the FIX introduces --
#     vet: pkg/build/gobuild_test.go:113:43: cannot use test.dir
#          (variable of type string) as Option value in argument to NewGo
# -- so the whole package fails to compile and the test act is a flat 0/0/0.
# That FAILS GATE 0: no act may report zero passing tests.
#
# At `./...` the other five packages still compile and run, so the compile
# failure is contained to the package that genuinely cannot build:
#
#     run 118 passed (6 pkgs) | test 59 passed (5 pkgs) | fix 125 passed (6 pkgs)
#
# The 59 tests that vanish in the test act are `pkg/build`'s; they were passing
# in the run act, so the harness reclassifies them to p2p rather than counting
# them as n2p (FLOW Issues 9 / 27).
#
# `set -e` is absent (only -uo pipefail): `go test` exits non-zero whenever any
# package fails to build, which is EXPECTED in the test act by design. Under -e
# the act would abort before the report script ran and score a silent 0/0/0.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/ko
JSON=/tmp/go-test.json
rm -f "$JSON"
go test -json -count=1 -timeout=15m ./... > "$JSON" 2>/tmp/go-test.err
go_rc=$?
echo "go test exit=${go_rc}"
echo "----- go test stderr -----"
cat /tmp/go-test.err
echo "----- per-test results -----"
python3 /home/go_test_report.py "$JSON"
"""


class KoImageBase(Image):
    """Shared base image, one per PR (`base-pr-<N>`).

    dockerfile() is deliberately NOT overridden: the harness's own
    Image.dockerfile() emits FROM -> apt -> clone -> WORKDIR -> reset ->
    checkout ${BASE_COMMIT} -> extra_setup -> scrub + its four assertions -> CMD,
    and DockerfileEnhancer prepends the BuildKit directive, the ARGs
    (BASE_COMMIT left EMPTY), the env block, labels and cert links.

    The tag carries the PR number because the base's CONTENT is per-PR -- a
    shared `:base` tag would be one name for two different images
    (FLOW Issue 25).
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    # go.mod declares `go 1.15`, but golang:1.15 is Debian buster, whose apt
    # mirrors have been moved to archive.debian.org -- the harness's mandated
    # apt layer 404s and the image never builds (the same trap that forced
    # golang:1.16 for a sibling Go config). golang:1.16 is the oldest tag whose
    # apt still works (measured: APT_OK) and it builds this tree cleanly.
    # Pinned, not :latest.
    def dependency(self) -> Union[str, "Image"]:
        return "golang:1.16"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # The harness already installs ca-certificates, curl, build-essential,
        # git, gnupg, make, python3, sudo and wget. python3 runs
        # go_test_report.py inside every act; nothing further is needed.
        return []

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}`, so go.mod/go.sum are this
        # PR's own. CGO_ENABLED=0 keeps the toolchain hermetic -- nothing in this
        # tree imports "C" (verified), so disabling cgo costs no packages and
        # removes a host-toolchain dependency.
        #
        # NOT `|| true`: this is the toolchain warm-up, not a nice-to-have cache.
        # A warm-up that fails quietly ships an image whose acts cannot run
        # (FLOW Issue 14, GATE 1). The assertions prove it took effect.
        return (
            "RUN go env -w CGO_ENABLED=0 GOFLAGS=-mod=mod && \\\n"
            "    go version && \\\n"
            "    go mod download && \\\n"
            "    go build ./... && \\\n"
            "    test -d /go/pkg/mod/github.com && \\\n"
            "    python3 --version"
        )


class KoImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and the act scripts, run
    prepare.sh -- and nothing else. The clone, the checkout and the history
    scrub already happened in the base."""

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
        return KoImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH),
            File(".", "go_test_report.py", _GO_TEST_REPORT_PY),
            File(".", "run_tests.sh", _RUN_TESTS_SH),
            # `git clean -fdq` is required, not decorative: `go test` can leave
            # build output in the tree, and `git reset --hard` does not remove
            # untracked files -- the next act would then abort on a dirty tree
            # (FLOW Issue 4). Go's caches live in /go/pkg/mod and
            # /root/.cache/go-build, OUTSIDE the work tree, so the clean cannot
            # destroy the base's warm-up.
            #
            # The warm-up is `go mod download || true` -- idempotent against the
            # cache the base already populated, and NON-DESTRUCTIVE. The asserts
            # are the guard: a lost module cache fails the IMAGE BUILD loudly
            # instead of the acts quietly scoring nothing (GATE 0).
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh
go mod download || true
go version
python3 --version
test "$(go env CGO_ENABLED)" = "0"
test -d /go/pkg/mod/github.com
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/ko
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/ko
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/ko
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh

""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image_name}

{copies}
RUN bash /home/prepare.sh

"""


@Instance.register("ko-build", "ko")
class Ko(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KoImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured status keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section the report script printed, when present.
        # The raw `go test` stderr shares the log and its lines can end in a
        # status word ("ok", "FAIL"), so a whole-log scan could invent tests.
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        result_res = [
            (re.compile(r"^(.+?)\s+PASSED$"), "pass"),
            (re.compile(r"^(.+?)\s+FAILED$"), "fail"),
            (re.compile(r"^(.+?)\s+SKIPPED$"), "skip"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            for rx, kind in result_res:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                if kind == "pass":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif kind == "fail":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                else:
                    skipped_tests.add(name)
                break

        # TestResult requires the three sets to be disjoint, else it raises.
        # Resolved toward the CONSERVATIVE outcome -- a name seen both passing
        # and skipping is counted as skipped, never claimed as a pass.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
