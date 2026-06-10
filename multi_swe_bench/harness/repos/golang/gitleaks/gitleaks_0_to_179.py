import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GitleaksLegacyImageBase(Image):
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
        # Pre-modules gitleaks: PRs #3..#169 ship either no manifest (PR #3,
        # stdlib + 1 missing dep), or `dep` with a committed vendor/ tree
        # (PR #40..#169). The newer toolchains drop GOPATH-mode niceties and
        # tighten `go vet` enough that the vendored code from 2017 stops
        # building. golang:1.16 still happily supports both GO111MODULE=off
        # with GOPATH and the lazy `go mod init` fallback used for PR #3.
        return "golang:1.16"

    def image_tag(self) -> str:
        return "base-legacy"

    def workdir(self) -> str:
        return "base-legacy"

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

        # Pre-modules gitleaks lives under GOPATH at
        # github.com/zricethezav/gitleaks (vendor/ uses that import path).
        # Symlink the cloned tree into $GOPATH/src so `go test` resolves the
        # in-repo imports without needing GO111MODULE.
        return f"""FROM {image_name}

{self.global_env}

ENV GOPATH=/go
ENV GO111MODULE=off
RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

RUN mkdir -p /go/src/github.com/zricethezav \\
    && ln -sfn /home/{self.pr.repo} /go/src/github.com/zricethezav/{self.pr.repo}

{self.clear_env}

"""


class GitleaksLegacyImageDefault(Image):
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
        return GitleaksLegacyImageBase(self.pr, self.config)

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

# Make sure the GOPATH symlink still resolves after the checkout switched
# branches; the ImageBase already created it but a clobbered tree is cheap
# to fix here.
mkdir -p /go/src/github.com/zricethezav
ln -sfn /home/{pr.repo} /go/src/github.com/zricethezav/{pr.repo}

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared helpers for the gitleaks pre-modules era (PR #3..#169).
#
# Three sub-layouts coexist in this range:
#   - PR #3 (only): no vendor/, no go.mod, no Gopkg.toml. A single external
#     dep (github.com/nbutton23/zxcvbn-go) is imported. We bootstrap a
#     minimal go.mod via `go mod init` + `go mod tidy` so the build can
#     resolve that import; the test still fails the build until test.patch
#     is applied, which is the expected baseline-broken state.
#   - PR #40..#169: Gopkg.toml + committed vendor/. Use GO111MODULE=off so
#     Go picks up vendor/ from the GOPATH layout.
# We chdir into $GOPATH/src/github.com/zricethezav/gitleaks (a symlink) so
# imports of the form "github.com/zricethezav/gitleaks/..." resolve.

REPO_SRC=/go/src/github.com/zricethezav/gitleaks
EXCLUDES="--exclude=*.lock --exclude=*.png --exclude=*.ico --exclude=*.mp4 \
--exclude=*.svg --exclude=*.gif --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.webp --exclude=*.pdf --exclude=docs/*"

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --3way $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --reject $EXCLUDES "$f" \\
    || true
}

# Decide which Go module mode this PR snapshot wants, and prepare it.
bootstrap_modules() {
  if [ -f Gopkg.toml ]; then
    # Vendor-era: GO111MODULE=off picks up vendor/ automatically.
    export GO111MODULE=off
  elif [ -f go.mod ]; then
    export GO111MODULE=on
    go mod download 2>/dev/null || true
  else
    # PR #3 fallback: synthesize a module so `go get` can resolve the
    # missing zxcvbn-go import. Must come BEFORE patches are applied so
    # the test.patch (which references RepoDesc/Options/etc.) doesn't get
    # rejected by an out-of-sync tidy run.
    export GO111MODULE=on
    [ -f go.mod ] || go mod init github.com/zricethezav/gitleaks 2>/dev/null || true
    go mod tidy 2>/dev/null || true
  fi
}

run_go_tests() {
  echo "=== Running go test -vet=off ./... in $PWD (GO111MODULE=$GO111MODULE) ==="
  # -vet=off bypasses the auto-vet step; pre-modules gitleaks fails modern
  # vet checks (printf-style and Println-newline warnings) even on commits
  # the maintainers shipped to master back in 2017-2018.
  go test -v -vet=off -count=1 -timeout=900s ./...
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

source /home/common.sh
cd "$REPO_SRC"

bootstrap_modules
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

source /home/common.sh
cd "$REPO_SRC"

bootstrap_modules
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

source /home/common.sh
cd "$REPO_SRC"

bootstrap_modules
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


@Instance.register("gitleaks", "gitleaks_0_to_179")
class Gitleaks0To179(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GitleaksLegacyImageDefault(self.pr, self._config)

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

        flush("unknown")

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
