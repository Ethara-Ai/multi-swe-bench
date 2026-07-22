import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class NtfyImageBase(Image):
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
        # Pinned instead of `golang:latest` so a rebuild months from now
        # produces the same image. ntfy's dataset spans release lines 1.9
        # (2021, go.mod `go 1.16`) through 2.19 (2025, `go 1.24`); a single
        # modern toolchain covers all of them because the `go` directive only
        # ever forces an *upgrade*, never a downgrade. GOTOOLCHAIN=auto (set
        # below) fetches the exact newer toolchain for any record whose go.mod
        # asks for more than 1.25.
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

        repo = self.pr.repo
        org = self.pr.org

        # One shared base for all 27 records (tag is "base", not per-PR), so it
        # clones at default HEAD and keeps full history -- every record's
        # base.sha must stay reachable from here. The per-record checkout and
        # the anti-cheat history strip happen in the PR layer
        # (NtfyImageDefault), which is the only layer that knows a single sha.
        #
        # The `# syntax` directive makes DockerfileEnhancer.enhance() return
        # this verbatim. That is deliberate: the enhancer would otherwise
        # rewrite the clone into a `git checkout ${BASE_COMMIT}` + hardening
        # block, which would pin this *shared* base to whichever record
        # happened to trigger the build and make the other 26 base.shas
        # unreachable. REPO_URL is still declared as an ARG so the build arg
        # that build_dataset passes is consumed here.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

# CGO is required: ntfy's message/user/auth stores are backed by
# mattn/go-sqlite3, which is a cgo package.
ENV CGO_ENABLED=1
ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=auto

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    build-essential \\
    gcc \\
    git \\
    make \\
    && rm -rf /var/lib/apt/lists/*

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


class NtfyImageDefault(Image):
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
        return NtfyImageBase(self.pr, self._config)

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

# ntfy's server package embeds the built web app and the mkdocs output:
#   //go:embed site      -> server/site
#   //go:embed docs      -> server/docs
# Neither directory is checked in (they are produced by `make web docs`, which
# needs node + mkdocs). Without them the server package fails to *compile*, so
# every test in the repo would error out before running. Placeholder files make
# the embeds resolve to an empty FS, which is all the Go tests need.
mkdir -p server/site server/docs server/templates
touch server/site/placeholder server/docs/placeholder

# Warm the module cache and the build cache so the graded run/test/fix stages
# are not dominated by downloads. `|| true` because some older release lines
# reference modules that no longer resolve cleanly; a genuine failure will
# resurface in the graded stages rather than being hidden here.
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared helpers for the ntfy run/test/fix scripts.
#
# These are release-line bundles, so fix.patch is large (hundreds of KB) and
# spans far more than Go source: the built web app under web/ and
# server/site/, mkdocs sources under docs/, screenshots, fonts and other
# binaries. Only the Go packages matter for grading, so binary/asset paths are
# excluded from `git apply` (they would abort the whole patch on a binary-diff
# hunk) and the test scope is narrowed to the Go packages the patches touch.

EXCLUDES="--exclude=*.lock --exclude=*.png --exclude=*.ico --exclude=*.mp4 \
--exclude=*.svg --exclude=*.gif --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.webp --exclude=*.pdf --exclude=*.woff --exclude=*.woff2 \
--exclude=*.ttf --exclude=*.eot --exclude=*.map --exclude=*.min.js \
--exclude=web/public/static/* --exclude=server/site/*"

# Apply a patch, escalating through the standard fallbacks. Unlike the previous
# revision of this file the failure is NOT swallowed: a fix.patch that does not
# apply must abort the stage loudly. Silently continuing produced runs where
# `fix` scored identically to `run`, which reads as "the fix changed nothing"
# when the truth was "the fix was never applied" -- an invalid record that is
# easy to mistake for a model failure.
apply_patch() {
  local f="$1"
  [ -s "$f" ] || { echo "apply_patch: $f is empty, nothing to apply"; return 0; }

  if git apply --whitespace=nowarn $EXCLUDES "$f" 2>/dev/null; then
    echo "apply_patch: applied $f cleanly"
    return 0
  fi
  if git apply --whitespace=nowarn --3way $EXCLUDES "$f" 2>/dev/null; then
    echo "apply_patch: applied $f via 3-way merge"
    return 0
  fi
  if git apply --whitespace=nowarn --reject $EXCLUDES "$f"; then
    echo "apply_patch: applied $f with --reject"
    find . -name '*.rej' -delete 2>/dev/null || true
    return 0
  fi

  # --reject applies every hunk it can and exits non-zero only when some hunk
  # was rejected. Report which files were left incomplete, then fail the stage.
  echo "apply_patch: FAILED to fully apply $f; rejected hunks in:"
  find . -name '*.rej' -print 2>/dev/null || true
  find . -name '*.rej' -delete 2>/dev/null || true
  return 1
}

# Print the unique Go package directories touched by test.patch + fix.patch
# that actually exist on disk and contain Go files. Written to stay safe under
# `set -eo pipefail`: a grep that matches nothing must not abort the script.
collect_pkgs() {
  local out d
  out=$(
    {
      cat /home/test.patch 2>/dev/null
      cat /home/fix.patch 2>/dev/null
    } \\
      | grep '^diff --git a/' \\
      | sed -E 's#^diff --git a/##; s# b/.*$##' \\
      | grep -E '\\.go$' \\
      | sed -E 's#/[^/]+$##' \\
      | grep -vE '^(web|docs|tools|\\.github)(/|$)' \\
      | sort -u
  ) || true

  for d in $out; do
    [ -n "$d" ] || continue
    if [ "$d" = "." ]; then
      if ls ./*.go >/dev/null 2>&1; then echo "."; fi
    elif [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then
      echo "./$d"
    fi
  done
}

run_go_tests() {
  local pkgs
  pkgs=$(collect_pkgs)
  if [ -z "$pkgs" ]; then
    echo "No Go test packages touched by the patches; nothing to run."
    return 0
  fi
  echo "=== Running go test on touched packages ==="
  echo "$pkgs"
  echo "==========================================="
  CGO_ENABLED=1 go test -v -count=1 -timeout 20m $pkgs
}

# The embed placeholders are recreated before every stage: `git apply` of a
# release-line patch can add or remove files under server/, and a stage that
# starts without them fails to compile.
ensure_embeds() {
  mkdir -p server/site server/docs server/templates
  touch server/site/placeholder server/docs/placeholder
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
source /home/common.sh

ensure_embeds
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
ensure_embeds
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
apply_patch /home/fix.patch
ensure_embeds
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

        # Anti-cheat hardening, per the contract in
        # multi_swe_bench.harness.image. The shared base deliberately keeps
        # full history, so the strip has to happen here, in the only layer that
        # knows a single base.sha. prepare.sh has already checked that sha out;
        # this block detaches at it, deletes every other ref, expires the
        # reflog, gc --prune=now, and then asserts
        # `rev-list --all == rev-list HEAD`.
        #
        # That assertion is the whole point: it proves no commit *after*
        # base.sha survives in the image, so a model in the container cannot
        # recover the real fix by reading `git log`, `git show <later-sha>`,
        # or a leftover origin ref. BASE_COMMIT is substituted with the literal
        # sha because build_dataset only passes the BASE_COMMIT build arg to
        # images whose dependency() is a string (the base), not to PR images.
        #
        # The `# syntax` directive keeps DockerfileEnhancer from rewriting this
        # layout. It is a no-op here regardless -- enhance() returns raw when
        # dependency() is an Image -- but stating it keeps the two layers
        # consistent and self-documenting.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("binwiederhier", "ntfy")
class Ntfy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NtfyImageDefault(self.pr, self._config)

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
        # A package summary line ("ok <path>", "FAIL <path>", "? <path>")
        # closes the block of tests printed above it.
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        # Tests are buffered per package so the import path can be prepended.
        # Several ntfy packages define identically named tests (e.g.
        # TestServer_* exists in both server/ and client/), so unqualified
        # names would collide and silently under-count.
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
                name = fail_match.group(1)
                pending_pass.discard(name)
                pending_skip.discard(name)
                pending_fail.add(name)
                continue

            skip_match = re_skip.match(line)
            if skip_match:
                name = skip_match.group(1)
                if name not in pending_fail:
                    pending_pass.discard(name)
                    pending_skip.add(name)
                continue

            pkg_match = re_pkg.match(line)
            if pkg_match:
                flush(pkg_match.group(1))

        # A package that panicked or was killed by the timeout never prints its
        # summary line; flush whatever is still buffered under a sentinel so
        # those results are not dropped.
        if pending_pass or pending_fail or pending_skip:
            flush("unknown")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
