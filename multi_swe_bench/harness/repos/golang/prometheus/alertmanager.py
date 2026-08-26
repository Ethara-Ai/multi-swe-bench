import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# prometheus/alertmanager — the Alertmanager service from the Prometheus family.
# Plain Go, standard `go test`, 60 packages / 47 test files at PR #2534's base sha.
#
# Discovery (verified in Docker at base sha 54431be8; every number was measured):
#
#   stage 1 (no patches)        73 passed, 0 failed
#   stage 2 (test.patch)        DOES NOT COMPILE -> 0 tests collected
#   stage 3 (test + fix.patch)  77 passed, 0 failed
#
# Stage 2 not compiling is expected here and is NOT a defect in the patch split.
# The gold tests reference a struct field the fix introduces:
#     config/config_test.go:890: firstConfig.APIURLFile undefined
#         (type *SlackConfig has no field or method APIURLFile)
#     notify/slack/slack_test.go:73: unknown field 'APIURLFile' in struct literal
# In a statically typed language a test for a not-yet-existing field cannot
# compile, so there is no "tests present and failing" state to observe. The
# consequence is f2p = 0 and the four new tests landing as n2p.
#
# That is still a SOLVABLE datapoint, which is the important distinction: the
# four new tests are added by **test.patch**, not by fix.patch (test.patch adds
# 4 `func Test...` and zero source changes; fix.patch changes only config.go,
# notifiers.go, slack.go and two .md files). At evaluation the harness applies
# the gold test patch alongside the model's patch, so a model that adds the
# APIURLFile field makes those tests compile and pass on its own merits.
#
# Toolchain and scope, each settled by measurement rather than by assumption:
#
#  - `golang:1.16`. `.promu.yml` pins `go: 1.16` at this sha (go.mod's `go 1.14`
#    is only a language-level minimum), and the sibling configs in this
#    directory are already era-split the same way (prometheus_go1_13/14/16).
#
#  - **No GOFLAGS / CGO_ENABLED overrides.** The repo vendors its dependencies
#    (`vendor/` is present), so Go 1.16 implies `-mod=vendor` and the whole test
#    run works with **no network** once the clone exists. An earlier attempt set
#    `GOFLAGS=-mod=mod`, which is actively worse: it bypasses vendor/ and makes
#    every stage depend on the module proxy. Verified: plain `go test ./config`
#    exits 0 with an empty GOFLAGS.
#
#  - Packages under test are derived from the directories this PR's own patches
#    touch (the established pattern in prometheus_go1_16.py), which resolves to
#    `./config ./notify/slack` here. That is not merely a speed optimisation --
#    it is what keeps the flaky acceptance tree out of the measurement. See the
#    NOT RUN note in run_tests.sh.
#
#  - parse_log emits "<file>::<TestName>", NOT the bare test name the sibling
#    configs in this directory use. Bare names are provably unsafe for this
#    repo: 15 test names are defined in two files each -- TestWebWithPrefix,
#    TestStateMerge, TestMatchFilterLabels and friends all exist in both
#    `test/with_api_v1/...` and `test/with_api_v2/...`, or in both `api/v1` and
#    `api/v2`. A full-suite run produced 319 result lines for only 315 unique
#    bare names, i.e. four silent merges. Every duplicate is cross-package, so
#    (package, name) is unique and resolves to exactly one file.


class AlertmanagerImageBase(Image):
    # Level 1. dependency() is a string AND this dockerfile carries the clone,
    # so DockerfileEnhancer rewrites the clone into the standard
    # REPO_URL/BASE_COMMIT form plus Image._HARDENING_BLOCK, and prepends the
    # syntax directive, TARGETARCH/proxy ARGs, ENV block, labels and cert
    # symlinks. None of that is hand-written here.
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        # Pinned to the version .promu.yml declares for this sha. Publishes
        # linux/amd64 and linux/arm64.
        return "golang:1.16"

    def image_tag(self) -> str:
        # Per-PR: the enhancer bakes `git checkout ${BASE_COMMIT}` into this
        # image, so a shared "base" tag would let a second PR of this repo
        # inherit the first PR's pinned tree.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

        # No apt layer: `golang:1.16` already ships git and ca-certificates
        # (verified in the built image), the repo vendors its dependencies, and
        # nothing under test needs a C toolchain.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class AlertmanagerImageDefault(Image):
    # Level 2 -- per-PR scripts. dependency() is an Image, so the enhancer
    # returns this dockerfile verbatim; everything needing REPO_URL/BASE_COMMIT
    # already happened in the base.
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
        return AlertmanagerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        check_git_changes = """#!/bin/bash
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

        run_tests = """#!/bin/bash
# Runs this PR's tests and prints `go test -v` output for parse_log.
# `set -e` is deliberately absent HERE (the three stage wrappers do use it): a
# failing test, and a package that will not compile until fix.patch lands, are
# both expected outcomes of one stage or another and must still reach the
# report below.
set -uo pipefail
cd /home/__REPO__

export CI=true
TEST_TIMEOUT="${AM_TEST_TIMEOUT:-1800}"

# ------------------------------------------------------------ 1. package scope
# Which packages to test comes from the directories this PR's own patches touch
# -- the pattern already used by prometheus_go1_16.py in this directory. Only
# .go files count: the patches also carry docs/*.md and config/testdata/*.yml,
# and neither names a Go package.
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null \\
  | grep '^diff --git' \\
  | sed 's|diff --git a/||;s| b/.*||' \\
  | grep '\\.go$' \\
  | xargs -I{} dirname {} \\
  | sort -u \\
  | sed 's|^|./|' \\
  | grep -v '^\\.$')

if [ -z "$PKGS" ]; then
  PKGS="./..."
  echo "AM RUNNER: the patches name no Go package; falling back to ./..."
fi
echo "AM RUNNER: packages under test: $(echo $PKGS | tr '\\n' ' ')"
echo "AM RUNNER: NOT RUN: every package this PR does not touch."
echo "AM RUNNER: NOT RUN: that deliberately excludes ./test/... , whose acceptance"
echo "AM RUNNER: NOT RUN: suites exec the built alertmanager/amtool binaries and"
echo "AM RUNNER: NOT RUN: contain at least one genuinely flaky test -- TestRetry in"
echo "AM RUNNER: NOT RUN: test/with_api_v2/acceptance failed 1 of 3 consecutive"
echo "AM RUNNER: NOT RUN: runs on an unchanged tree. A test that flips on its own"
echo "AM RUNNER: NOT RUN: would fabricate an f2p or trip the PASS->FAIL guard."

# ------------------------------------------------------------ 2. test->file map
# Go's test runner never reports which file a test came from, but parse_log
# needs that to build a "<file>::<test>" id. Emit the mapping from the source
# itself. Duplicated test names in this repo are always in DIFFERENT packages
# (15 such names at this sha), so (package directory, test name) resolves to
# exactly one file -- which is why the marker below carries the file path and
# parse_log keys on the pair.
echo '##### TESTMAP-BEGIN'
for pkg in $PKGS; do
  d="${pkg#./}"
  [ "$d" = "..." ] && d="."
  find "$d" -name '*_test.go' -maxdepth 1 2>/dev/null | while IFS= read -r f; do
    grep -oE '^func (Test[A-Za-z0-9_]+)\\(' "$f" 2>/dev/null \\
      | sed 's/^func //; s/($//; s/(//' \\
      | while IFS= read -r t; do echo "${f#./}|$t"; done
  done
done
echo '##### TESTMAP-END'

# ------------------------------------------------------------ 3. run
# One package at a time, each behind a marker. Go buffers per-package output
# when several packages run together, but that grouping is a property of the
# runner rather than a guarantee -- running them separately makes the package
# each result belongs to a fact in the log instead of an inference.
# No GOFLAGS/CGO_ENABLED overrides: vendor/ is present, so Go implies
# -mod=vendor and the run needs no network.
rc_any=0
echo '===== BEGIN TEST RESULTS ====='
for pkg in $PKGS; do
  echo "##### PKG: $pkg"
  timeout --kill-after=60 "$TEST_TIMEOUT" \\
    go test -v -count=1 -timeout 15m "$pkg" 2>&1
  rc=$?
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "##### RUNNER-TIMEOUT: $pkg"
    rc_any=$rc
  fi
  echo
done
echo '===== END TEST RESULTS ====='

# ------------------------------------------------------------ 4. verdict
# `go test` exits 1 for "tests failed" and 2 for "did not compile". Both are
# legitimate measurements for some stage of this PR -- stage 2 genuinely does
# not compile -- so neither is escalated. Only a timeout means the run measured
# nothing at all.
if [ "$rc_any" -ne 0 ]; then
  echo "AM RUNNER: INFRASTRUCTURE FAILURE: a package hit the ${TEST_TIMEOUT}s cap;"
  echo "AM RUNNER: the results above are not trustworthy"
  exit "$rc_any"
fi
exit 0
""".replace("__REPO__", repo)

        prepare = """#!/bin/bash
set -e
export CI=true
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git config core.autocrlf input
git config core.filemode false

git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the build cache at the base sha so the three stages do not each compile
# the dependency tree from scratch. `|| true` because a compile failure on one
# architecture must not abort the image build.
go build ./... || true

# The ./test/... acceptance suites exec these two binaries and panic without
# them ("Error accessing amtool command, try 'make build'"). This PR's package
# scope excludes those suites, but a PR that touches test/ would pull them in,
# and building here costs seconds. Both paths are listed in .gitignore
# (/alertmanager, /amtool), so they do NOT dirty the work tree -- verified.
go build -o alertmanager ./cmd/alertmanager || true
go build -o amtool ./cmd/amtool || true

# Rehearse this PR's own test selection once, in the same environment the
# stages use.
bash /home/run_tests.sh > /home/prepare-warmup.log 2>&1 || true
tail -20 /home/prepare-warmup.log || true

# The build cache lives outside the work tree and the two binaries are
# gitignored, so this reset leaves the warm-up intact while returning the tree
# to a clean base sha -- which is what lets test.patch/fix.patch apply cleanly.
git reset --hard
bash /home/check_git_changes.sh
""".replace("__REPO__", repo).replace("__SHA__", self.pr.base.sha)

        # `set -e` in the three wrappers is load-bearing: without it a failing
        # `git apply` does not stop the script, the stage measures the wrong
        # tree, and the PR's own tests are misclassified in a log that looks
        # perfectly healthy.
        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "run_tests.sh", run_tests),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
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


@Instance.register("prometheus", "alertmanager")
class Alertmanager(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AlertmanagerImageDefault(self.pr, self._config)

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

        ansi = re.compile(r"\x1B\[[0-?9;]*[mK]")
        clean = ansi.sub("", test_log)

        # 1. Rebuild the (package dir, test name) -> source file map that
        #    run_tests.sh emitted. Go never reports a test's file, so without
        #    this the best available id would be a bare test name -- which is
        #    unsafe in this repo: 15 names are defined twice (always in
        #    different packages), and a full-suite run collapses 319 result
        #    lines into 315 unique bare names.
        testmap: dict[tuple[str, str], str] = {}
        in_map = False
        for line in clean.splitlines():
            s = line.strip()
            if s == "##### TESTMAP-BEGIN":
                in_map = True
                continue
            if s == "##### TESTMAP-END":
                in_map = False
                continue
            if in_map and "|" in s:
                path, _, name = s.partition("|")
                path, name = path.strip(), name.strip()
                if path and name:
                    testmap[(path.rsplit("/", 1)[0] if "/" in path else ".", name)] = path

        # 2. Walk the run, tracking which package each result belongs to.
        #    "--- PASS: Name (0.00s)" at top level; subtests are the same shape
        #    but indented and named "Parent/child".
        pkg_re = re.compile(r"^##### PKG:\s*(\S+)\s*$")
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")

        cur_dir = ""
        for line in clean.splitlines():
            m = pkg_re.match(line)
            if m:
                pkg = m.group(1)
                cur_dir = pkg[2:] if pkg.startswith("./") else pkg
                if cur_dir in ("...", ""):
                    cur_dir = "."
                continue

            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)

            # A subtest's file is its parent's file; Go reports it as
            # "TestParent/sub_case".
            parent = name.split("/", 1)[0]
            src = testmap.get((cur_dir, parent))
            # Falling back to the package directory keeps the id unique even
            # when the map missed (e.g. a test defined in a file the map's
            # `-maxdepth 1` scan did not reach). It never invents a filename.
            test_id = f"{src}::{name}" if src else f"{cur_dir}::{name}"

            if status == "PASS":
                passed_tests.add(test_id)
            elif status == "FAIL":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        # Keep the buckets disjoint. Failure wins: crediting a test that was
        # ever seen failing as passed is the unsafe direction.
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
