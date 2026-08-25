import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Toolchain
# ---------------------------------------------------------------------------

# Pinned from the `go` directive in go.mod at PR #229's base.sha
# (1d6cea8ab2d6f304a9dcb710f2776a7afa15fa91 -> "go 1.16") and from the
# Makefile's own `GO_VERSION=1.16`; the merge commit (2e20bb07) still declares
# `go 1.16`, so a single era covers this dataset. `-bullseye` rather than the
# default `golang:1.16` (Debian *buster*), whose apt indexes were retired to
# archive.debian.org and now 404 for any downstream layer that runs
# `apt-get update`. Nothing here installs packages, so the variant only has to
# stay resolvable; bullseye does. Both variants publish linux/amd64 and
# linux/arm64, so the image is multi-arch clean.
_GO_IMAGE = "golang:1.16-bullseye"

# ---------------------------------------------------------------------------
# Test scope
# ---------------------------------------------------------------------------

# The Makefile's own unit-test scope is
#   go test ./main/... ./sys/... ./storage/... ./service/...
# (target `test`, mirrored by .circleci/config.yml's `test-go` job). The
# top-level `./main` package is expanded into its sub-packages here for one
# reason: main/main.go imports github.com/ProxeusApp/proxeus-core/main/handlers/assets,
# a go-bindata artifact generated from ./ui/core/dist and .gitignore'd
# (.gitignore:8), so it is absent from a fresh clone. CI produces it in a
# separate `build-ui` job (yarn + webpack) and attaches it to the workspace.
# Building the UI to satisfy a package that contributes ZERO runnable tests --
# main/main_test.go is behind the `coverage` build tag and is never selected by
# an untagged `go test` -- would add a Node/yarn toolchain to the image for no
# measured test. Enumerating main's sub-packages keeps every test the Makefile
# scope would have run (main/app, main/config, main/handlers/**,
# main/priceservice, main/www) while dropping only the untestable root package.
#
# NOT included, deliberately:
#   ./test/...              -- `make test-api`; needs a live proxeus server,
#                              document-service and node-crypto-forex-rates.
#   ./storage/database/db   -- has tests, but they are all behind the
#                              `integration` build tag (`make test-integration`,
#                              needs a MongoDB cluster). The package is still in
#                              scope via ./storage/... and compiles; the tagged
#                              files are simply not selected.
#   ./externalnode          -- no test files.
_TEST_PKGS = (
    "./main/app/... ./main/config/... ./main/handlers/... "
    "./main/priceservice/... ./main/www/... "
    "./sys/... ./storage/... ./service/..."
)

# -p 1 builds and runs one package at a time. parse_log attributes each
# `--- PASS/FAIL/SKIP` line to the package whose `ok`/`FAIL`/`?` summary line
# closes it, so serialising package execution removes any chance of two
# packages interleaving their result lines into the same buffer.
# -count=1 disables the test result cache, so the three stages really re-run.
# -timeout 20m must stay BELOW build_dataset.py's per-stage `agent_timeout`
# (build_dataset.py:289, default 1800s = 30m). If go's own timeout were the
# larger of the two, a hung test would be killed by the harness -- container
# gone, log truncated or empty -- instead of by `go test`, which panics, dumps
# every goroutine and still prints the `--- FAIL` / `FAIL <pkg>` lines
# parse_log needs. 20m also leaves ~10m of headroom for that dump to flush.
# No `|| true`: a swallowed launch failure would hand parse_log an empty log,
# TestResult(0/0/0) and a Report rejected by rule 1 (fix_patch_result.all_count).
_TEST_CMD = f"go test -v -count=1 -p 1 -timeout 20m {_TEST_PKGS}"

# `-run` value chosen to match no test at all: compiles every test binary (and
# therefore resolves + downloads every test-only dependency) without executing
# anything. Used by prepare.sh only.
_NO_MATCH_RUN = "ZZZ_MSB_NEVER_MATCHES"

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

# PROXEUS_ENCRYPTION_SECRET_KEY is REQUIRED, not cosmetic. The fix patch makes
# SettingsDB.Put/Get run every settings value through EncryptWithAES /
# DecryptWithAES with `os.Getenv("PROXEUS_ENCRYPTION_SECRET_KEY")` as the AES
# key. storage/database/common_test.go's TestMain calls NewSettingsDB + Put
# during package setup; with the variable unset the key is 0 bytes,
# aes.NewCipher returns "invalid key size 0", TestMain's maybeFail sets code=1
# and os.Exit(1) fires BEFORE m.Run() -- i.e. the ENTIRE storage/database
# package, including the PR's own TestUtils, would silently produce no results
# in the fix stage and the instance would be rejected by rule 3. The value is
# the repo's own default, exported by the Makefile (line 32 after the fix
# patch): 32 bytes, a valid AES-256 key.
#
# Exported identically in prepare.sh and in all three run scripts so the three
# stages are compared under the same environment.
_ENV_EXPORTS = """export CI=true
export GO111MODULE=on
export PROXEUS_ENCRYPTION_SECRET_KEY=PleAsE_chAnGe_me_32_Characters++
"""

# ---------------------------------------------------------------------------
# parse_log constants
# ---------------------------------------------------------------------------

# Go's test namespace is per-package but `go test -v` prints only the bare
# function name on each result line. This repo has real homonyms in the scoped
# packages -- `TestMain` (storage/database and main/handlers/customNode) and
# `TestList` (two packages) -- so a flat set of names would merge them and let
# one package's failure be attributed to the other. Every recorded name is
# therefore `<repo-relative package dir>::<test name>`, e.g.
# `storage/database::TestUtils`.
#
# The qualifier is the package directory relative to the repo root, NOT the raw
# Go import path. `go test` prints the full import path
# (`github.com/ProxeusApp/proxeus-core/storage/database`); the
# `github.com/<org>/<repo>/` module prefix is identical on every line, carries
# no disambiguating information, and makes the IDs read as URLs rather than
# paths. Stripping it yields a real directory in the tree -- the folder the test
# lives in -- which matches the path-shaped IDs other languages produce
# (pytest emits `core/tests/foo/test_x.py::test_y`).
#
# It stops at the DIRECTORY, not the file, and that is a hard limit of the Go
# toolchain rather than a choice: a Go package is compiled from all of its .go
# files at once, so `go test` never prints which file a test was declared in.
# The directory is the deepest real path recoverable from the log.
_PKG_SEP = "::"

# Fallback qualifier for result lines that reach EOF without a package summary
# line (a panic that kills the test binary mid-package can produce them).
_UNKNOWN_PKG = "UNKNOWN_PKG"

# Namespace for the synthetic entry recorded when a whole Go package fails to
# COMPILE. This is the normal shape of this instance's test stage: test.patch
# adds storage/database/utils_test.go, which calls EncryptWithAES /
# DecryptWithAES -- functions the fix patch introduces. Before the fix they are
# undefined, so `go test` emits
#   FAIL  github.com/ProxeusApp/proxeus-core/storage/database [build failed]
# and NOT a single `--- FAIL:` line. Recording the build failure keeps that
# fact visible in the report instead of silently losing it.
#
# It cannot distort classification: the entry is FAIL in the test stage and
# absent (NONE) in the run and fix stages, and report.py's classifier skips
# every name whose fix status is not PASS. It is also invisible to rules 2 and
# 4, which both require fix == FAIL.
_BUILD_FAILED_PREFIX = "BUILD_FAILED::"


def _script(body: str, pr: PullRequest) -> str:
    """Substitute repo / sha placeholders without str.format.

    The scripts contain `${...}` and `$(...)`, which str.format would have to
    brace-escape; placeholder substitution keeps the shell text readable and
    copy-pasteable.
    """
    return (
        body.replace("@@ENV@@", _ENV_EXPORTS)
        .replace("@@REPO@@", pr.repo)
        .replace("@@BASE_SHA@@", pr.base.sha)
        .replace("@@TEST_CMD@@", _TEST_CMD)
        .replace("@@NO_MATCH_RUN@@", _NO_MATCH_RUN)
    )


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


# `|| true` on every warm-up command is required and is confined to this
# script: a module download or a compile probe that fails here must not abort
# the image build, because the same work is retried (with a real, unswallowed
# exit status) by the run scripts.
_PREPARE_SH = """#!/bin/bash
set -e

@@ENV@@
cd /home/@@REPO@@
git reset --hard
bash /home/check_git_changes.sh
git checkout @@BASE_SHA@@
bash /home/check_git_changes.sh

# Warm the module cache and the build cache for the BASE module graph, so the
# run/test stages do not need the network.
go mod download || true
go test -run @@NO_MATCH_RUN@@ -count=1 -p 1 @@TEST_CMD_PKGS@@ || true

# Warm them a second time for the POST-FIX module graph: the fix patch rewrites
# go.mod / go.sum (go.mongodb.org/mongo-driver 1.5.0 -> 1.7.3, golang.org/x/net,
# golang.org/x/sys, golang.org/x/tools, plus new indirect requirements). Without
# this the fix stage would be the only stage that has to reach proxy.golang.org.
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true
go mod download || true
go test -run @@NO_MATCH_RUN@@ -count=1 -p 1 @@TEST_CMD_PKGS@@ || true

# Restore the pristine base tree: `checkout --` reverts the tracked files the
# patches modified and `clean -fd` removes the file test.patch adds
# (storage/database/utils_test.go). Neither touches /go/pkg/mod or the build
# cache, so both warm-ups survive.
git checkout -- .
git clean -fdq
bash /home/check_git_changes.sh

"""


_RUN_SH = """#!/bin/bash
set -eo pipefail

@@ENV@@
cd /home/@@REPO@@
@@TEST_CMD@@

"""


_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

@@ENV@@
cd /home/@@REPO@@
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
@@TEST_CMD@@

"""


_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

@@ENV@@
cd /home/@@REPO@@
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
@@TEST_CMD@@

"""


class ProxeusCoreImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        # Per-PR, NOT a shared "base". This layer bakes
        # `git checkout ${BASE_COMMIT}` plus the history-hardening block that
        # prunes every ref not reachable from that commit, so its filesystem is
        # specific to one PR's base.sha and it must never be reused under a tag
        # another PR could resolve to.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Deliberately minimal: no `# syntax` directive, no ARG/proxy/cert/label
        # blocks and no apt layer. DockerfileEnhancer.enhance() injects the
        # BuildKit directive, TARGETARCH / REPO_URL / BASE_COMMIT ARGs, the proxy
        # ARGs + ENV block, the OCI labels and the CA-cert symlinks, and
        # _standardize_repo_fetch() rewrites the bare `git clone` below into
        # clone + WORKDIR + `git reset --hard` + `git checkout ${BASE_COMMIT}` +
        # the history-hardening block + CMD. golang:1.16-bullseye already ships
        # git, gcc and make, which is everything the Go test suite needs.
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ProxeusCoreImageBase(self.pr, self._config)

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
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                _script(_PREPARE_SH, self.pr).replace(
                    "@@TEST_CMD_PKGS@@", _TEST_PKGS
                ),
            ),
            File(
                ".",
                "run.sh",
                _script(_RUN_SH, self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                _script(_TEST_RUN_SH, self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                _script(_FIX_RUN_SH, self.pr),
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


@Instance.register("ProxeusApp", "proxeus-core")
class ProxeusCore(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        # ANSI first: `go test` is not colourised by default, but the log is
        # captured through an interactive bash session and any escape sequence
        # that reached a result line would break every regex below.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Result lines. Subtest results are indented by go test, hence `^\s*`.
        # Only the test name is captured -- the trailing `(0.03s)` duration is
        # left out of the group, so the SAME test yields the SAME name in all
        # three stages (report.py unions names across stages; a duration baked
        # into the name would split one test into three phantom entries).
        re_pass = re.compile(r"^\s*--- PASS:\s+(\S+)")
        re_fail = re.compile(r"^\s*--- FAIL:\s+(\S+)")
        re_skip = re.compile(r"^\s*--- SKIP:\s+(\S+)")

        # Per-package summary lines, which close a package's result block:
        #   ok      github.com/ProxeusApp/proxeus-core/sys/validate  0.031s
        #   ok      github.com/ProxeusApp/proxeus-core/sys/tar       (cached)
        #   FAIL    github.com/ProxeusApp/proxeus-core/storage/database  0.012s
        #   FAIL    github.com/ProxeusApp/proxeus-core/storage/database [build failed]
        #   ?       github.com/ProxeusApp/proxeus-core/storage/mock  [no test files]
        # A bare `FAIL` / `PASS` line carries no package and is not matched.
        re_pkg_end = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+)(?:\s|$)")
        re_pkg_build_fail = re.compile(r"^FAIL\s+\S+\s+\[build failed\]")

        # Module prefix to strip off every package path (see _PKG_SEP). Derived
        # from the PR rather than hardcoded; go.mod at the base commit declares
        # `module github.com/ProxeusApp/proxeus-core`, which is exactly
        # github.com/<org>/<repo>.
        module_path = f"github.com/{self.pr.org}/{self.pr.repo}"
        module_prefix = f"{module_path}/"

        def rel_pkg(pkg: str) -> str:
            """Go import path -> package dir relative to the repo root."""
            if pkg.startswith(module_prefix):
                return pkg[len(module_prefix) :]
            # The module root package itself ("." in `go list` terms). Not in
            # the current test scope, but kept so a future scope change cannot
            # silently produce a name with an empty qualifier.
            if pkg == module_path:
                return "."
            # Anything else (a vendored path, or _UNKNOWN_PKG) is left as-is
            # rather than mangled -- an unrecognised qualifier is still a
            # correct, unique qualifier.
            return pkg

        buffer: list[tuple[str, str]] = []

        def flush(pkg: str) -> None:
            pkg = rel_pkg(pkg)
            for status, name in buffer:
                full = f"{pkg}{_PKG_SEP}{name}"
                if status == "pass":
                    passed_tests.add(full)
                elif status == "fail":
                    failed_tests.add(full)
                elif status == "skip":
                    skipped_tests.add(full)
            buffer.clear()

        for line in test_log.splitlines():
            m = re_pass.match(line)
            if m:
                buffer.append(("pass", m.group(1)))
                continue
            m = re_fail.match(line)
            if m:
                buffer.append(("fail", m.group(1)))
                continue
            m = re_skip.match(line)
            if m:
                buffer.append(("skip", m.group(1)))
                continue
            m = re_pkg_end.match(line)
            if m:
                pkg = m.group(1)
                flush(pkg)
                if re_pkg_build_fail.match(line):
                    # Same repo-relative form as the test names, so the
                    # synthetic entry reads consistently with them.
                    failed_tests.add(f"{_BUILD_FAILED_PREFIX}{rel_pkg(pkg)}")

        flush(_UNKNOWN_PKG)

        # A retried or subtest-bearing name can land in more than one bucket
        # within a single package. Reconcile with failure winning, then skip, so
        # the three sets stay disjoint -- TestResult.__post_init__ raises
        # otherwise and the whole instance run dies.
        passed_tests -= failed_tests | skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
