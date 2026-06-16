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
        # Because we opt out of the enhancer we reproduce its proxy/cert infra
        # here so build-env parity (MITM proxy) is kept and per-PR images
        # inherit it via FROM. The git-history hardening that the enhancer
        # would normally add is instead applied per-PR in GqlgenImageDefault,
        # AFTER prepare.sh checks out the PR's base commit.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT=""
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

{self.global_env}

ENV GOTOOLCHAIN=auto

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem

WORKDIR /home/

{code}

{self.clear_env}

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
