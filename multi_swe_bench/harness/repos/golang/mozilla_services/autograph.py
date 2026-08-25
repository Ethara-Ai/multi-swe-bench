import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AutographImageBase(Image):
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
        # Pinned to 1.15 deliberately, rather than bumped to a still-supported
        # Debian base. `.circleci/config.yml` runs every job on
        # `golang:1.15-buster` and the repo's own Dockerfile on
        # `golang:1.15.14-buster`. The xpi suite asserts exact `crypto/x509`
        # error strings -- "certificate is not valid for any names, but wanted
        # to match ..." -- whose wording and CN-fallback behaviour changed after
        # 1.15, so a newer toolchain would turn passing assertions into failures
        # that have nothing to do with this PR.
        return "golang:1.15-buster"

    def image_tag(self) -> str:
        # Per-PR, not a shared `base`: DockerfileEnhancer injects
        # `git checkout ${BASE_COMMIT}` plus the history scrub into this image,
        # so a tag shared across PRs would stay pinned to whichever PR built it
        # first and would have every other PR's commit pruned from the object
        # store.
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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

# Debian buster is end-of-life: deb.debian.org and security.debian.org no longer
# publish a Release file for it, so the image's stock sources.list makes
# `apt-get update` fail outright ("does not have a Release file").
# archive.debian.org still serves buster, but its Release files are long past
# Valid-Until, which apt rejects by default -- hence the explicit opt-out.
#
# libltdl-dev is not optional. `signer/signer.go` imports
# github.com/ThalesIgnite/crypto11, which pulls in the cgo package
# github.com/miekg/pkcs11 and needs <ltdl.h> at compile time. Every package that
# imports `signer` -- including `signer/xpi`, the package this PR changes --
# fails to build without it, and the resulting build failure is indistinguishable
# from a genuine test regression in the parsed report.
#
# gpg is required at run time by `signer/gpg2`, which shells out to `gpg` via
# exec.Command. libncurses5 matches the runtime dependency the repo's own
# Dockerfile installs alongside libltdl-dev.
#
# The two probes on the last lines are the hard verification for this layer: apt
# can exit 0 having skipped a package, and a missing ltdl.h would otherwise only
# surface much later as a wall of compile errors during the test stages.
#
# DEBIAN_FRONTEND is deliberately NOT set here: DockerfileEnhancer.enhance()
# already exports it in the ENV block it injects ahead of this layer, and
# repeating it renders a duplicate ENV that reads as though the config did not
# know what the enhancer provides.
RUN set -eux; \\
    printf 'deb http://archive.debian.org/debian buster main\\n' > /etc/apt/sources.list; \\
    printf 'deb http://archive.debian.org/debian-security buster/updates main\\n' >> /etc/apt/sources.list; \\
    printf 'Acquire::Check-Valid-Until "false";\\n' > /etc/apt/apt.conf.d/99no-check-valid; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends \\
        libltdl-dev \\
        gpg \\
        libncurses5; \\
    rm -rf /var/lib/apt/lists/*; \\
    test -f /usr/include/ltdl.h; \\
    gpg --version > /dev/null

{code}

{self.clear_env}

"""


class AutographImageDefault(Image):
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
        return AutographImageBase(self.pr, self.config)

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export GOFLAGS=-mod=mod
export CGO_ENABLED=1

# Warm the module cache for the BASE module graph.
go mod download || true

# ... and for the graph the fix patch introduces. fix.patch bumps
# go.mozilla.org/pkcs7 from v0.0.0-20200128120323 to v0.0.0-20210730143726 in
# go.mod/go.sum, so without this the fix stage -- and only the fix stage -- would
# be the one that has to reach the network. Warming both graphs here keeps the
# three stages equivalent and lets the instance run with networking disabled.
#
# --check first: if the patch does not apply cleanly on its own, skip the warm
# rather than abort the build under `set -e`. The fix stage applies test.patch
# and fix.patch together and is the authority on whether they apply.
if git apply --check --whitespace=nowarn /home/fix.patch 2>/dev/null; then
  git apply --whitespace=nowarn /home/fix.patch
  go mod download || true
  git checkout -- .
  git clean -fdq
fi

# The warm above must leave the tree exactly as it found it: fix.patch adds two
# new .pem files, and an untracked leftover would make every later
# `check_git_changes.sh` fail for a reason unrelated to the stage being run.
bash /home/check_git_changes.sh

# `|| true` on the downloads is deliberate -- a partially resolved module graph
# is tolerable and the stages can still fetch. A broken toolchain is not, and
# must fail here rather than surface later as a wall of "test failures".
# Building the packages under test is what actually proves cgo, <ltdl.h> and the
# module cache are usable.
go build ./signer/... || {{ echo "prepare: signer packages failed to build" >&2; exit 1; }}

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
# The single definition of the test invocation. run.sh, test-run.sh and
# fix-run.sh differ only in which patches they apply and then all exec this
# file, so the command provably cannot drift between the three stages. A drift
# there would invent f2p/p2p transitions that never happened.
set -eo pipefail

export CI=true
# Go 1.15 still defaults to -mod=mod, but pinning it is explicit and keeps the
# behaviour identical if the base image is ever moved to 1.16+, where readonly
# became the default and the go.mod bump in fix.patch would be refused.
export GOFLAGS=-mod=mod
# Explicit, not incidental. `signer/signer.go` imports crypto11 -> miekg/pkcs11,
# which is a cgo package; with CGO_ENABLED=0 every package that imports `signer`
# -- signer/xpi included -- fails to build. Go defaults this to 1 only when a C
# toolchain is detected, so relying on the default makes the whole suite hostage
# to the base image shipping gcc.
export CGO_ENABLED=1

cd /home/{pr.repo}

# The package set, and why it is not `./...`:
#   ./database                      needs a live PostgreSQL on 127.0.0.1:5432;
#                                   every test there fails "connection refused"
#                                   and would be recorded as a real failure.
#   .            (root package)     shells out to `java` for the apk2 signing
#                                   path: 'exec: "java": executable file not
#                                   found in $PATH'.
#   ./signer/apk2                   same java dependency, plus apksigner, which
#                                   only exists in EOL buster-backports.
#   ./formats                       has no test files.
#   ./verifier/contentsignature     is a separate Go module, so `go test` from
#                                   this module cannot address it.
# What remains are the seven packages that pass cleanly at the base commit, and
# they include signer/xpi -- the package this PR changes.
PACKAGES="./signer ./signer/contentsignature/... ./signer/contentsignaturepki/... ./signer/genericrsa/... ./signer/gpg2/... ./signer/mar/... ./signer/xpi/..."

# -p 1 serialises the package test binaries. signer/xpi's RSA cache tests assert
# on elapsed time against a cache hit, and running packages concurrently on a
# constrained runner makes those timings flaky.
#
# tee so the harness still streams live output while report-build-failures.sh
# gets a file to re-read; PIPESTATUS keeps the stage's exit code the one
# `go test` returned rather than tee's.
set +e
go test -v -count=1 -p 1 -timeout 1200s $PACKAGES 2>&1 | tee /tmp/go-test.log
status=${{PIPESTATUS[0]}}
set -e

bash /home/report-build-failures.sh /tmp/go-test.log
exit "$status"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "report-build-failures.sh",
                """#!/bin/bash
# Usage: report-build-failures.sh <go-test-log>
#
# `go test` builds one test binary per package. A single `_test.go` that does not
# compile takes that whole binary down, and the only thing `go test` then prints
# for the package is
#
#     FAIL	github.com/mozilla-services/autograph/signer/xpi [build failed]
#
# with no `--- PASS:`/`--- FAIL:` lines at all. Every test in the package
# silently disappears from the parsed result instead of being recorded as
# failing.
#
# That is exactly the test-patch stage of this PR. The gold test patch calls
# `verifyCOSESignatures` and `VerifySignedFile` with a fourth argument
# (verificationTime) that only fix.patch adds to their signatures, so applying
# the test patch to the base commit yields six "too many arguments in call"
# errors and `signer/xpi [build failed]`. Left alone, test_patch_result would
# come back far smaller than run_result even though a test patch can only ever
# add tests, and the 47 silenced tests would classify as p2p instead of f2p.
#
# Re-attribute the compile failure to the tests it silenced: for every package
# reported `[build failed]`, emit one `--- FAIL:` line per top-level
# `func TestXxx` declared in that package's `_test.go` files. A test that cannot
# be built did not pass, so FAIL is the honest status, and it keeps the
# stage-over-stage counts comparable.
#
# Deliberately not `set -e`: `grep` exiting 1 on "no match" is the normal case
# here (no build failures at the run and fix stages) and must not abort.
set -uo pipefail

MODULE=github.com/mozilla-services/autograph
log="${1:?usage: report-build-failures.sh <go-test-log>}"

[ -f "$log" ] || exit 0

pkgs=$(grep -E '^FAIL[[:space:]]+[^[:space:]]+[[:space:]]+\\[build failed\\]' "$log" \\
       | awk '{print $2}' | sort -u)

for pkg in $pkgs; do
  # Import path -> directory, relative to the module root (this script runs with
  # the repo as cwd). Anything outside the module is not ours to map.
  case "$pkg" in
    "$MODULE") dir="." ;;
    "$MODULE"/*) dir=".${pkg#$MODULE}" ;;
    *) continue ;;
  esac
  [ -d "$dir" ] || continue

  # Only top-level `func TestXxx(` counts. Methods (`func (s *Suite) TestX`) run
  # as subtests of their runner, and TestMain is the package entry point, not a
  # test.
  names=$(grep -hoE '^func Test[A-Za-z0-9_]*\\(' "$dir"/*_test.go 2>/dev/null \\
          | sed -E 's/^func //; s/\\($//' | sort -u)

  emitted=0
  for name in $names; do
    if [ "$name" = "TestMain" ]; then
      continue
    fi
    # Never contradict a real result. parse_log flattens test names across
    # packages, and this repo genuinely reuses names -- TestSignData exists in
    # signer/xpi, signer/mar, signer/genericrsa and signer/gpg2; TestSignFile in
    # signer/xpi and signer/mar; TestNewFailure in signer/xpi,
    # signer/contentsignature and signer/contentsignaturepki. When a sibling
    # package has already reported one of those names, it keeps the status that
    # package observed rather than being overwritten by this package's failure.
    if grep -qE "^[[:space:]]*--- (PASS|FAIL|SKIP): ${name}[ /]" "$log"; then
      continue
    fi
    # Provenance banner, printed once, immediately before the first synthetic
    # line for this package. Without it the appended `--- FAIL:` lines sit after
    # the final package summary with no matching `=== RUN`, and anyone diffing
    # raw `go test` output against the parsed report would reasonably read them
    # as fabricated. The banner cannot be mistaken for a result: parse_log only
    # matches `--- PASS/FAIL/SKIP:`, so these lines are inert to the parser.
    if [ "$emitted" -eq 0 ]; then
      echo "=== SYNTHETIC: $pkg reported [build failed]; go test produced no per-test"
      echo "=== SYNTHETIC: results for it. report-build-failures.sh attributes the compile"
      echo "=== SYNTHETIC: failure to every top-level Test func declared in $dir."
      emitted=1
    fi
    echo "--- FAIL: $name (0.00s)"
  done

  if [ "$emitted" -eq 1 ]; then
    echo "=== SYNTHETIC: end of synthesized results for $pkg"
  fi
done

""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

# Baseline stage: no patch applied. Establishes which tests pass at base.sha
# before either patch exists.
cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

# Test-patch-only stage: proves the gold tests actually exercise the bug. For
# this PR the patch changes two call signatures that only fix.patch provides, so
# signer/xpi legitimately fails to compile here; report-build-failures.sh (called
# from run-tests.sh) records that as a failure of each test it silenced.
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

# Fix stage: both patches. Applied in one `git apply` so the two are staged
# atomically -- a partial application would leave a tree that is neither the
# test stage nor the fix stage.
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run-tests.sh

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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("mozilla-services", "autograph")
class Autograph(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AutographImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Only `--- STATUS:` lines are parsed. The package-level summary lines
        # (`ok  \tgithub.com/...\t77.865s` and `FAIL\tgithub.com/...\t[build
        # failed]`) are deliberately left alone: any pattern loose enough to
        # catch them also captures the import path as though it were a test name.
        # The tests a `[build failed]` package swallows are recovered upstream
        # instead -- report-build-failures.sh appends a synthetic `--- FAIL:`
        # line for each one, so they reach this parser as ordinary failures.
        #
        # The captured group is the bare test node id and nothing else: `(\S+)`
        # stops at the space before `(0.43s)`, so the elapsed time never becomes
        # part of the name. A name carrying its duration would differ between the
        # run, test and fix stages and the same test would be counted three
        # times over.
        #
        # Subtest suffixes are kept verbatim (`Parent/sub`). Truncating to the
        # parent would collapse this repo's table-driven cases -- the seven
        # `TestRecommendationNotIncludedInOtherSignerModes/N_"..."` subtests, for
        # instance -- into a single colliding name.
        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        # ANSI first: a colourised `--- \x1b[32mPASS\x1b[0m: TestX` would not
        # match at all, and a stray reset sequence inside a name would fork one
        # test into two.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for line in clean_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        # A parent test is reported FAIL when any subtest fails, and this repo
        # reuses test names across packages (TestSignData exists in four of the
        # seven packages under test), so one name can arrive with two different
        # statuses in a single log. Failure wins, which also keeps the three sets
        # disjoint.
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
