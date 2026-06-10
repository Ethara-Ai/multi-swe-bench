import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FliptImageBase(Image):
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
        # flipt's go.mod `go` directive ranges from 1.13 (PR #194, when the
        # module was still `github.com/markphelps/flipt`) up to 1.26.0 (PR
        # #5404, after the move to `go.flipt.io/flipt`). Go is backward
        # compatible, so the newest toolchain in the dataset builds every era;
        # GOTOOLCHAIN=auto lets newer go.mod files request a different
        # toolchain if needed.
        return "golang:1.26-bookworm"

    def image_tag(self) -> str:
        return "base"

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

ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=auto
RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

{self.clear_env}

"""


class FliptImageDefault(Image):
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
        return FliptImageBase(self.pr, self._config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

git config --global --add safe.directory '*'
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Pre-fetch module dependencies for every go.mod that actually ships in the
# checkout. flipt is a Go multi-module monorepo: the root module plus
# sibling modules under errors/, core/, build/, rpc/flipt/, rpc/v2/*,
# sdk/go/, sdk/go/v2/, and internal/cmd/protoc-gen-*. Early-era PRs (pre
# v1.10) only ship the root module, so this loop adapts to whichever
# go.mod files exist at the checked-out commit. `|| true` keeps a missing
# or transient module from aborting the whole image build.
while IFS= read -r mod; do
  case "$mod" in
    *"/testdata/"*|*"/node_modules/"*|*"/ui/"*|*"/_tools/"*|*"/examples/"*) continue ;;
  esac
  dir="$(dirname "$mod")"
  echo "=== go mod download in $dir ==="
  ( cd "$dir" && go mod download ) || true
done < <(find . -name go.mod -not -path '*/node_modules/*')

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared helpers for the flipt run/test/fix scripts.
#
# flipt-io/flipt is a Go multi-module monorepo. Module layout changes over
# the dataset's PR range (#194 -> #5404, spanning v0.11 -> v2.7):
#
#   * PR #194 era: a single root module at github.com/markphelps/flipt
#   * Modern era: root go.flipt.io/flipt plus sibling modules under
#     errors/, core/, build/, rpc/flipt/, rpc/v2/{environments,evaluation}/,
#     sdk/go/, sdk/go/v2/, internal/cmd/protoc-gen-{flipt-openapi,go-flipt-sdk}/
#
# Running `go test ./...` from the repo root would silently skip sibling
# modules in the modern era. Instead we collect the directories touched by
# the patches, walk each one up to its nearest go.mod, and run one
# `go test` per (module, package-list) group so package paths stay relative
# to that module's root. Same shape as the encore config.
#
# Non-Go trees (ui/, docs/, examples/, _tools/, testdata/) are filtered
# out -- they contain no `go test` targets exercisable in this harness.

EXCLUDES="--exclude=*.lock --exclude=*.png --exclude=*.ico --exclude=*.mp4 \
--exclude=*.svg --exclude=*.gif --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.webp --exclude=*.pdf --exclude=docs/* --exclude=ui/*"

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --3way $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --reject $EXCLUDES "$f" \\
    || true
}

# Walk up from $1 (a directory relative to the repo root) until a go.mod is
# found. Echoes the relative path of that module directory, or empty if
# none exists (file outside any module, e.g. docs/).
_module_dir_for() {
  local d="$1"
  while [ -n "$d" ] && [ "$d" != "." ]; do
    if [ -f "$d/go.mod" ]; then
      echo "$d"
      return 0
    fi
    d="$(dirname "$d")"
  done
  if [ -f "go.mod" ]; then
    echo "."
  fi
}

# Print "<module_dir>\\t<package_rel_to_module>" for every unique Go test
# directory touched by test.patch + fix.patch. Excludes testdata/ui/docs
# trees and any directories that don't exist on disk for the current
# checkout. Written to be safe under `set -eo pipefail`: a no-match grep /
# empty awk pipeline must not abort the script.
collect_module_packages() {
  local raw
  raw=$(
    {
      git apply --numstat --whitespace=nowarn /home/test.patch 2>/dev/null
      git apply --numstat --whitespace=nowarn /home/fix.patch 2>/dev/null
    } \\
      | awk -F'\\t' '{print $NF}' \\
      | grep -E '\\.go$' \\
      | grep -vE '(^|/)(testdata|ui|docs|examples|_tools)(/|$)' \\
      | sed -E 's#/[^/]+$##' \\
      | sort -u
  ) || true

  local d mod rel
  for d in $raw; do
    [ -n "$d" ] || continue
    [ -d "$d" ] || continue
    mod=$(_module_dir_for "$d")
    [ -n "$mod" ] || continue
    if [ "$mod" = "." ]; then
      rel="./$d"
    else
      rel="./${d#$mod/}"
    fi
    printf '%s\\t%s\\n' "$mod" "$rel"
  done | sort -u
}

run_go_tests() {
  local pairs current_mod="" pkgs=""

  # Modern era has go.work => workspace mode, which rejects -mod=mod
  # (set globally via ENV GOFLAGS in the dockerfile). Clear GOFLAGS so
  # `go test` runs with the workspace's default (-mod=readonly).
  if [ -f go.work ]; then
    export GOFLAGS=""
  fi

  pairs=$(collect_module_packages)
  if [ -z "$pairs" ]; then
    echo "No Go test packages touched by the patches; nothing to run."
    return 0
  fi

  echo "=== Touched (module, package) pairs ==="
  printf '%s\\n' "$pairs"
  echo "======================================="

  # Group consecutive lines by module (input is already sorted) and run one
  # `go test` per module so package paths stay relative to that go.mod.
  local rc=0
  while IFS=$'\\t' read -r mod rel; do
    if [ "$mod" != "$current_mod" ]; then
      if [ -n "$current_mod" ] && [ -n "$pkgs" ]; then
        echo "=== go test in $current_mod ==="
        ( cd "$current_mod" && go test -v -count=1 -timeout=1200s $pkgs ) || rc=$?
      fi
      current_mod="$mod"
      pkgs=""
    fi
    pkgs="$pkgs $rel"
  done <<< "$pairs"

  if [ -n "$current_mod" ] && [ -n "$pkgs" ]; then
    echo "=== go test in $current_mod ==="
    ( cd "$current_mod" && go test -v -count=1 -timeout=1200s $pkgs ) || rc=$?
  fi

  return $rc
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto

cd /home/{pr.repo}
source /home/common.sh

run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
apply_patch /home/fix.patch
run_go_tests

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


@Instance.register("flipt-io", "flipt")
class Flipt(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FliptImageDefault(self.pr, self._config)

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
        # `go test` is not colorized by default, but strip ANSI escapes
        # defensively in case the log was captured through a colorizing tee.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")
        # A package summary line ("ok   <import-path>", "FAIL <import-path>",
        # "?    <import-path>") closes the block of tests printed above it.
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        # Tests are buffered per package so the package import path can be
        # prepended -- this keeps names globally unique when several packages
        # are tested in one `go test` invocation.
        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            pass_match = re_pass.match(line)
            if pass_match:
                pending_pass.add(pass_match.group(1))
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                pending_fail.add(fail_match.group(1))
                continue

            skip_match = re_skip.match(line)
            if skip_match:
                pending_skip.add(skip_match.group(1))
                continue

            pkg_match = re_pkg.match(line)
            if pkg_match:
                flush(pkg_match.group(1))

        # Flush tests not followed by a summary line (e.g. truncated/timed-out
        # log) so they are still counted.
        flush("unknown")

        # Enforce TestResult disjointness invariants: a test reported as both
        # passed and failed (e.g. flaky retry) counts as failed.
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
