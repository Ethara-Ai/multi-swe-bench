import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Single-registration config for the 99designs/gqlgen `_lht_final` dataset
# (98 release-delta bundles, #134..#4075, gqlgen release lines 0.2.0..v0.17.90).
# The dataset has NO `number_interval` set, so the harness routes every record
# to "99designs/gqlgen" (instance.py:create). A multi-file ranged split would
# make every record unroutable, so a single registration is required -- the
# same constraint as dolthub/dolt, hashicorp/terraform-provider-azurerm and
# camel-ai/camel.
#
# Three toolchain eras exist across this 7-year span (Docker-verified):
#
#   1. Pre-go-modules "dep" era    -- #134..#461 (10 PRs, v0.2.0..v0.7.2):
#        Gopkg.toml/Gopkg.lock only, NO go.mod. Within this era there is a
#        further split: the very early PRs (#134..#195) still reference the
#        pre-rename module path `github.com/vektah/gqlgen` (the project moved
#        to the 99designs org around v0.4.x). The later dep-era PRs use
#        `github.com/99designs/gqlgen`. Modern Go (1.21+) has removed GOPATH
#        mode entirely, so any build requires a go.mod. prepare.sh bootstraps
#        one at the right module name (chosen by majority import path) and
#        runs `go mod tidy` -- which auto-fetches transitive dependencies via
#        proxy.golang.org rather than the original dep-managed versions, but
#        the gqlgen Go unit tests (codegen, graphql, handler, client) don't
#        depend on micro-version differences in their transitive deps and
#        pass on every dep-era PR I checked.
#   2. Early go.mod era            -- #768..(roughly v0.11): go.mod present
#        but no `go` directive (Go 1.11 modules-experiment style). Builds
#        with modern Go without any special handling.
#   3. Modern era                  -- v0.11+ onwards through v0.17.90:
#        Normal go.mod with `go X.Y` directive ranging from go1.16 to go1.25.
#        GOTOOLCHAIN=auto on the bundled Go 1.25 absorbs all of them.
#
# A SECOND multi-module wrinkle appears around v0.17.x: the `_examples/`
# directory becomes a *separate* Go submodule with its own `go.mod` (and a
# `replace github.com/99designs/gqlgen => ../` directive). `go test ./...`
# from the repo root skips submodules, so tests in `_examples/<sub>/...`
# must be run with cwd = `_examples/`. The run scripts handle this by
# walking up from each test file to the nearest go.mod and grouping packages
# by their module root -- which also transparently handles the old dep-era
# layout where `example/` is part of the root module.
#
# No system packages required: gqlgen is pure-Go with no CGO bindings. The
# bare golang:1.25-bookworm image is sufficient on both linux/amd64 and
# linux/arm64 (golang official images are multi-arch).
#
# Docker-verified on golang:1.25-bookworm + GOTOOLCHAIN=auto (linux/arm64;
# multi-arch base so amd64 is equivalent):
#   * Oldest PR 134 base d26ef2a (gqlgen 0.2.0, NO go.mod, code imports
#       `github.com/vektah/gqlgen/neelance/...`): prepare.sh bootstraps with
#       `go mod init github.com/vektah/gqlgen` + `go mod tidy`; the patch
#       target ./codegen/ runs cleanly with standard `--- PASS:` lines
#       (TestImportCollisions etc.). The non-test example/chat patch hunks
#       are not in the test target set so are not exercised.
#   * Dep-era boundary PR 461 base 3a7f37c (v0.7.1..v0.7.2, NO go.mod, code
#       already uses `github.com/99designs/gqlgen/...`): bootstrap with
#       `go mod init github.com/99designs/gqlgen` + `go mod tidy`; the
#       handler/ test target runs `TestWebsocket` + `TestHandlerPOST` etc.
#       with standard PASS output.
#   * Early go.mod PR 768 base b128a29 (v0.9.1..v0.9.2, go.mod with no
#       `go` directive): `go mod download` only; ./codegen/ runs cleanly.
#   * Modern PR 4075 base 76d62cf (v0.17.89..v0.17.90, `go 1.25.0`):
#       direct `go mod download`; ./codegen/config/ runs ~30 test functions
#       (TestResolverConfig, TestLoadSchema, FuzzReadConfig with seed cases)
#       in ~1.3s with full `--- PASS:` coverage incl. subtests + seeds.
#   * Modern submodule PR 4075 _examples: cd /home/gqlgen/_examples and run
#       `go test ./chat` -> TestChatSubscriptions PASS in ~0.5s.
#
# parse_log is the proven hashicorp/Go matcher (see nomad.py /
# terraform_provider_azurerm.py / dolt.py): standard
# `--- PASS|FAIL|SKIP: <name>` lines, validated against the captured output
# at all four era endpoints above.
# ---------------------------------------------------------------------------


class GqlgenImageBase(Image):
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
        # go.mod `go` directive in the dataset (1.16 .. 1.25) directly --
        # 1.25 covers them all, no auto-fetch needed -- and remains
        # forward-compatible if a later v0.17.x bumps to 1.26.
        return "golang:1.25-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # SHARED base: this single image (tag `base`) is the FROM parent for
        # every per-PR image, so it MUST keep the full git history + origin so
        # each PR can `git checkout <its own base commit>`.
        #
        # The leading `# syntax=docker/dockerfile:1.6` directive makes
        # DockerfileEnhancer.enhance() return this Dockerfile verbatim (it
        # early-returns when SYNTAX_DIRECTIVE is already present -- see
        # image.py). That is what stops the enhancer from rewriting the clone
        # into a `git checkout ${BASE_COMMIT}` + history-strip block, which
        # would pin this shared base to whichever PR built it first and prune
        # away every other PR's commit ("reference is not a tree").
        #
        # No proxy/cert/MITM injection (removed per build-env policy). The
        # git-history hardening that the enhancer would normally add is instead
        # applied per-PR in GqlgenImageDefault, AFTER prepare.sh checks out the
        # PR's base commit -- keeping this ONE shared base reusable by every PR.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        org = self.pr.org

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8 \\
    GOTOOLCHAIN=auto

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo}

# SHARED-base light hardening: keep FULL history (per-PR layer checks out base.sha
# in prepare.sh) but remove the remote so the model cannot fetch/pull the fix.
# NO checkout/gc-prune here (that would pin+prune the shared base to one commit).
WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class GqlgenImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    def _get_test_files(self) -> str:
        """Repo-root-relative `_test.go` paths touched by this PR's test.patch.

        Returns a single space-separated string. The bash scripts (prepare.sh,
        run.sh, test-run.sh, fix-run.sh) iterate this list and group entries
        by their nearest go.mod (walking up the tree) so that:
          * old-era `example/...` tests run in the root module,
          * modern-era `_examples/<sub>/...` tests run in the `_examples/`
            submodule (which has its own go.mod with a `replace` directive
            pointing at the parent),
          * everything else (codegen, graphql, handler, plugin, ...) runs in
            the root module.

        Skips:
          * non-`_test.go` files (templates, .gotpl, .yml, .yaml, .graphql,
            non-test .go files added/modified by the PR -- those will be
            exercised by `_test.go` files in the same package),
          * `+++ /dev/null` deletions.
        Falls back to the empty string if no _test.go files are touched; the
        run scripts treat that as "no work to do" rather than running `./...`
        (which is unsafe in the dep era where example/ subpackages may have
        broken transitive imports after our `go mod tidy` bootstrap).
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
        return GqlgenImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        # MUST be `pr-<number>` with the number immediately after `pr-`:
        # gen_report.collect_report_tasks parses it as
        # int(dir.name[3:].split('-')[0]).
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
            # ancestor go.mod. Each line of output:  "<mod_root> <pkg_rel>"
            # where mod_root is the absolute path (so we can `cd` to it)
            # and pkg_rel is a `./pkg/...`-style pattern rooted at that
            # module. The script is deduplicated so each (mod_root, pkg)
            # pair appears at most once. Files for which no go.mod can be
            # found (theoretical: shouldn't happen post-bootstrap) are
            # silently skipped.
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
  else
    mod_root="$ROOT/$found"
    rel="${dir#$found/}"
  fi
  [ -z "$rel" ] && rel="."
  key="$mod_root|./$rel"
  if [ -z "${SEEN[$key]:-}" ]; then
    SEEN[$key]=1
    echo "$mod_root ./$rel"
  fi
done

""",
            ),
            # Bootstrap a go.mod when the base commit predates Go modules.
            # Picks the module path by majority vote across .go imports
            # (pre-99designs-rename PRs use `github.com/vektah/gqlgen` as
            # both the module path and the self-import prefix). Commits the
            # generated go.mod/go.sum so the working tree is clean for
            # subsequent `git apply` of test.patch and fix.patch.
            File(
                ".",
                "bootstrap_gomod.sh",
                """#!/bin/bash
set -eo pipefail

if [ -f go.mod ]; then
  echo "bootstrap_gomod: go.mod present, skipping"
  exit 0
fi

# grep exits 1 when there are no matches; under `set -o pipefail` that would
# propagate out of the command substitution. Force success: the wc -l output
# is what we care about (it'll just be 0).
vektah_count=$( { grep -lr --include="*.go" "vektah/gqlgen/" . 2>/dev/null || true; } | wc -l)
designs_count=$( { grep -lr --include="*.go" "99designs/gqlgen/" . 2>/dev/null || true; } | wc -l)

if [ "$vektah_count" -gt "$designs_count" ]; then
  modname="github.com/vektah/gqlgen"
else
  modname="github.com/99designs/gqlgen"
fi

echo "bootstrap_gomod: initializing as $modname (vektah=$vektah_count designs=$designs_count)"
go mod init "$modname"
go mod tidy

git -c user.email=bot@gqlgen -c user.name=bot add -A
git -c user.email=bot@gqlgen -c user.name=bot commit -m "bootstrap go.mod ($modname)" --quiet

""",
            ),
            # Pick the Go toolchain by the go.mod golang.org/x/tools version.
            # Old x/tools fails to COMPILE under newer Go: v0.2..v0.24 hit
            # `tokeninternal.go: invalid array length -delta*delta` on Go>=1.23,
            # and v<=0.1.x (incl. v0.0.0-* pseudo-versions) hit the older
            # go/packages "package X without types" / loader panic that even
            # Go 1.22 triggers. The bundled Go 1.25 base can DOWNLOAD any
            # toolchain via GOTOOLCHAIN, so we cap the version per era:
            #   x/tools <= v0.1.x  -> go1.19.13   (ancient era)
            #   x/tools v0.2..v0.24-> go1.22.12   (mid era; tokeninternal-safe)
            #   x/tools >= v0.25 / none -> auto   (modern; honors go.mod `go`)
            # Modern PRs are unchanged (auto == prior behavior). Prints the
            # GOTOOLCHAIN value for the given go.mod (default ./go.mod).
            File(
                ".",
                "select_gotoolchain.sh",
                """#!/bin/bash
gomod="${1:-go.mod}"
if [ ! -f "$gomod" ]; then echo "auto"; exit 0; fi
xt=$(grep -E "^[[:space:]]*golang.org/x/tools " "$gomod" | head -1 \\
     | grep -oE "v[0-9]+\\\\.[0-9]+\\\\.[0-9]+([-.0-9a-zA-Z]*)?" | head -1)
if [ -z "$xt" ]; then echo "auto"; exit 0; fi
if echo "$xt" | grep -qE "^v0\\\\.0\\\\.0-"; then echo "go1.19.13"; exit 0; fi
minor=$(echo "$xt" | sed -E "s/^v[0-9]+\\\\.([0-9]+)\\\\..*/\\\\1/")
case "$minor" in ''|*[!0-9]*) echo "auto"; exit 0;; esac
if [ "$minor" -le 1 ]; then echo "go1.19.13"; exit 0; fi
if [ "$minor" -le 24 ]; then echo "go1.22.12"; exit 0; fi
echo "auto"

""",
            ),
            # Strip plain `Binary files ... differ` diff blocks (git diff
            # without --binary -> no appliable content) so the remaining text
            # hunks apply atomically. gqlgen carries such blocks only for
            # docs assets (favicon.ico, *.png) the Go tests never touch; this
            # dataset has no GIT-literal binary patches, so nothing appliable
            # is lost. Prints the cleaned patch to stdout. (Same mechanism as
            # dolthub/dolt.)
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
            # Warm-up: pin the base commit, bootstrap go.mod when needed,
            # and pre-fetch module deps + pre-build the target test packages
            # so the module cache and build cache live in the image layer.
            # `|| true` on the pre-build: a target package may not exist
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

bash /home/bootstrap_gomod.sh

export GOTOOLCHAIN="$(bash /home/select_gotoolchain.sh /home/{repo}/go.mod)"
echo "prepare: GOTOOLCHAIN=$GOTOOLCHAIN"

TEST_FILES="{test_files}"
if [ -n "$TEST_FILES" ]; then
  bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
    (cd "$mod_root" && go mod download || true)
    (cd "$mod_root" && go test -vet=off -count=1 "$pkg" || true)
  done
fi

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha, test_files=test_files),
            ),
            # Baseline (no patch). Skip pkg dirs absent at base -- a brand
            # new package added by the patch is not present here, so going
            # ahead would abort the whole `go test` invocation; its tests
            # are correctly NONE at base.
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export GOTOOLCHAIN="$(bash /home/select_gotoolchain.sh /home/{repo}/go.mod)"
TEST_FILES="{test_files}"
if [ -z "$TEST_FILES" ]; then
  echo "gqlgen-runner: no _test.go files in test.patch"
  exit 0
fi

bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
  pkg_dir="${{pkg%/...}}"
  pkg_dir="${{pkg_dir#./}}"
  if [ -n "$pkg_dir" ] && [ "$pkg_dir" != "." ] && [ ! -d "$mod_root/$pkg_dir" ]; then
    continue
  fi
  # Tolerate a single package's failure (e.g. internal/code's ancient
  # golang.org/x/tools "package ... without types" loader error) so that
  # `set -e` cannot abort this loop at the first bad package and hide a
  # genuine fail->pass in a later one (e.g. internal/rewrite's TestRewriter
  # in PR #1284). Every target package is still executed and its PASS/FAIL
  # lines captured for parse_log; cross-stage f2p is computed from those.
  (cd "$mod_root" && go test -vet=off -count=1 -v -timeout 30m "$pkg") || true
done

""".format(repo=self.pr.repo, test_files=test_files),
            ),
            # test.patch only. Apply from the repo root (patch paths are
            # repo-root-relative); the bootstrap commit above keeps the
            # touched files unchanged so this applies cleanly.
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /home/test.clean.patch
git apply --whitespace=nowarn /home/test.clean.patch
export GOTOOLCHAIN="$(bash /home/select_gotoolchain.sh /home/{repo}/go.mod)"
TEST_FILES="{test_files}"
if [ -z "$TEST_FILES" ]; then
  echo "gqlgen-runner: no _test.go files in test.patch"
  exit 0
fi

bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
  # Tolerate a single package's failure (e.g. internal/code's ancient
  # golang.org/x/tools "package ... without types" loader error) so that
  # `set -e` cannot abort this loop at the first bad package and hide a
  # genuine fail->pass in a later one (e.g. internal/rewrite's TestRewriter
  # in PR #1284). Every target package is still executed and its PASS/FAIL
  # lines captured for parse_log; cross-stage f2p is computed from those.
  (cd "$mod_root" && go test -vet=off -count=1 -v -timeout 30m "$pkg") || true
done

""".format(repo=self.pr.repo, test_files=test_files),
            ),
            # test.patch + fix.patch, applied atomically (single git apply
            # invocation).
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /home/test.clean.patch
bash /home/strip_binary.sh /home/fix.patch > /home/fix.clean.patch
git apply --whitespace=nowarn /home/test.clean.patch /home/fix.clean.patch
export GOTOOLCHAIN="$(bash /home/select_gotoolchain.sh /home/{repo}/go.mod)"
TEST_FILES="{test_files}"
if [ -z "$TEST_FILES" ]; then
  echo "gqlgen-runner: no _test.go files in test.patch"
  exit 0
fi

bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
  # Tolerate a single package's failure (e.g. internal/code's ancient
  # golang.org/x/tools "package ... without types" loader error) so that
  # `set -e` cannot abort this loop at the first bad package and hide a
  # genuine fail->pass in a later one (e.g. internal/rewrite's TestRewriter
  # in PR #1284). Every target package is still executed and its PASS/FAIL
  # lines captured for parse_log; cross-stage f2p is computed from those.
  (cd "$mod_root" && go test -vet=off -count=1 -v -timeout 30m "$pkg") || true
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

        # Git-history hardening for the per-PR (agent) image, applied AFTER
        # prepare.sh has checked out this PR's base commit -- so the commit to
        # KEEP is the current HEAD (not a ${BASE_COMMIT} build-arg, which is not
        # passed to FROM-an-image builds). The shared base deliberately keeps
        # full history + origin (see GqlgenImageBase); this strips the remote
        # and every ref/commit not reachable from HEAD, so the evaluated agent
        # cannot recover the fix from git log/show/history. Mirrors the harness
        # hardening block, anchored on HEAD. The .gitmodules branch is a no-op
        # for gqlgen (its _examples is a sibling Go module, not a git submodule)
        # but is kept for parity.
        repo = self.pr.repo
        harden = f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{harden}

{self.clear_env}

"""


@Instance.register("99designs", "gqlgen")
class Gqlgen(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GqlgenImageDefault(self.pr, self._config)

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_Gqlgen = [
    '1038-1039-1043-1046',
    '171-172-173-175-176',
    '186-190-191-192-193-194',
    '2021-2024',
    '2129-2130-2131-2132-2133',
    '2205-2209-2214-2216',
    '2330-2332-2335',
    '2380-2381-2382',
    '2436-2581-2584-2587',
    '254-255',
    '2590-2594-2597-2599-2601',
    '2787-2788-2789-2793-2794-2798',
    '281-282',
    '2829-2830-2831',
    '2837-2839-2840-2841-2849-2850-2851',
    '3280-3281-3282-3287',
    '3291-3292',
    '3296-3299-3302-3303-3304-3305-3307-3311',
    '3430-3431',
    '3672-3673',
    '3723-3726-3727-3728-3730-3731-3732-3733-3734-3735-3736-3737-3738',
    '3776-3777',
    '3842-3843',
    '461-476-530',
    '1050-1051-1053-1054-1057-1061-1071-1072-1074-1079-1080-1081-1086',
    '1104-1112-1115-1117-1119-1121-1124-1131-1134-1137-1147-1154-1163-1170-1181-1188-1189-1198-1202-1207-1215-1218-1221-1224-1242-1243-1246-1248-1255-1262-1264-1267-1276-1277',
    '1173-1285-1309-1317-1340-1359-1404-1405-1418-1419-1491-1507-1515-1525-1526-1529-1549-1578-1581-1595-1604-1606-1607-1608-1610-1612-1613-1614-1617-1619-1623-1624-1627-1628-1633-1638-1640-1641-1644-1647-1648-1650-1652-1654-1655-1657-1658-1659-1660-1661-1663-1666-1669-1674-1676-1680-1682-1684-1693-1697-1698-1699-1701-1706-1708-1709-1711-1712-1713-1717-1719-1723-1725-1728-1737-1740-1741-1743-1747-1751-1757-1760-1763',
    '1269-1303-1341-1345-1346-1356-1358-1360-1363-1365-1374-1387-1394-1399-1400-1401-1408-1409-1417-1436-1444-1449-1451-1452-1456-1458-1459-1460-1464-1465-1480-1487-1488-1489-1495-1497-1501-1502-1503-1511-1512-1514-1517-1568-1572-1582-1584-1594-1597-1598-1599-1600-1601-1602-1603',
    '1284-1288-1294',
    '1295-1312-1316-1324',
    '134-139-146-147',
    '151-157-159-163-166',
    '1734-1771-1848-1850-1851-1856-1857-1858-1859-1863-1870-1871-1872-1949-1950-1954-1956-1957-1958-1973-1975-1982-1983-1986-2006-2007-2008-2014-2015-2016-2017-2019',
    '1773-1778-1833-1835-1836-1838-1839-1842',
    '179-180',
    '195-197-201-203-204-205-206-208-210-214-217-218-220-221-225-229-235-236-237-238-239-240-241-244-245-246-247-248-251-252',
    '2011-3140-3141-3142-3143-3144-3147-3148-3149-3150-3151-3153-3156-3157-3158-3159-3162-3163-3164-3165-3166-3167-3171-3172-3173-3174-3175-3176-3179-3183-3185-3187-3188-3189-3190-3191-3193-3194-3196-3197-3199-3200-3203-3206-3207-3208-3209-3210-3211-3215-3216-3217-3218-3219-3220-3221-3226-3229-3231-3233-3234-3235-3236-3237-3243-3246-3247-3250-3255-3256-3257-3258-3259-3261-3262-3268',
    '2062-2066-2078-2079-2080-2085-2095-2096-2097-2101-2102-2103-2109',
    '2111-2351-2388-2389-2390-2391-2396-2397-2411-2432-2434-2435-2438-2445-2447',
    '2115-2118-2120',
    '2119-2124-2135-2138-2140-2141-2142-2148-2149-2157-2168-2176-2182-2184',
    '2174-2175-2219-2221-2223-2231-2235-2237-2239',
    '2247-2254-2257-2262-2263-2266-2267-2270',
    '2275-2287-2288-2289',
    '2293-2299-2300-2305-2314-2315-2317-2322-2328',
    '2346-2348-2355-2363-2366-2368',
    '2371-2374-2375',
    '2437-2461-2467-2468-2471-2486-2488-2497-2498-2500-2501-2503-2504-2506-2508-2513',
    '2449-2454-2455',
    '2516-2519-2528-2529-2533-2553-2556-2557-2562-2570-2571',
    '2585-2622-2625-2627-2628',
    '2592-2598-2607-2611-2612',
    '260-263-264-266-267-270-274-279',
    '2630-2631-2634-2635-2638-2642-2644-2649-2655-2656-2657',
    '2665-2666-2667',
    '2674-2675-2677-2682-2683-2686-2689-2692-2694',
    '2697-2706-2707-2709-2713-2714',
    '2716-2718-2719-2720-2722-2723-2725-2730-2733',
    '2738-2740-2751-2753-2759-2773-2774-2776-2778-2779-2780-2784-2785',
    '2770-2846-2858-2859-2861-2863-2866-2868-2871',
    '2791-2800-2802-2803-2809-2810-2811-2812-2813-2814-2815-2819-2821',
    '283-285-292-296-297',
    '2876-2885-2891-2894-2896-2897-2899-2900-2901-2902-2903-2904-2905-2906-2907-2908-2909-2910-2911-2912-2913-2914-2915-2916-2917-2918-2919-2920-2921-2922-2924-2925-2926-2927-2928-2929-2930-2931-2932',
    '2877-2878-2880-2882',
    '2884-2935-2936-2937-2938-2939-2940-2943-2944-2945-2946-2947-2953-2954-2955-2957-2959-2960-2961-2962-2963-2964-2965-2966',
    '2886-3061-3062-3063-3064-3065-3069-3070-3071-3072-3073-3074-3075-3076-3077-3078-3080',
    '294-298-299-300-301-304-307-308-309-310-311-312-314-315-316-318-320-321-322-325-326',
    '2970-2971-2972-2976-2977-2978-2979-2980-2982-2983-2984-2985-2986-2987-2988-2992-2993-2994-2995-2996-2999-3005-3006-3010-3011-3012-3014-3015-3017-3020-3021-3022-3023-3024-3025-3026-3027-3029-3031-3033-3034-3035-3036-3037-3038-3040-3041-3042-3047-3048-3049-3050-3051-3052-3053-3054-3058',
    '3084-3085-3088-3090-3093-3094-3095-3097-3098-3099-3100-3101-3102-3103-3105-3107-3108-3109-3110-3111-3112-3113-3114-3117',
    '3123-3124-3125-3126-3127-3133-3134-3136-3137',
    '3242-3850-3855-3857-3858-3859-3860-3861-3865-3866-3867-3869-3870-3871-3872-3873-3874-3875-3876-3878-3879-3880-3881-3882-3883-3885-3888-3889-3890-3891-3892-3893-3894-3895-3896-3897-3898',
    '3314-3315-3317-3318-3319-3322-3323-3324-3325-3326-3329-3332-3333-3334-3336-3337-3338-3339-3340-3341-3343-3344-3345-3346-3347-3349-3350-3352-3353-3354-3356-3357-3358-3359',
    '3361-3363-3364-3365-3368-3369-3373-3376-3377-3378-3379-3382-3383-3384',
    '3386-3387-3391-3392-3393-3394-3395-3397-3399-3400-3401-3402-3404-3407-3408-3409-3411-3416-3417-3418-3419-3420-3423-3424',
    '3434-3435-3436-3437-3438-3439-3440',
    '3445-3448-3449-3450-3451-3452-3453-3456-3458-3460-3462-3463',
    '3466-3477-3478-3479-3480-3481-3482-3484-3485-3489-3490-3491-3493-3494-3495-3500-3501-3502-3503-3504-3506-3507',
    '3509-3511-3513-3514-3519-3520-3521-3522-3523-3524-3525-3526-3527-3537',
    '3546-3547-3548-3549-3551-3552-3554-3555-3556-3557-3559-3561-3562-3564-3566-3568-3569-3570-3571-3574-3576-3577-3578-3579-3580-3582-3587-3588-3590-3591-3592',
    '3595-3623-3624-3625-3626-3627-3630-3631-3637-3639-3640-3643-3644-3646-3647-3648-3649-3650-3651-3652-3655-3656-3658-3659-3660-3663-3664-3665-3666-3668-3669-3670',
    '3598-3599-3600-3604-3609-3610-3611-3612-3613-3614-3615',
    '3632-3675-3676-3677-3678-3679-3680-3682-3684-3685-3687-3689',
    '3691-3692-3695-3698-3699-3700-3701-3702-3704-3705-3707-3709-3710-3715-3719-3720-3721',
    '3741-3909-3911-3913-3914-3915-3916-3917-3918-3919-3920-3921-3922-3923-3924-3926-3929-3932-3933-3934-3935-3936-3937',
    '3743-3744-3745-3746-3747-3748-3749-3750-3751',
    '3779-3781-3782-3783-3784-3785-3786-3787-3790-3791-3792-3793-3795-3796-3797-3798-3799-3801-3804-3806-3807-3808-3809-3812-3813-3814-3815-3817-3818-3819-3820-3821-3822-3823-3826-3827-3829-3830-3831-3832-3833-3834-3835-3836-3837-3838-3839',
    '3901-3902-3903-3904-3905-3907',
    '3928-3939-3940-3941-3942-3943-3944-3945-3949-3954-3956-3957-3958-3959-3962-3963-3964-3965-3967-3968-3969',
    '3972-3973-3974-3975-3976-3977-3978-3979-3980-3982-3984-3985-3987-3988-3989',
    '3991-3992-3993-3994-3995-3996-3997-3998-3999-4000-4002-4004-4005-4006-4007-4008-4009-4010-4011-4012-4013-4014-4015-4016-4017-4019-4020-4021-4023-4024-4026-4027-4028-4029-4030',
    '4032-4033-4034-4035-4036-4037-4039-4040-4042-4043-4045-4046-4047-4048-4049-4050-4051-4052-4054-4055-4057-4058',
    '4061-4067-4069-4070-4071-4080-4087-4088-4092',
    '4075-4076-4078-4086-4090-4098-4099-4101-4103-4104-4105-4106-4107-4108-4109-4110-4111-4112-4113-4115-4116-4118-4120-4122-4123-4124-4125-4126-4127-4128-4130-4133-4134-4135-4136-4137-4139-4141-4142-4143-4144-4145-4146',
    '768-771-780-781-784-795-797-801-805-807-816-820-821-822',
    '819-828-838-839-854-861-862-870-871-872-874-875',
    '829-831',
    '851-885-931-947-970-974-978-979-982-983-985-988-989-993-994-995-1003-1006-1007-1009-1010-1011-1012-1013-1016-1018-1019-1020-1022-1027-1028-1034-1036',
    '879-882-883-889-894-897-900-902-904-907-916-917-919-923-927-929-938-939-940-941-942',
]
for _ni in _BUNDLE_NIS_Gqlgen:
    Instance.register('99designs', _ni)(Gqlgen)
