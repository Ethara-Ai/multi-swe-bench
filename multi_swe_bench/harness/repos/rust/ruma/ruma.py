import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ruma/ruma — the Rust Matrix protocol crates. A 15-crate cargo workspace
# (`crates/*`), edition 2021, MSRV 1.75, tested with the built-in libtest
# harness plus trybuild UI tests.
#
# Discovery (verified in Docker at PR #1932's base sha 54dc4100; every number
# below was measured, not inferred):
#
#   stage 1 (no patches)        1250 passed, 0 failed
#   stage 2 (test.patch)        1001 passed, 0 failed (ruma-events' `it` binary
#                               does not compile; the other 22 binaries do)
#   stage 3 (test + fix.patch)  1252 passed, 0 failed
#
# Stage 2 not compiling is expected and is NOT a defect in the patch split. The
# gold tests reference a type the fix introduces:
#     error[E0432]: unresolved import
#         `ruma_events::sticker::StickerEventContentWithoutRelation`
# In a statically typed language a test for a not-yet-existing type cannot
# compile, so there is no "tests present and failing" state to observe, and the
# two new tests land as n2p rather than f2p.
#
# That is still a SOLVABLE datapoint: the two new tests are added by
# **test.patch** (which adds 2 `#[test]` and zero source changes), while
# fix.patch touches only `crates/ruma-events/src/sticker.rs` and a CHANGELOG.
# At evaluation the gold test patch is applied alongside the model's patch, so a
# model that adds the `relates_to` field makes those tests compile and pass on
# its own merits.
#
# Toolchain, and why it is what it is -- each settled by a real failure first:
#
#  - `rust-toolchain.toml` pins `channel = "nightly-2024-09-06"`. That file is
#    REMOVED before testing, for two independent reasons. First, upstream does
#    the same: `xtask/src/ci.rs` runs its test tasks as
#    `rustup run stable cargo test ...`, so the pinned nightly is for rustfmt
#    and clippy, never for tests. Second, that nightly cannot build the
#    dependency graph at all today (see below), so honouring it would fail
#    outright. This is the opposite call from a repo whose toolchain file says
#    a floating "stable"; here the pin is real but is not the test toolchain.
#
#  - The era-appropriate toolchain does NOT work. `Cargo.lock` is gitignored
#    (.gitignore:3), so there is no lockfile in the committed tree and cargo
#    resolves the newest semver-compatible dependencies. Today that includes
#    `idna_adapter 1.2.2`, which declares `edition = "2024"`. Rust 1.82 (the
#    PR's own era, Oct 2024) cannot parse it:
#        error: failed to parse manifest ... edition2024 ... unstable
#    Edition 2024 needs rustc >= 1.85, so a modern pinned stable is the only
#    combination that resolves and builds. 1.98 was verified end to end.
#
#    Consequence worth knowing: because there is no committed lockfile, the
#    dependency set is whatever crates.io serves at image-build time. Within one
#    image it is frozen -- prepare.sh generates Cargo.lock during the Docker RUN
#    and `git reset --hard` does not remove an ignored untracked file, so all
#    three stages share one resolution -- but it can drift between rebuilds.
#
#  - The test command is upstream's, verbatim. `xtask/src/ci.rs::test_all` is
#        rustup run stable cargo test --tests --features __ci
#    and `__ci` (crates/ruma/Cargo.toml) is
#        ["full", "compat-upload-signatures", "__unstable-mscs", "unstable-unspecified"]
#    It is a feature of the `ruma` meta-crate and must be requested exactly this
#    way -- from the workspace root, so that feature unification sees every
#    default member. Two near-misses were measured and rejected:
#
#      * `--all-features` also enables `compat-encrypted-stickers`, which `__ci`
#        deliberately omits (upstream's other job, `test_compat`, uses
#        `__ci,compat`, and `compat` does not list it either -- so upstream
#        never compiles it). That feature switches this PR's own gold test on to
#        a `#[cfg]` branch whose assertion is wrong upstream: at
#        `crates/ruma-events/tests/it/sticker.rs:225` it expects a body without
#        the "* " replacement-fallback prefix that the very JSON above it
#        carries. Measured: `sticker::replace_content_deserialization` FAILS in
#        the fix stage, so the gold fix appears to fail its own test and the PR
#        loses half its n2p -- and Report.check() does not catch it, because the
#        test is NONE in both earlier stages.
#
#      * Running per crate as `-p ruma -p <pkg> --features ruma/__ci` narrows
#        the selected package set, which narrows feature unification. Measured:
#        1248 result lines instead of 1252, silently dropping
#        `pdu::{de,}serialize_pdu_as_v{1,3}`.
#
#    Two deliberate additions to upstream's command:
#
#      * `--no-fail-fast` — NOT optional. Without it cargo stops after the first
#        failing test binary and the remaining binaries never run. Measured on
#        the unpatched tree: the run collected **670** results and stopped,
#        versus the full suite with the flag. A config missing this flag
#        silently loses half its coverage and looks fine.
#
#      * `-- --skip ::ui` — the trybuild UI tests assert the exact *text* of
#        rustc diagnostics against checked-in .stderr files. Those files were
#        generated with the PR-era compiler, so they mismatch on any rustc new
#        enough to build the dependencies (see above) -- an unavoidable
#        version artefact, not a code defect. `--list` under `__ci` reports
#        exactly five tests whose name contains "::ui" -- `api::ruma_api::ui`,
#        `identifiers::id_macros::ui`, `event::ui`, `event_content::ui` and
#        `event_enums::ui` -- and nothing else, so the skip is precise: other
#        tests containing the letters "ui" (`deserialize_uiaa_info`,
#        `invalid_uint_version`, `required_headers`) have no "::ui" in them.
#
#  - Tests are BUILT by cargo and then EXECUTED DIRECTLY, one binary at a time,
#    rather than run through `cargo test`. This is what keeps stage 2 meaningful.
#    `cargo test` refuses to execute ANY test binary once some target failed to
#    compile, so a workspace-wide `cargo test` in stage 2 -- where
#    `ruma-events`' `it` target cannot build -- collects nothing at all, even
#    though 22 of the 23 test binaries are perfectly fine. `--no-fail-fast` does
#    not help: it governs test failures, not build failures, and `cargo test`
#    rejects `--keep-going` outright ("unexpected argument").
#
#    `cargo build` does accept `--keep-going`, so the runner builds with
#        cargo build --tests --features __ci --keep-going --message-format=json
#    which deterministically produces every binary that can be produced (23 at
#    the base sha, 22 under test.patch), and then runs each one itself. Measured:
#    stage 2 goes from 0 collected to 1001, while stages 1 and 3 reproduce the
#    `cargo test` numbers EXACTLY (1250 and 1252, zero failures, identical test
#    ids) -- which is the evidence that the runner environment below is a
#    faithful stand-in for cargo's own.
#
#    To be that stand-in, each binary is launched the way cargo launches it:
#      * cwd = the package root (from the JSON `manifest_path`), because tests
#        that open fixture files use paths relative to their own crate
#      * CARGO_MANIFEST_DIR set to the same directory
#      * LD_LIBRARY_PATH including `rustc --print target-libdir`, without which
#        the proc-macro crate's test binary dies with
#            error while loading shared libraries: libstd-<hash>.so
#        and `ruma-macros`' two tests (`serde::case::rename_fields`,
#        `serde::case::rename_variants`) vanish silently. That lookup MUST run
#        after rust-toolchain.toml is deleted, or rustup resolves the pinned
#        nightly and returns the wrong toolchain's libdir.
#
#    Running the binaries directly also makes `--no-fail-fast` unnecessary --
#    each binary is its own process, so one failing suite cannot stop the rest --
#    and it makes the test id exact for free: the JSON reports each target's
#    `src_path`, which the runner prints in a `##### BIN:` marker ahead of that
#    binary's output. libtest never says which file a test came from, and
#    cargo's own `Running tests/it/main.rs (...deps/it-<hash>)` is ambiguous
#    twice over (the path is relative to its package, and four crates build a
#    target from `tests/it/main.rs`), so the marker is the only stable source of
#    that information. Measured on all three stages: raw result lines == unique
#    ids, and every id resolves to a real repository path.


class RumaImageBase(Image):
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
        # Pinned rather than a floating tag, and modern rather than
        # era-matching: the PR-era 1.82 cannot parse the edition-2024
        # dependency the lock-free manifest now resolves to. Publishes
        # linux/amd64 and linux/arm64.
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

        # No apt layer: `rust:1.98-bookworm` already ships git and
        # ca-certificates (verified in the built image), and nothing in this
        # workspace links against a C library that would need -dev headers.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class RumaImageDefault(Image):
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
        return RumaImageBase(self.pr, self._config)

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

        # RAW string: this body is pure shell. `\(`, `\.`, `\3` and the trailing
        # `\` line-continuations must reach bash exactly as written -- in a
        # normal string Python would turn `\3` into a control character and
        # swallow the continuations outright.
        run_tests = r"""#!/bin/bash
# Builds this PR's tests, runs each test binary directly, and prints libtest
# output for parse_log.
# `set -e` is deliberately absent HERE (the three stage wrappers do use it): a
# failing test, and a crate that will not compile until fix.patch lands, are
# both expected outcomes of one stage or another and must still reach the
# verdict below.
set -uo pipefail
cd /home/__REPO__

export CI=true
export CARGO_TERM_COLOR=never
TEST_TIMEOUT="${RU_TEST_TIMEOUT:-3600}"
# Only the stages that are supposed to measure the whole suite set this; see
# section 4. Stage 2 loses one binary by design, so it leaves it at 0.
MIN_RESULTS="${RU_MIN_RESULTS:-0}"

# The repo pins `channel = "nightly-2024-09-06"` in rust-toolchain.toml. Remove
# it: upstream's own CI runs its test tasks as `rustup run stable cargo test`,
# so that pin governs rustfmt/clippy rather than tests, and the nightly it names
# cannot parse the edition-2024 dependency this lock-free manifest resolves to.
# This is a working-tree edit rather than a build-time deletion because
# `git reset --hard` in prepare.sh restores it and each stage starts from the
# committed tree.
rm -f rust-toolchain.toml
echo "RU RUNNER: removed rust-toolchain.toml (pins nightly-2024-09-06; CI tests on stable)"
echo "RU RUNNER: toolchain = $(rustc --version)"

# Proc-macro test binaries link libstd dynamically; cargo normally sets this for
# them. Without it `ruma-macros` dies with "error while loading shared
# libraries: libstd-<hash>.so" and its two tests vanish silently. This MUST come
# after the rm above -- with rust-toolchain.toml still present, rustup resolves
# the pinned nightly and hands back the wrong toolchain's libdir.
export LD_LIBRARY_PATH="$(rustc --print target-libdir):/home/__REPO__/target/debug/deps${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
echo "RU RUNNER: target-libdir = $(rustc --print target-libdir)"

# ------------------------------------------------------- 1. build
# Upstream's own feature selection, verbatim -- `xtask/src/ci.rs::test_all` is
# `cargo test --tests --features __ci`, requested from the workspace root so
# feature unification sees every default member. Do not substitute
# `--all-features` (it adds compat-encrypted-stickers, which breaks this PR's
# gold test) or a per-crate `-p` form (which narrows unification and drops four
# tests). Both were measured; see the header.
#
# `--keep-going` is why stage 2 still measures something: it builds every target
# it can instead of abandoning the run at the first compile error. `cargo test`
# does not accept this flag, which is precisely why the binaries are executed
# directly in section 2 rather than through cargo.
cargo build --tests --features __ci --keep-going --message-format=json \
  > /tmp/ru-artifacts.json 2> /tmp/ru-build.err
build_rc=$?
echo "RU RUNNER: cargo build rc=$build_rc (non-zero is expected in the test.patch stage)"

# Each compiler-artifact line carries the target's absolute src_path and the
# executable it produced, so one pass yields both the run list and the exact
# source file for every binary. No hashes, no guessing.
sed -n 's|.*"manifest_path":"\([^"]*\)".*"src_path":"\([^"]*\)".*"executable":"\([^"]*\)".*|\3 \2 \1|p' \
  /tmp/ru-artifacts.json | sed 's|/home/__REPO__/||g' | sort > /tmp/ru-bins.txt
echo "RU RUNNER: $(wc -l < /tmp/ru-bins.txt) test binaries built"

# `tests/it/main.rs` declares exactly one `mod` per sibling file, so a test
# named `sticker::content_serialization` provably lives in `tests/it/sticker.rs`.
# Emit the candidate files so parse_log can refine those ids against reality
# instead of assuming the file exists.
echo '##### FILEMAP-BEGIN'
find crates -path '*/tests/*' -name '*.rs' 2>/dev/null | sed 's|^\./||' | sort
echo '##### FILEMAP-END'

# ------------------------------------------------------- 2. run
# One binary at a time, launched the way cargo launches it: cwd at the package
# root and CARGO_MANIFEST_DIR to match, so tests that open fixture files by
# relative path behave identically. `--skip ::ui` drops the trybuild tests,
# which assert the exact text of rustc diagnostics against .stderr files
# generated by the PR-era compiler and cannot match on any rustc new enough to
# build this dependency graph. `--list` under `__ci` reports exactly five tests
# containing "::ui" and nothing else, so the skip is precise.
: > /tmp/ru-test.log
while read -r exe src manifest; do
  pkgdir=$(dirname "$manifest")
  {
    echo "##### BIN: $exe $src"
    ( cd "/home/__REPO__/$pkgdir" \
        && CARGO_MANIFEST_DIR="/home/__REPO__/$pkgdir" \
           timeout --kill-after=60 "$TEST_TIMEOUT" \
           "/home/__REPO__/$exe" --skip ::ui 2>&1 )
    brc=$?
    if [ "$brc" -eq 124 ] || [ "$brc" -eq 137 ]; then
      echo "##### RUNNER-TIMEOUT: $exe"
    fi
    echo
  } >> /tmp/ru-test.log
done < /tmp/ru-bins.txt

echo '===== BEGIN TEST RESULTS ====='
cat /tmp/ru-test.log
echo '===== END TEST RESULTS ====='

# ------------------------------------------------------- 3. accounting
results=$(grep -c '^test .* \.\.\. ' /tmp/ru-test.log)
timeouts=$(grep -c '^##### RUNNER-TIMEOUT:' /tmp/ru-test.log)
echo "RU RUNNER: $results result lines collected, $timeouts binaries timed out"

# ------------------------------------------------------- 4. verdict
# A crate that will not compile is a legitimate measurement for stage 2 -- it is
# exactly why the two gold tests land as n2p rather than f2p -- so a non-zero
# build rc is never escalated.
#
# What IS escalated is a stage that measured nothing when it was supposed to
# measure the whole suite. Without this, a crates.io outage or a broken
# toolchain during stage 1 would leave the baseline empty, every test in the fix
# stage would look like a NONE->PASS transition, and Report.check() would accept
# the corrupt result as valid -- the one failure mode here that is silent rather
# than loud. run.sh and fix-run.sh set the floor; test-run.sh must not.
if [ "$timeouts" -ne 0 ]; then
  echo "RU RUNNER: INFRASTRUCTURE FAILURE: $timeouts binaries hit the ${TEST_TIMEOUT}s cap;"
  echo "RU RUNNER: the results above are not trustworthy"
  exit 1
fi

if [ "$results" -lt "$MIN_RESULTS" ]; then
  echo "RU RUNNER: INFRASTRUCTURE FAILURE: collected $results result lines,"
  echo "RU RUNNER: expected at least $MIN_RESULTS for this stage. Build log tail:"
  tail -40 /tmp/ru-build.err
  exit 1
fi

exit 0
""".replace("__REPO__", repo)

        prepare = """#!/bin/bash
set -e
export CI=true
export CARGO_TERM_COLOR=never
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git config core.autocrlf input
git config core.filemode false

git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the cargo registry and build the whole test graph at the base sha, with
# the same feature selection the stages use, so the three stages do not each
# compile ~200 crates from scratch and a crates.io outage mid-stage is far less
# likely to leave a stage with nothing collected. `|| true` because a dependency
# that fails to build on one architecture must not abort the image build.
rm -f rust-toolchain.toml
timeout --kill-after=60 7200 cargo build --tests --features __ci --keep-going || true

# Rehearse this PR's own test selection once, in the same environment the
# stages use.
bash /home/run_tests.sh > /home/prepare-warmup.log 2>&1 || true
tail -20 /home/prepare-warmup.log || true

# target/, Cargo.lock and the cargo registry are all either outside the work
# tree or gitignored, so this reset leaves the warm-up intact while returning
# the tree to a clean base sha -- which is what lets test.patch/fix.patch apply
# cleanly. The reset also restores rust-toolchain.toml; run_tests.sh removes it
# again in each stage.
git reset --hard
bash /home/check_git_changes.sh
""".replace("__REPO__", repo).replace("__SHA__", self.pr.base.sha)

        # `set -e` in the three wrappers is load-bearing: without it a failing
        # `git apply` does not stop the script, the stage measures the wrong
        # tree, and the PR's own tests are misclassified in a log that looks
        # perfectly healthy.
        #
        # RU_MIN_RESULTS is the floor described in run_tests.sh section 4. Stages
        # 1 and 3 both compile and run the entire suite (measured 1250 and 1252
        # result lines), so 1000 leaves generous room for dependency drift while
        # still catching a stage that collected nothing or a small fraction.
        # test-run.sh deliberately has no floor: ruma-events does not compile
        # there, and with a workspace-wide invocation that means zero results.
        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
export RU_MIN_RESULTS=1000

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
export RU_MIN_RESULTS=1000

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes),
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


@Instance.register("ruma", "ruma")
class Ruma(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RumaImageDefault(self.pr, self._config)

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

        # 1. FILEMAP: the integration-test source files that actually exist, used
        #    only to refine the shared `tests/it/main.rs` targets below.
        files: set[str] = set()
        in_map = False
        for line in clean.splitlines():
            s = line.strip()
            if s == "##### FILEMAP-BEGIN":
                in_map = True
                continue
            if s == "##### FILEMAP-END":
                in_map = False
                continue
            if in_map and s.endswith(".rs"):
                files.add(s)

        # run_tests.sh prints one marker per test binary, carrying that target's
        # source file exactly as cargo reported it in the build's JSON stream.
        # That is the whole id mechanism -- no hashes, and nothing inferred from
        # cargo's own `Running` line, which is relative to its package and
        # therefore ambiguous across the four crates that build `tests/it/main.rs`.
        bin_re = re.compile(r"^##### BIN:\s+\S+\s+(\S+\.rs)$")
        # libtest: "test some::path::name ... ok" (and FAILED / ignored).
        # A trailing "- should panic" is part of the name libtest prints, so the
        # name is captured up to the " ... " separator rather than to the first
        # space.
        res_re = re.compile(r"^test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)\b")

        src = ""
        for line in clean.splitlines():
            m = bin_re.match(line.rstrip())
            if m:
                src = m.group(1)
                continue

            m = res_re.match(line)
            if not m:
                continue
            name, status = m.group(1).strip(), m.group(2)

            # A result line with no BIN marker ahead of it cannot be attributed
            # to a file, and an unattributed id would collide across crates.
            # Dropping it is the safe direction: it then reads as NONE in every
            # stage and can never manufacture a transition. If the markers were
            # missing wholesale the fix stage would come back empty and
            # Report.check() rule 1 rejects the instance loudly.
            if not src:
                continue

            test_id = f"{src}::{name}"
            # `tests/it/main.rs` declares one `mod` per sibling file, so the
            # first segment of an integration test's path names its file. Refine
            # to that file when it really exists; otherwise keep main.rs, which
            # is already correct, just coarser.
            if src.endswith("/tests/it/main.rs"):
                refined = src[: -len("main.rs")] + name.split("::", 1)[0] + ".rs"
                if refined in files:
                    test_id = f"{refined}::{name}"

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
