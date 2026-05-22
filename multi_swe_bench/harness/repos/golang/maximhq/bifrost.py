import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Single-registration config for the maximhq/bifrost `_lht_final` dataset
# (81 release-delta bundles, #27..#3154, bifrost `transports/v1.1.x..v1.5.x`
# release lines). The dataset has NO `number_interval` set, so the harness
# routes every record to "maximhq/bifrost" (instance.py:create). A multi-file
# ranged split would make every record unroutable, so a single registration
# is required -- the same constraint as dolthub/dolt, 99designs/gqlgen,
# hashicorp/terraform-provider-azurerm and camel-ai/camel.
#
# bifrost is a Go MULTI-MODULE monorepo: there is no root go.mod; instead each
# top-level component is its own module (`core/`, `transports/`, `framework/`,
# `cli/`, one per `plugins/<name>/`, one per `tests/<name>/`). The module path
# maps cleanly to the directory: `github.com/maximhq/bifrost/<X>` lives in
# `<X>/`. The module LAYOUT evolves across the #27..#3154 span (Docker-verified
# by `find -name go.mod` at three base commits):
#   * Oldest era (~#27): only THREE modules -- core/, plugins/, transports/
#     (plugins is a single module, not yet split per-plugin).
#   * Modern era (#3154): ~20 modules -- core, framework, cli, transports,
#     plugins/* split into one module each, tests/* split into one module each.
# Because the layout is discovered dynamically at runtime (group_pkgs.sh walks
# up from each test file to its nearest go.mod), the SAME script set is correct
# for every era -- no per-era config files are needed (uniform config, like
# dolthub/dolt and toeverything/AFFiNE).
#
# CROSS-MODULE FIX VISIBILITY (the key wrinkle). A single PR bundle commonly
# changes source in one module (e.g. core/) while its test.patch adds/edits
# `_test.go` files in a DIFFERENT module (e.g. tests/core-providers/,
# plugins/semanticcache/, transports/...). The sibling modules depend on
# PUBLISHED bifrost versions via the module proxy -- e.g. `require
# github.com/maximhq/bifrost/core v1.1.8`, with the local `replace ... =>
# ../../core` directive COMMENTED OUT in the committed go.mod (Docker-verified
# at #157). Left as-is, a fix.patch to local core/ source would be invisible to
# a tests/core-providers test and fix-run would equal test-run. So before each
# test run inject_replace.sh rewrites every `github.com/maximhq/bifrost/*`
# requirement in the target module's go.mod to a local `replace` pointing at
# the in-tree sibling dir, then `go mod tidy` -- making every test build
# against the patched local checkout. Docker-verified: #157 tests/core-providers
# with core replaced => /home/bifrost/core builds + runs.
#
# Many bifrost suites are LIVE-API integration tests (tests/core-providers/*,
# plugins/maxim, core/tests/vertex_test.go, ...) that call OpenAI/Anthropic/
# Bedrock/Vertex/Maxim and need real keys (os.Getenv("OPENAI_API_KEY") ...).
# Without keys they compile and run but FAIL ("no keys found that support
# model: ...") -- this is inherent to the dataset (its f2p/p2p/s2p sets are all
# empty; the harness re-derives them from real run output). The config still
# runs them and parse_log captures the standard go-test FAIL lines; the
# meaningful, key-free unit suites (config-merge, schemas, retries, hashing,
# core/internal/mcptests with mock MCP servers, framework/*, ...) PASS and are
# captured as `--- PASS:`.
#
# No system packages required: every touched module compiled on the bare
# golang:1.25-bookworm image (pure-Go deps incl. aws-sdk-go-v2; no CGO). Multi-
# arch safe (golang official images are multi-arch; verified native linux/arm64,
# amd64 equivalent).
#
# Docker-verified on golang:1.25-bookworm + GOTOOLCHAIN=auto:
#   * #768 base 3d960239 (transports/v1.3.18..19, core/go.mod `go 1.24.0`):
#       test.patch core/bifrost_test.go -> module=core, pkg=. ; replace inject
#       + tidy + `go test .` ran TestExecuteRequestWithRetries_* with full
#       `--- PASS:` incl. subtests.
#   * #3154 base 833f312a (transports/v1.5.1..2, newest): 30 _test.go files
#       across core/internal/mcptests, core/mcp, plugins/logging, framework,
#       transports; group_pkgs.sh fanned them to their modules and emitted
#       standard `--- PASS:` (TestLoadConfig_*, TestSchema*, ...) per module.
#   * #157 base e8af2e36 (tests/core-providers, live-API): replace core =>
#       local built+ran; testify FAILs ("no keys found...") captured in
#       standard `--- FAIL: TestAnthropic/SimpleChat` form (no-key dataset
#       reality).
#   * #29 base f9147ae6 (oldest 3-module era, core/tests/vertex_test.go) and
#       #31 base d1051297 (plugins/maxim, single plugins module): both compiled
#       + ran via the same script set (FAIL on missing keys, not build errors),
#       confirming the dynamic grouping works at the oldest layout too.
# parse_log is the proven hashicorp/Go matcher (see nomad.py / dolt.py /
# gqlgen.py): standard `--- PASS|FAIL|SKIP: <name>` lines, validated against the
# captured output above.
# ---------------------------------------------------------------------------


class BifrostImageBase(Image):
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
        # go.mod `go` directive in the dataset (1.24.x .. 1.25) directly and
        # auto-fetches the exact toolchain for any newer pin a later v1.5.x
        # bumps to.
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
# Module graph is rewritten at run time (inject_replace.sh adds local
# `replace` directives + `go mod tidy`), so the build must be allowed to
# update go.mod/go.sum.
ENV GOFLAGS=-mod=mod

WORKDIR /home/

{code}

{self.clear_env}

"""


class BifrostImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    def _get_test_files(self) -> str:
        """Repo-root-relative `_test.go` paths added/modified by this PR's
        test.patch, as a single space-separated string.

        The bash scripts iterate this list and group entries by their nearest
        ancestor go.mod (group_pkgs.sh) so each touched test package is run
        from the root of the module that actually owns it -- which is what
        bifrost's multi-module, layout-varies-by-era monorepo requires.

        Skips non-`_test.go` files (templates, .go support files, .json, ...)
        and `+++ /dev/null` deletions. Returns "" when no _test.go is touched;
        the run scripts then no-op (6/81 bundles only edit test *helper* .go
        files and have nothing test-gated to run -- consistent with the
        dataset's empty f2p sets).
        """
        test_files = set()
        for match in re.finditer(r"^\+\+\+ b/(.+)$", self.pr.test_patch, re.M):
            fpath = match.group(1).strip()
            # strip a trailing tab+timestamp some diff tools append
            fpath = fpath.split("\t")[0].strip()
            if not fpath.endswith("_test.go"):
                continue
            test_files.add(fpath)
        return " ".join(sorted(test_files))

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return BifrostImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        # MUST be `pr-<number>` with the number immediately after `pr-`:
        # gen_report.collect_report_tasks parses it as
        # int(dir.name[3:].split('-')[0]). PR numbers are globally unique.
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = self._get_test_files()
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
            # Group the PR's test files by the directory of their nearest
            # ancestor go.mod. Each output line:  "<mod_root> <pkg>"
            #   mod_root = absolute path of the owning module (so we can `cd`)
            #   pkg      = "." (test file sits in the module root dir) or
            #              "./<rel>" (test file in a subpackage of the module)
            # Deduplicated so each (mod_root, pkg) pair appears at most once.
            # Files with no ancestor go.mod are skipped (shouldn't happen --
            # every bifrost source dir lives under some module).
            #
            # NOTE the `dir == found` branch: unlike gqlgen (whose touched test
            # files always sit in SUBdirectories of the module root), bifrost
            # routinely has `_test.go` directly in a module root
            # (core/bifrost_test.go, plugins/maxim/plugin_test.go, ...). Without
            # this branch the rel-strip leaves rel="core" and `go test ./core`
            # from inside the core module fails "[setup failed]" -- the bug this
            # branch fixes (Docker-verified at #768).
            File(
                ".",
                "group_pkgs.sh",
                """#!/bin/bash
set -eo pipefail

# Args: $1 = absolute repo root, $2... = repo-root-relative test files
ROOT="$1"
shift

cd "$ROOT"
declare -A SEEN

for f in "$@"; do
  [ -f "$f" ] || continue
  dir="$(dirname "$f")"
  cur="$dir"
  found=""
  while :; do
    if [ -f "$cur/go.mod" ]; then
      found="$cur"
      break
    fi
    [ "$cur" = "." ] && break
    [ "$cur" = "/" ] && break
    cur="$(dirname "$cur")"
  done
  [ -z "$found" ] && continue
  if [ "$found" = "." ]; then
    mod_root="$ROOT"
    rel="$dir"
  elif [ "$dir" = "$found" ]; then
    mod_root="$ROOT/$found"
    rel="."
  else
    mod_root="$ROOT/$found"
    rel="${dir#$found/}"
  fi
  [ -z "$rel" ] && rel="."
  if [ "$rel" = "." ]; then pkg="."; else pkg="./$rel"; fi
  key="$mod_root|$pkg"
  if [ -z "${SEEN[$key]:-}" ]; then
    SEEN[$key]=1
    echo "$mod_root $pkg"
  fi
done

""",
            ),
            # Make the patched local checkout the source of truth for the
            # module in the CURRENT directory: for every bifrost sibling module
            # required by this go.mod, add a `replace` pointing at the in-tree
            # dir, then `go mod tidy`. Without this the module would build
            # against PUBLISHED bifrost versions and a fix.patch to local
            # sibling source would be invisible (test-run == fix-run). Skips the
            # module's own path (a self-replace is meaningless) and any sibling
            # that isn't actually present as a directory+go.mod in the tree.
            File(
                ".",
                "inject_replace.sh",
                """#!/bin/bash
set -eo pipefail

# Arg: $1 = absolute repo root. cwd must already be the target module dir.
ROOT="$1"

self="$(awk '/^module /{print $2; exit}' go.mod)"

for full in $(grep -oE 'github\\.com/maximhq/bifrost/[a-zA-Z0-9/_-]+' go.mod | sort -u); do
  [ "$full" = "$self" ] && continue
  sub="${full#github.com/maximhq/bifrost/}"
  if [ -d "$ROOT/$sub" ] && [ -f "$ROOT/$sub/go.mod" ]; then
    go mod edit -replace "$full=$ROOT/$sub"
  fi
done

go mod tidy

""",
            ),
            # Strip plain `Binary files ... differ` diff blocks (git diff
            # without --binary -> no appliable content) so the remaining text
            # hunks apply atomically. Prints the cleaned patch to stdout.
            # (Same mechanism as dolthub/dolt and 99designs/gqlgen.)
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
            # Warm-up: pin the base commit, then for each owning module pre-fetch
            # deps (with local replaces) + pre-build the target package so the
            # toolchain, module cache and build cache live in the image layer.
            # `|| true` everywhere: a target dir may not exist yet at base (added
            # by the patch) and that is fine for baseline. The replace edits +
            # tidy dirty the worktree, so the tree is reset --hard back to a
            # clean base commit at the end -- the warmed /go/pkg caches persist
            # in the image regardless.
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

TEST_FILES="{test_files}"
if [ -n "$TEST_FILES" ]; then
  bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
    ( cd "$mod_root" \\
        && bash /home/inject_replace.sh "/home/{repo}" \\
        && go test -vet=off -count=1 "$pkg" ) || true
  done
fi

cd /home/{repo}
git reset --hard {base_sha}
git clean -fdq
bash /home/check_git_changes.sh

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha, test_files=test_files),
            ),
            # Baseline (no patch). Skip a package whose dir is absent at base --
            # a brand-new package added by the patch is not present here, so
            # `go test` on a missing path would abort that module's run; its
            # tests are correctly NONE at base.
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
TEST_FILES="{test_files}"
if [ -z "$TEST_FILES" ]; then
  echo "bifrost-runner: no _test.go files in test.patch"
  exit 0
fi

bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
  pkg_dir="${{pkg#./}}"
  if [ "$pkg_dir" != "." ] && [ ! -d "$mod_root/$pkg_dir" ]; then
    continue
  fi
  ( cd "$mod_root" \\
      && bash /home/inject_replace.sh "/home/{repo}" \\
      && go test -vet=off -count=1 -v -timeout 30m "$pkg" )
done

""".format(repo=self.pr.repo, test_files=test_files),
            ),
            # test.patch only. Apply from the repo root (patch paths are
            # repo-root-relative) after stripping unappliable binary blocks,
            # then run each owning module's target package with local replaces.
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /home/test.clean.patch
git apply --whitespace=nowarn /home/test.clean.patch
TEST_FILES="{test_files}"
if [ -z "$TEST_FILES" ]; then
  echo "bifrost-runner: no _test.go files in test.patch"
  exit 0
fi

bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
  ( cd "$mod_root" \\
      && bash /home/inject_replace.sh "/home/{repo}" \\
      && go test -vet=off -count=1 -v -timeout 30m "$pkg" )
done

""".format(repo=self.pr.repo, test_files=test_files),
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
TEST_FILES="{test_files}"
if [ -z "$TEST_FILES" ]; then
  echo "bifrost-runner: no _test.go files in test.patch"
  exit 0
fi

bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
  ( cd "$mod_root" \\
      && bash /home/inject_replace.sh "/home/{repo}" \\
      && go test -vet=off -count=1 -v -timeout 30m "$pkg" )
done

""".format(repo=self.pr.repo, test_files=test_files),
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


@Instance.register("maximhq", "bifrost")
class Bifrost(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BifrostImageDefault(self.pr, self._config)

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
