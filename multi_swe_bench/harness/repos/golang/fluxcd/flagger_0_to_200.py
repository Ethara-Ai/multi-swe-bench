from __future__ import annotations

"""fluxcd/flagger — era 1 registry config (PRs 1..200, number_interval='flagger_0_to_200').

This era predates Go modules: every base commit in the range ships `Gopkg.toml`
(dep) plus a committed `vendor/` tree and **no** `go.mod`. Builds therefore run
in classic GOPATH mode, which requires the working tree to live at
`$GOPATH/src/<import-path>`.

The import path is NOT constant across the era -- flagger was renamed while it
was still on dep:

    PRs   1..90   ->  github.com/stefanprodan/flagger
    PRs 127..200  ->  github.com/weaveworks/flagger

(and later, in era 2, again to github.com/fluxcd/flagger). A hardcoded GOPATH
directory would break 6 of the 16 records, so `prepare.sh` detects the import
path from the checked-out source and symlinks accordingly.

- Base image: golang:1.12 (contemporaneous with the dep era; GO111MODULE=off).
  Verified: all 16 base shas `go build ./pkg/...` clean.
- Tests: go test -v -count=1 ./pkg/... (era-1 base commits ship no tests; the
  test patch introduces pkg/controller/*_test.go)
- Parse: standard Go test output (--- PASS/FAIL/SKIP: TestName)
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FlaggerEra1ImageBase(Image):
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
        # Pinned to the toolchain contemporaneous with flagger's dep/vendor era.
        # Newer toolchains reject this era's dependency graph outright
        # (k8s.io/apiextensions-apiserver ...+incompatible), and golang:1.12 is
        # the newest image that builds all 16 base shas in GOPATH mode.
        return "golang:1.12"

    def image_tag(self) -> str:
        return "base-go112"

    def workdir(self) -> str:
        return "base-go112"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + `git gc --prune` HERE, pruning the shared base to a single
        # PR's base.sha and breaking every other PR in the era with
        # "reference is not a tree". The base keeps full history; the strict
        # anti-reward-hack hardening runs per-PR (see FlaggerEra1ImageDefault).
        #
        # No apt-get: golang:1.12 is Debian buster, whose repos are archived, so
        # `apt-get update` fails. Everything this era needs is already in the
        # image -- git 2.20.1, gcc 8.3.0 and ca-certificates -- and the era's
        # dependencies are vendored, so no network fetch happens at build time.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GO111MODULE=off

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class FlaggerEra1ImageDefault(Image):
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
        return FlaggerEra1ImageBase(self.pr, self._config)

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
                "gopath.sh",
                """#!/bin/bash
# Resolve this commit's Go import path and expose the GOPATH work dir.
#
# flagger was renamed mid-era (github.com/stefanprodan/flagger ->
# github.com/weaveworks/flagger), and GOPATH mode requires the tree to sit at
# $GOPATH/src/<import-path>, so the owner is detected from the source rather
# than hardcoded. `gopath_owner` is written by prepare.sh at image build time.

GOPATH_ROOT=/go/src/github.com

detect_owner() {
  local owner
  owner=$(grep -rhoE 'github\\.com/[a-z]+/flagger' cmd/flagger/main.go 2>/dev/null | head -1 | cut -d/ -f2)
  if [ -z "$owner" ]; then
    owner=$(grep -rhoE 'github\\.com/[a-z]+/flagger' --include='*.go' pkg cmd 2>/dev/null | head -1 | cut -d/ -f2)
  fi
  echo "$owner"
}

flagger_dir() {
  local owner
  owner=$(cat /home/gopath_owner 2>/dev/null)
  if [ -z "$owner" ]; then
    owner=$(cd /home/flagger && detect_owner)
  fi
  echo "$GOPATH_ROOT/$owner/flagger"
}
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

source /home/gopath.sh

git config --global --add safe.directory '*'
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# GOPATH layout: link the checkout in at its own import path. Persisted so the
# run/test/fix scripts resolve the same directory without re-detecting.
OWNER=$(detect_owner)
if [ -z "$OWNER" ]; then
  echo "prepare.sh: could not detect flagger import path" >&2
  exit 1
fi
echo "$OWNER" > /home/gopath_owner
echo "prepare.sh: import path = github.com/$OWNER/flagger"

mkdir -p "$GOPATH_ROOT/$OWNER"
ln -sfn /home/{repo} "$GOPATH_ROOT/$OWNER/flagger"

# Dependencies are vendored in-tree for this era; no fetch required. Warm the
# build cache so the test stages spend their time running tests, not compiling.
cd "$GOPATH_ROOT/$OWNER/flagger"
go build ./pkg/... 2>&1 | tail -5 || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared patch-apply helper for the flagger era-1 run scripts.
#
# `git apply` is atomic: one unappliable hunk aborts the entire patch and the
# stage silently reports 0 tests. flagger's patches carry binary blobs under
# docs/ (PNG/JPG diagrams and packaged .tgz charts) which cannot affect
# `go test ./pkg/...`, so they are excluded rather than risked.

EXCLUDES="--exclude=docs/* --exclude=*.png --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.gif --exclude=*.svg --exclude=*.ico --exclude=*.tgz \
--exclude=*.pdf --exclude=*.lock"

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn $EXCLUDES "$f"
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

source /home/gopath.sh
cd "$(flagger_dir)"
go test -v -count=1 ./pkg/... 2>&1
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

source /home/gopath.sh
source /home/common.sh
cd "$(flagger_dir)"
apply_patch /home/test.patch
go test -v -count=1 ./pkg/... 2>&1
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

source /home/gopath.sh
source /home/common.sh
cd "$(flagger_dir)"
apply_patch /home/test.patch
apply_patch /home/fix.patch
go test -v -count=1 ./pkg/... 2>&1
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable. It touches only git state, so the GOPATH symlink
        # created by prepare.sh survives.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("fluxcd", "flagger_0_to_200")
class Flagger0To200(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FlaggerEra1ImageDefault(self.pr, self._config)

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Registered so delivered records (which carry the dash-joined number_interval)
# resolve to this class (PIPELINE §11/§11b). The era key "flagger_0_to_200" above
# still routes the build-time dataset, whose number_interval is the era tag.
_BUNDLE_NIS_FLAGGER_ERA1 = [
    "1-4-6",  # pr-1 (3 PRs)
    "15-18",  # pr-15 (2 PRs)
    "20-21-24-25",  # pr-20 (4 PRs)
    "26-28-29-31",  # pr-26 (4 PRs)
    "33-35",  # pr-33 (2 PRs)
    "39-40-41-43-44-46-47",  # pr-39 (7 PRs)
    "51-53-54-55-57",  # pr-51 (5 PRs)
    "66-68-70-71-72-73-74-78-80-82-83-84",  # pr-66 (12 PRs)
    "88-91-93-94",  # pr-88 (4 PRs)
    "90-98-99-105-107-108-109-112-113-118-119-121-122-123-124",  # pr-90 (15 PRs)
    "127-130-134-136-139-141-146-147-148-149-150",  # pr-127 (11 PRs)
    "151-153-154-156-158-159",  # pr-151 (6 PRs)
    "160-162-167-168-170-173",  # pr-160 (6 PRs)
    "176-178",  # pr-176 (2 PRs)
    "179-180-181-182-183-185-187",  # pr-179 (7 PRs)
    "200-202-203",  # pr-200 (3 PRs)
]

for _ni in _BUNDLE_NIS_FLAGGER_ERA1:
    Instance.register("fluxcd", _ni)(Flagger0To200)
