import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Single-registration config for the dolthub/dolt `_lht_final` dataset
# (136 release-delta bundles, #8186..#11002, dolt release lines
# v1.42.13 .. v2.0.3). The dataset has NO `number_interval` set, so the
# harness routes every record to "dolthub/dolt" (instance.py:create). A
# multi-file ranged split would make every record unroutable, so a single
# registration is required -- the same constraint as
# hashicorp/terraform-provider-azurerm and camel-ai/camel.
#
# No per-era branching is needed either. dolt is a single Go module rooted
# at the repo's `go/` subdirectory (`module github.com/dolthub/dolt/go`),
# and the ONLY thing that varies across the v1.42..v2.0 span is the go.mod
# `go` directive (1.22.2 -> 1.26.2, Docker-verified at both extremes).
# `GOTOOLCHAIN=auto` makes the toolchain absorb that difference: the
# bundled Go 1.25 builds every directive <= 1.25 directly, and for the
# newer pins (e.g. `go 1.26.2`) it auto-fetches that exact toolchain. So
# one image + one command set is correct for every era (uniform config,
# like toeverything/AFFiNE).
#
# Unlike terraform-provider-azurerm, dolt does NOT vendor its dependencies
# (no `go/vendor/` at any base commit), so there is NO `-mod=vendor`: it
# is a normal module-download build (deps fetched + cached into the image
# layer by prepare.sh). All `go test` runs `cd /home/dolt/go` first because
# the module root is the `go/` subdir; patch paths are repo-root-relative
# (`go/...`), so `git apply` runs from `/home/dolt`.
#
# Docker-verified on golang:1.25-bookworm (clone from github inside the
# container, GOTOOLCHAIN=auto, native linux/arm64; golang official images
# are multi-arch so linux/amd64 is equivalent):
#   * Oldest PR 8246 base (v1.42.13..14, 9e7fa221, go.mod `go 1.22.2`):
#       built directly by the bundled Go 1.25 toolchain; package `ok`.
#   * Newest PR 10953 base (v2.0.2..v2.0.3, 120f1ad0, go.mod `go 1.26.2`):
#       GOTOOLCHAIN=auto auto-downloaded go1.26.2 (`go version` ->
#       go1.26.2); package `ok`.
#   * Representative PR 9678 (v1.58.5..6, base 4b02944e): real target
#       package `./cmd/dolt/commands/` built+ran in ~65s with standard
#       `go test -v` output (`--- PASS: <name>` incl. subtests); atomic
#       `git apply` of test.patch+fix.patch (99 files) applied cleanly;
#       post-patch `go test -v ./libraries/events/` -> all PASS.
#   * Binary-fixture PR 8305 (base d36ad30b): the 10 dataset patches that
#       carry `Binary files ... differ` blocks (bats/testdata noms
#       objects, none are GIT-literal binary patches, none needed to
#       compile/run Go unit tests) are stripped by strip_binary.sh so the
#       remaining text hunks apply atomically -- verified `git apply
#       --check` passes after stripping (89/93 blocks kept).
# parse_log is the proven hashicorp/Go matcher (see nomad.py /
# terraform_provider_azurerm.py): standard `--- PASS|FAIL|SKIP: <name>`
# lines, validated against the captured output above.
# ---------------------------------------------------------------------------


class DoltImageBase(Image):
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
        # Newest stable Go major. With GOTOOLCHAIN=auto this builds every
        # go.mod directive in the dataset (1.22.2 .. 1.26.x) directly and
        # auto-fetches the exact toolchain only for the newer pins.
        return "golang:1.25-bookworm"

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

ENV GOTOOLCHAIN=auto

# dolt depends on github.com/dolthub/go-icu-regex, a CGO binding that
# #include <unicode/uregex.h>. The bare golang image has no ICU dev
# headers, so every SQL-engine package (sqle/*, enginetest, statspro,
# merge, doltdb, prolly, ...) -- ~78% of this dataset -- fails to compile
# with "fatal error: unicode/uregex.h: No such file or directory". dolt's
# own CI never hits this because GitHub's Linux runners ship libicu-dev
# (its setup-go-toolchain action only sets ICU flags on macOS/Windows;
# "On Linux, just sets up Go"). Docker-verified: golang:1.25-bookworm +
# libicu-dev/pkg-config builds go-icu-regex and ./libraries/doltcore/
# sqle/statspro cleanly. libicu-dev is a single arch-agnostic apt name
# available on both amd64 and arm64 in bookworm (multi-arch safe).
RUN apt-get update && apt-get install -y --no-install-recommends \\
        libicu-dev pkg-config \\
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class DoltImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    def _get_test_packages(self) -> str:
        """Go package patterns for the test files the PR's test.patch
        targets, relative to the `go/` module root.

        dolt's module root is the repo's `go/` subdir, so test paths look
        like `go/cmd/dolt/commands/init_test.go`; this strips the `go/`
        prefix and reduces each to its package directory
        (`./cmd/dolt/commands/...`). Reads post-image paths from `+++ b/...`
        headers, skips deletions (`+++ /dev/null`), non-`_test.go` files,
        and non-`go/` paths (e.g. bats/integration-tests fixtures the Go
        suite never runs). Falls back to `./...` if none are found.
        """
        packages = set()
        for match in re.finditer(r"^\+\+\+ b/(.+)$", self.pr.test_patch, re.M):
            fpath = match.group(1).strip()
            # strip a trailing tab+timestamp some diff tools append
            fpath = fpath.split("\t")[0].strip()
            if not fpath.endswith("_test.go"):
                continue
            if not fpath.startswith("go/"):
                continue
            rel = fpath[len("go/"):]
            parts = rel.rsplit("/", 1)
            pkg_dir = parts[0] if len(parts) > 1 else "."
            if pkg_dir == ".":
                # a *_test.go directly in the module root (none exist in
                # dolt, but stay conservative rather than emit ".././...")
                packages.add("./...")
            else:
                packages.add(f"./{pkg_dir}/...")
        if not packages:
            return "./..."
        return " ".join(sorted(packages))

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return DoltImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        # MUST be `pr-<number>` with the number immediately after `pr-`:
        # gen_report.collect_report_tasks parses it as
        # int(dir.name[3:].split('-')[0]). PR numbers are globally unique
        # across eras, so this is collision-free with no era prefix.
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_packages = self._get_test_packages()
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
            # Strip plain `Binary files ... differ` diff blocks (git diff
            # without --binary -> no content) so the remaining text hunks
            # apply atomically. dolt carries such blocks only for
            # bats/testdata noms-object fixtures the Go unit tests don't
            # need; this dataset has ZERO GIT-literal binary patches
            # (which `git apply` *can* apply), so nothing appliable is
            # lost. Prints the cleaned patch to stdout.
            File(
                ".",
                "strip_binary.sh",
                """#!/bin/bash
set -eo pipefail
awk '
/^diff --git /{ if (blk!="") { if (!drop) printf "%s",blk } blk=""; drop=0 }
/^Binary files .* differ$/{ drop=1 }
{ blk=blk $0 ORS }
END{ if (blk!="" && !drop) printf "%s",blk }
' "$1"

""",
            ),
            # Warm-up: pin the base commit and pre-build the target test
            # packages so the per-era Go toolchain (auto-fetched for the
            # newer go.mod pins), the module cache and the build cache live
            # in the image layer. `|| true` -- a target dir may not exist
            # yet at base (added by the patch); that is fine for baseline.
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

cd /home/{repo}/go
go test -vet=off -count=1 {test_packages} || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha, test_packages=test_packages),
            ),
            # Baseline (no patch). Only test dirs that already exist at the
            # base commit -- a brand-new package added by the patch is
            # absent here, so `go test` on a missing path would abort the
            # whole invocation; its tests are correctly NONE at base.
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}/go
EXIST=""
for p in {test_packages}; do
  d="${{p%/...}}"
  if [ -d "$d" ]; then EXIST="$EXIST $p"; fi
done
if [ -z "$EXIST" ]; then
  echo "dolt-runner: no target packages at base (n2p baseline)"
  exit 0
fi
go test -vet=off -count=1 -v -timeout 60m $EXIST

""".format(repo=self.pr.repo, test_packages=test_packages),
            ),
            # test.patch only. Strip unappliable binary blocks first, then
            # apply from the repo root (patch paths are `go/...`).
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /home/test.clean.patch
git apply --whitespace=nowarn /home/test.clean.patch
cd /home/{repo}/go
go test -vet=off -count=1 -v -timeout 60m {test_packages}

""".format(repo=self.pr.repo, test_packages=test_packages),
            ),
            # test.patch + fix.patch, applied atomically (single git apply
            # invocation) after stripping binary blocks from each.
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /home/test.clean.patch
bash /home/strip_binary.sh /home/fix.patch > /home/fix.clean.patch
git apply --whitespace=nowarn /home/test.clean.patch /home/fix.clean.patch
cd /home/{repo}/go
go test -vet=off -count=1 -v -timeout 60m {test_packages}

""".format(repo=self.pr.repo, test_packages=test_packages),
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


@Instance.register("dolthub", "dolt")
class Dolt(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DoltImageDefault(self.pr, self._config)

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

        # Go test does not colorize when stdout is not a TTY (the harness
        # captures to a pipe), but strip ANSI defensively so the anchored
        # regexes below can never be defeated by escape sequences.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in failed_tests:
                        continue
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    passed_tests.add(base_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        passed_tests.remove(base_name)
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    failed_tests.add(base_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        continue
                    if base_name in failed_tests:
                        continue
                    skipped_tests.add(base_name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
