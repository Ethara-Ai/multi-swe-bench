"""bazelbuild/bazelisk - Go 1.24 / go modules / single module.

Every value below was captured by running the toolchain in Docker at base commit
cfa90e99, not by reading manifests:

  go.mod          module github.com/bazelbuild/bazelisk
                  go 1.23.0
                  toolchain go1.24.2          <- drives the base image choice
  .github/        only dependabot.yml + config.yml; NO test workflow to copy
  go list ./...   9 packages, 4 of which carry tests (core, httputil,
                  httputil/progress, platforms, versions)

Four things discovered by running it, each of which shapes this file:

1. `golang:1.24-bookworm`, NOT 1.22 and NOT `latest`. go.mod declares
   `toolchain go1.24.2`, so anything older refuses to build:
   `golang:1.22-bookworm` was tried first and rejected the module outright.
   The 1.24 image ships go1.24.13, which satisfies >= 1.24.2 locally.

2. `GOTOOLCHAIN=local`. Without it, a Go older than the `toolchain` directive
   silently downloads go1.24.2 over the network mid-build. Pinning to `local`
   turns that into an immediate, legible error instead of a proxy-dependent
   download. Verified it still builds: "BUILD OK with GOTOOLCHAIN=local".

3. `-count=1`. prepare.sh warms the build+test cache into the image layer, so
   without it the three scored stages replay cached results - measured: a second
   `go test -v ./...` printed the same 32 top-level PASS lines plus 5 `(cached)`
   markers. The counts happen to survive the replay, but a stage that reports
   without executing is not a measurement. -count=1 forces a real run.

4. THE TEST STAGE LOSES THE `core` PACKAGE, BY DESIGN OF THE PR. The test patch
   adds 11 tests to core/core_test.go that call helpers the FIX patch
   introduces (isCompletionCommand, constructInstallerURL,
   extractCompletionScriptsFromZip, handleCompletionCommand,
   getBazelCompletionScript, platforms.DetermineBazelInstallerFilename). With
   the test patch alone the package does not compile:

       core/core_test.go:220:14: undefined: isCompletionCommand
       FAIL github.com/bazelbuild/bazelisk/core [build failed]

   `go test ./...` still runs every OTHER package, so the stage reports a
   partial suite rather than zero. Measured across the three stages:

       run    exit 0    51 results   (51 passed)
       test   exit 1    44 results   (44 passed, core absent - build failed)
       fix    exit 0    71 results   (71 passed)

   That yields f2p=0 and n2p=20. The 7 pre-existing `core` results are
   recovered as p2p by report.py's CBC path (run PASS -> test NONE -> fix PASS),
   and the 20 new results - 11 new top-level tests plus their 9 subtests -
   classify as n2p. Those n2p are GENUINE: the helpers under test do not exist
   at the base commit, and the baseline stage ran a full clean 51-result suite.

The suite is hermetic. Re-run under `--network none` after `go mod download`:
51 passed, 0 failed, exit 0. Nothing here reaches the network at test time, so
no stage depends on the proxy and none of it can flake on a slow release feed.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# -count=1 defeats the cache prepare.sh warms (see docstring #3). -timeout 600s
# is ~20x the measured 30s wall time, so it can only fire on a genuine hang.
# No `|| true` and no -skip: the suite is clean and hermetic, and swallowing a
# non-zero exit would hide exactly the build failure that defines stage 2.
GO_TEST = "go test -v -count=1 -timeout 600s ./..."


class BazeliskImageBase(Image):
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
        # go.mod says `toolchain go1.24.2`; this image ships go1.24.13. Pinned
        # rather than `golang:latest` so the instance stays reproducible after
        # the next Go release. Verified: golang:1.22-bookworm does NOT work.
        return "golang:1.24-bookworm"

    def image_tag(self) -> str:
        # Per-PR. DockerfileEnhancer injects a hardening block that detaches at
        # one ${BASE_COMMIT} and prunes every other object, so a shared tag would
        # let whichever PR built first pin the commit for all the others.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

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

        return f"""\
FROM {image_name}

{self.global_env}

# See docstring #2: without this, a toolchain mismatch turns into a silent
# network download of go1.24.2 instead of an error you can read.
ENV GOTOOLCHAIN=local
ENV GOFLAGS=-mod=mod
ENV CGO_ENABLED=0
ENV LC_ALL=C.UTF-8
ENV CI=true

# The golang image is buildpack-deps based and already carries git and the CA
# bundle; installing them explicitly costs one cached layer and removes the
# assumption. bazelisk is pure Go - CGO_ENABLED=0 above - so no C toolchain,
# no pkg-config, and no -dev headers are needed.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class BazeliskImageDefault(Image):
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
        return BazeliskImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
# Assert the reset actually produced a clean tree rather than assuming it did.
# A stray modified file would flow into all three graded stages and corrupt the
# comparison with nothing in the log to explain why.
bash /home/check_git_changes.sh

git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Pull the module graph into the image layer so the three scored stages never
# touch proxy.golang.org. Measured: 6 modules, a few seconds. `timeout 900`
# covers what `|| true` cannot - `|| true` handles a command that FAILS, but one
# that HANGS never returns and never reaches `||`, and Docker has no per-step
# timeout, so a stalled module fetch would block the build forever.
timeout 900 go mod download

# Warm the build+test cache. -count=1 in the scored stages defeats the *result*
# cache (docstring #3) but the compiled package objects still carry over, which
# is what makes the graded runs fast.
if timeout 1800 {go_test} > /tmp/warm.log 2>&1; then
  echo "warm-up: OK" > /home/.warm_status
else
  echo "warm-up: INCOMPLETE (exit $?)" > /home/.warm_status
  tail -25 /tmp/warm.log || true
fi
cat /home/.warm_status

# Hard gate. The warm-up can "succeed" while leaving the cache unusable, and
# that surfaces three stages later as an unexplained 0/0/0 rather than as a
# build error. Prove the baseline suite actually RUNS before sealing the image -
# a missing image is honest, a silently hollow one is not.
{go_test} > /tmp/baseline.log 2>&1
grep -qE '^ok ' /tmp/baseline.log
echo "baseline suite OK: $(grep -cE '^[[:space:]]*--- PASS: ' /tmp/baseline.log) results"

# go build artifacts live outside the worktree, so nothing above dirties it.
git checkout -- . || true
""".format(pr=self.pr, go_test=GO_TEST),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{go_test}
""".format(pr=self.pr, go_test=GO_TEST),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{go_test}
""".format(pr=self.pr, go_test=GO_TEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
{go_test}
""".format(pr=self.pr, go_test=GO_TEST),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Generated from files() rather than hard-coded, so a file added there can
        # never be written into the build context yet left uncopied - which would
        # surface at build time as `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/{f.name}\n" for f in self.files())

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

"""


def parse_go_test_log(test_log: str) -> TestResult:
    """Parse `go test -v ./...` output.

    Captured verbatim from the container at base commit cfa90e99:

        === RUN   TestBuildURLFromFormat
        --- PASS: TestBuildURLFromFormat (0.00s)
        === RUN   TestFormatMb
        === RUN   TestFormatMb/48_MB
        --- PASS: TestFormatMb (0.00s)
            --- PASS: TestFormatMb/48_MB (0.00s)
        PASS
        ok  	github.com/bazelbuild/bazelisk/core	0.011s
        ?   	github.com/bazelbuild/bazelisk/ws	[no test files]

    Two properties of this output drive the implementation:

    1. SUBTESTS ARE INDENTED AND KEPT. Go reports `t.Run` subtests as
       `    --- PASS: Parent/Sub (0.00s)` beneath the parent's own result line.
       Both are recorded under their full names. The subtest name is unique
       (Go disambiguates collisions itself - the real suite contains both
       `TestFormatMb/48_MB` and `TestFormatMb/48_MB#01`) and stable across the
       three stages, which is what makes the run/test/fix comparison meaningful.
       Keeping them raises the signal from 32 to 51 baseline results.

    2. THE PACKAGE-SUMMARY LINES ARE DELIBERATELY NOT COUNTED. `PASS`, `FAIL`,
       `ok <tab>pkg<tab>0.011s` and `?   <tab>pkg<tab>[no test files]` are
       per-package summaries, not tests. A broad `FAIL` pattern would match the
       bare `FAIL` line and the `FAIL<tab>github.com/...<tab>[build failed]`
       line, injecting phantom failing tests - which is exactly what stage 2
       emits, so the corruption would land on the one stage that matters most.
       Only `--- PASS/FAIL/SKIP:` lines are read.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Leading whitespace is significant only as indentation for subtests; it is
    # stripped before matching so parent and subtest lines parse identically.
    result_re = re.compile(r"^---\s+(?P<status>PASS|FAIL|SKIP):\s+(?P<name>\S+)")

    ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    for raw_line in test_log.split("\n"):
        line = ansi_escape.sub("", raw_line).strip()
        if not line:
            continue

        match = result_re.match(line)
        if not match:
            continue

        name = match.group("name").strip()
        status = match.group("status")

        # A failure is sticky: once a name has failed in this log it cannot be
        # demoted back to passed/skipped by a later line.
        if status == "FAIL":
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif status == "PASS":
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)
        else:  # SKIP
            if name not in failed_tests and name not in passed_tests:
                skipped_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("bazelbuild", "bazelisk")
class Bazelisk(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BazeliskImageDefault(self.pr, self._config)

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
        return parse_go_test_log(test_log)