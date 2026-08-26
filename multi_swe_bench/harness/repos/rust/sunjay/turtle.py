import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# sunjay/turtle — a Rust teaching crate ("learn Rust by creating animated
# drawings"), edition 2018, tested with the built-in libtest harness.
#
# Discovery (verified in Docker at PR #173's base sha bf64b833; every number
# below was measured, not inferred):
#
#   stage 1 (no patches)        125 passed, 0 failed
#   stage 2 (test.patch)        DOES NOT COMPILE -> 0 tests collected
#   stage 3 (test + fix.patch)  130 passed, 0 failed
#
# (Doctests are deliberately excluded from the run -- see the --lib --bins
# --tests note in run_tests.sh. With them included the counts are 262 and 271,
# but their ids are not stable across stages.)
#
# Stage 2 failing to compile is a property of THIS PR, not of the config:
# test.patch deletes src/renderer_process/test.rs, but the `mod test;` that
# refers to it lives in a file only fix.patch rewrites, so the tree is
# inconsistent until both patches are applied:
#     error[E0583]: file not found for module `test`
#     error[E0432]: unresolved import `crate::renderer_process::RendererProcess`
# The practical consequence is that no test can be FAIL->PASS (f2p = 0): every
# test is absent in stage 2 and passes in stage 3, i.e. NONE->PASS (n2p).
# PR #173 is "Rewrite internals of turtle crate" -- 83 files, a full swap of the
# piston_window renderer for glutin + pathfinder + an IPC/tokio architecture --
# and a rewrite of that size simply has no separable "tests only" state.
#
# It is also worth recording that test.patch contains ZERO #[test] functions.
# Its 8 files are examples/*.rs plus modules merely NAMED test.rs (a mock
# rendering backend). The crawler classified them as tests by filename. The real
# suite is 125 #[test] functions in src/ at the base sha, which the *fix* patch
# grows to 130.
#
# Toolchain, and why it is what it is -- each of these was a real failure first:
#
#  - `rust-toolchain` in the repo contains "stable", and rustup honours it over
#    whatever the image ships: inside rust:1.43 it silently downloaded and used
#    1.98. Upstream CI does `rm rust-toolchain` before installing its own
#    toolchain for exactly this reason, and run_tests.sh does the same. Without
#    it the pinned base image is decorative.
#
#  - The era-appropriate toolchain does NOT work. Rust 1.43 (May 2020, matching
#    the PR) cannot build the dependency graph at all: the crate pins
#    `serde_json = "1.0"`, there is no Cargo.lock in the tree, so cargo resolves
#    to today's serde_json 1.0.143, which declares edition 2021 ->
#    "supported edition values are `2015` or `2018`, but `2021` is unknown".
#    Cargo 1.43 also cannot fetch the modern crates.io index through its bundled
#    libgit2 ("error reading from the zlib stream"). A modern pinned toolchain
#    is the only combination that resolves and builds, and 1.98 was verified end
#    to end.
#
#  - RUSTFLAGS="--cap-lints=allow" is REQUIRED, not hygiene. src/lib.rs:78 has
#    `#![deny(unused_must_use)]`, and modern rustc marks more `From` impls
#    #[must_use], so 2020 test code like `Color::from("#fffff")` becomes a hard
#    error and the whole build fails. --cap-lints overrides source-level deny
#    attributes (the mechanism docs.rs uses to build old crates); a plain
#    `-A unused_must_use` does NOT, because source attributes outrank it.
#
#  - The test command is `cargo test --features "test unstable" --all`.
#    Cargo.toml states outright that "tests MUST be run with cargo test
#    --features test" -- the `test` feature swaps in a headless mock backend,
#    which is why a GUI crate is testable in a container at all. `unstable` is
#    needed because examples/loadtest.rs (added by test.patch) fails to compile
#    without it. This single command is what THIS PR changes CI to, and it also
#    works unmodified at the base sha (verified), so all three stages can share
#    one command as the harness requires.
#
#  - The GL/X11 -dev packages are needed to BUILD, even though nothing opens a
#    window: piston_window at the base sha and glutin/pathfinder after the fix
#    both link against them regardless of which features are active.


class TurtleImageBase(Image):
    # Level 1. dependency() is a string AND this dockerfile carries the clone,
    # so DockerfileEnhancer rewrites the clone into the standard
    # REPO_URL/BASE_COMMIT form plus Image._HARDENING_BLOCK, and prepends the
    # syntax directive, TARGETARCH/proxy ARGs, ENV block, labels and cert
    # symlinks. None of that is hand-written here.
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
        # Pinned rather than `stable`, so the image is reproducible; bookworm
        # rather than the era-matching rust:1.43, which is Debian buster and now
        # EOL (apt fails until sources are rewritten to archive.debian.org).
        # Publishes linux/amd64 and linux/arm64.
        return "rust:1.98-bookworm"

    def image_tag(self) -> str:
        # Per-PR: the enhancer bakes `git checkout ${BASE_COMMIT}` into this
        # image, so a shared "base" tag would let a second PR of this repo
        # inherit the first PR's pinned tree.
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    pkg-config cmake libx11-dev libxi-dev libxcursor-dev libxrandr-dev \\
    libxinerama-dev libgl1-mesa-dev libfreetype6-dev libexpat1-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class TurtleImageDefault(Image):
    # Level 2 -- per-PR scripts. dependency() is an Image, so the enhancer
    # returns this dockerfile verbatim; everything needing REPO_URL/BASE_COMMIT
    # already happened in the base.
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
        return TurtleImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        check_git_changes = """#!/bin/bash
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

        cargo_env = """#!/bin/bash
# Sourced by prepare.sh and run_tests.sh, so the warm-up build and the three
# stages compile under identical settings -- a value that drifted between them
# would stop the warm-up rehearsing what the stages actually do.

# src/lib.rs:78 is `#![deny(unused_must_use)]`, and a modern rustc marks more
# `From` impls #[must_use], so 2020-era test code such as
# `Color::from("#fffff")` becomes a hard error and nothing builds. --cap-lints
# overrides source-level deny attributes; `-A unused_must_use` does not, because
# attributes in the source outrank a command-line allow.
export RUSTFLAGS="--cap-lints=allow"

# Harmless on a modern cargo (which uses the sparse HTTP index), and the escape
# hatch if this config is ever re-pointed at an older toolchain whose bundled
# libgit2 cannot fetch today's crates.io index.
export CARGO_NET_GIT_FETCH_WITH_CLI=true

export CARGO_TERM_COLOR=never
"""

        run_tests = """#!/bin/bash
# Runs this PR's tests and prints cargo's output for parse_log.
# `set -e` is deliberately absent HERE (the three stage wrappers do use it):
# failing tests, and a tree that does not compile until fix.patch lands, are
# both expected outcomes of one stage or another and must still reach the
# report below.
set -uo pipefail
cd /home/__REPO__

export CI=true
source /home/cargo_env.sh
TEST_TIMEOUT="${TU_TEST_TIMEOUT:-3600}"

# The repo's `rust-toolchain` file contains "stable", and rustup honours it over
# the toolchain the image pins -- inside rust:1.43 it silently fetched and used
# 1.98. Upstream CI removes the file for the same reason. This is a working-tree
# edit rather than a build-time deletion because `git reset --hard` in prepare.sh
# would restore it, and each stage starts from the committed tree.
rm -f rust-toolchain
echo "TU RUNNER: removed rust-toolchain (pins 'stable'; would override the image toolchain)"
echo "TU RUNNER: toolchain = $(rustc --version)"

# One command for all three stages, which is what makes their logs comparable.
# --features "test": Cargo.toml states tests MUST run with it; it swaps in the
#   headless mock backend that lets a GUI crate be tested in a container.
# --features "unstable": kept in step with what this PR changes CI to, and
#   needed by examples/loadtest.rs if example targets are ever re-included.
# --no-fail-fast: one failing target must not hide the results of the others.
# --lib --bins --tests: run the unit/integration suites but NOT doctests.
#   This is a deliberate, measured exclusion. A Rust doctest's only identity is
#   its source position -- libtest names it "src/color.rs - color (line 103)" --
#   so when a patch edits that file every line below the edit shifts and an
#   UNCHANGED doctest reappears under a new id. Measured on this PR: of 137
#   doctest ids at the base sha, only 24 survived into the fix stage, while 113
#   vanished and 117 appeared, none of which reflects a real test changing
#   state. The harness unions test names across the three stages, so that churn
#   would manufacture phantom PASS->NONE and NONE->PASS transitions and corrupt
#   the f2p/p2p classification. Dropping the line number instead is worse: it
#   collides, because one file routinely holds many doctests under a single item
#   path (262 real ids collapse toward ~45), which breaks uniqueness WITHIN a
#   stage rather than just across stages. Excluding doctests costs coverage --
#   137 of 262 tests here -- but the 125 unit tests that remain are stable:
#   124 of 125 ids persist across run->fix, and the one that does not is a test
#   the rewrite genuinely deletes.
echo "TU RUNNER: cargo test --lib --bins --tests --features \\"test unstable\\" --all --no-fail-fast"
echo "TU RUNNER: NOT RUN: doctests (--doc). Their ids embed source line numbers,"
echo "TU RUNNER: NOT RUN: which shift whenever a patch edits the file, so the same"
echo "TU RUNNER: NOT RUN: doctest is unmatchable across stages. See the config."
echo '===== BEGIN TEST RESULTS ====='
timeout --kill-after=60 "$TEST_TIMEOUT" \\
  cargo test --lib --bins --tests --features "test unstable" --all --no-fail-fast 2>&1
rc=$?
echo '===== END TEST RESULTS ====='

# cargo exits 101 both for "tests failed" and for "did not compile". Those are
# different things, but both are legitimate measurements for some stage of this
# PR (stage 2 genuinely does not compile), so neither is escalated here. Only a
# timeout means the run measured nothing at all.
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  echo "TU RUNNER: INFRASTRUCTURE FAILURE: hit the ${TEST_TIMEOUT}s cap;"
  echo "TU RUNNER: the results above are not trustworthy"
  exit "$rc"
fi
if [ "$rc" -ne 0 ]; then
  echo "TU RUNNER: cargo exited $rc (test failure or compile failure -- a result)"
fi
exit 0
""".replace("__REPO__", repo)

        prepare = """#!/bin/bash
set -e
export CI=true
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git config core.autocrlf input
git config core.filemode false
source /home/cargo_env.sh

git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the cargo registry and build the dependency graph at the base sha, so the
# three stages do not each pay a cold build of ~140 crates (glutin, pathfinder,
# tokio and friends) and so a crates.io outage mid-stage is far less likely to
# leave a stage with nothing collected. `|| true` because a native-dependency
# build failure on one architecture must not abort the image build.
rm -f rust-toolchain
timeout --kill-after=60 3600 cargo test --lib --bins --tests \\
  --features "test unstable" --all --no-run || true

# Rehearse this PR's own test run once, in the same environment the stages use,
# and prove the headless `test` feature really works on this platform.
bash /home/run_tests.sh > /home/prepare-warmup.log 2>&1 || true
tail -20 /home/prepare-warmup.log || true

# target/ and the cargo registry live outside the work tree, so the warm-up
# survives this reset while the tree goes back to a clean base sha -- which is
# what lets test.patch/fix.patch apply cleanly. The reset also restores
# rust-toolchain; run_tests.sh removes it again in each stage.
git reset --hard
bash /home/check_git_changes.sh
""".replace("__REPO__", repo).replace("__SHA__", self.pr.base.sha)

        # `set -e` in the three wrappers is load-bearing: without it a failing
        # `git apply` does not stop the script, the stage measures the wrong
        # tree, and the PR's own tests are misclassified in a log that looks
        # perfectly healthy.
        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "cargo_env.sh", cargo_env),
            File(".", "run_tests.sh", run_tests),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("sunjay", "turtle")
class Turtle(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TurtleImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi = re.compile(r"\x1B\[[0-?9;]*[mK]")
        clean = ansi.sub("", test_log)

        # libtest emits two shapes, and they must both be captured:
        #   unit test : "test color::tests::check_complement ... ok"
        #   doctest   : "test src/color.rs - color (line 120) ... ok"
        # The `(\\S+)` pattern used by most Rust configs in this registry is
        # WRONG for a crate with doctests: it stops at the first space, so every
        # doctest in one file collapses to the single id "src/color.rs". This
        # repo has 137 doctests, so that would silently merge them into a
        # handful of ids and destroy the per-test signal. Capture everything
        # between "test " and " ... " instead, non-greedily.
        line_re = re.compile(
            r"^test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)\b", re.M
        )

        for m in line_re.finditer(clean):
            name = m.group(1).strip()
            status = m.group(2)

            # A doctest name already carries its source file, as
            # "src/color.rs - color (line 120)". Normalise that to the
            # "<file>::<test>" shape the rest of the corpus uses, so
            # report.py's _test_name_matches_files can split on the first "::"
            # and resolve the head against a patch-touched path. A unit test
            # name is a Rust module path with no file information anywhere in
            # libtest's output, so it is left as-is rather than guessing a file
            # from the module path -- `foo::bar` may live in src/foo/bar.rs or
            # in src/foo.rs, and inventing the wrong one is worse than omitting.
            doc = re.match(r"^(\S+\.rs)\s+-\s+(.*)$", name)
            if doc:
                test_id = f"{doc.group(1)}::{doc.group(2).strip()}"
            else:
                test_id = name

            if status == "ok":
                passed_tests.add(test_id)
            elif status == "FAILED":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        # Keep the buckets disjoint. Failure wins: crediting a test that was
        # ever seen failing as passed is the unsafe direction.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
