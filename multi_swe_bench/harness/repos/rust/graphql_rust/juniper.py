import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ─────────────────────────────────────────────────────────────────────────────
# graphql-rust/juniper — GraphQL server library for Rust.
#
# SYSTEM DEPENDENCIES: none beyond the official `rust:` image. The packages this
# config selects (`juniper`, `juniper_codegen`, `juniper_tests`) resolve to a
# dependency graph that is pure Rust end to end — serde, futures, indexmap,
# graphql-parser, chrono, bson, url, uuid, tokio 0.2. There is no `-sys` crate,
# no `bindgen`/`cmake` build script and no `pkg-config` probe anywhere in it, so
# the `buildpack-deps:bookworm` base underneath `rust:` (which already ships
# git, curl, ca-certificates and a full C toolchain) needs nothing added.
#
# MULTI-ARCH: with no prebuilt binary fetched and no C library linked there is
# nothing arch-specific to branch on — no `[arch=...]` apt pin, no arch-suffixed
# tarball download, no TARGETARCH conditional.
#
# Verified green on BOTH linux/amd64 and linux/arm64, not just reasoned about: a
# `docker buildx build --platform linux/amd64,linux/arm64` produced a 2-entry OCI
# image index for each of the two images, and the arm64 variant was then loaded
# and actually executed under emulation. Its baseline stage reports 871 passed /
# 0 failed and parse_log yields a test-id set *identical* to amd64's (0 ids on
# either side only) — so nothing here is endian-, SIMD- or pointer-width
# sensitive, and the graded ids do not shift with the architecture.
# ─────────────────────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────────────────────
# PACKAGE SELECTION
#
# The workspace at the base commit has 18 members, but only three are reachable
# from the two gold patches:
#
#   * `juniper`          — test.patch adds 5 tests to juniper/src/macros/tests/args.rs
#   * `juniper_tests`    — test.patch adds 1 test to
#                          integration_tests/juniper_tests/src/codegen/impl_object.rs
#   * `juniper_codegen`  — fix.patch edits juniper_codegen/src/util/mod.rs; its own
#                          12 `util::test::*` unit tests cover that module directly
#                          and act as the regression guard on the fix.
#
# `--workspace` is NOT usable here. `juniper_rocket` pins `rocket = "0.4.2"`,
# whose build chain is nightly-only; measured in-container on the pinned stable
# toolchain, `cargo test -p juniper_rocket --no-run` dies with
#
#   error: failed to run custom build command for `pear_codegen v0.1.5`
#     Error: Pear requires a 'dev' or 'nightly' version of rustc.
#
# so a workspace-wide `cargo test` cannot compile at all. The remaining members
# (juniper_warp / _actix / _hyper / _iron / _graphql_ws / _subscriptions and the
# examples) pull 2020-era web frameworks whose modern transitive releases are a
# standing bit-rot risk; they contribute no graded signal and are left out
# deliberately.
#
# `integration_tests/codegen_fail` (package `juniper_codegen_tests`) is excluded
# for the same reason plus one of its own: it is a `trybuild` compile-fail suite,
# and trybuild asserts against expected rustc stderr, which is not stable across
# compiler versions — it would report spurious failures on any toolchain but the
# one its .stderr fixtures were generated against.
#
# `--tests` selects the lib unit-test targets and excludes doctests, whose result
# lines carry a source line number ("... - executor::FieldError (line 141) ...").
# That number is metadata that is not part of a test's identity, and excluding
# doctests keeps test ids free of it entirely.
#
# `--no-fail-fast` is load-bearing. Without it cargo stops at the first test
# binary that fails, and at the TEST stage `juniper` fails — so the rest would
# never run and their tests would read NONE in that stage while reading PASS in
# run and fix.
# ─────────────────────────────────────────────────────────────────────────────
_CARGO_PACKAGES = ("juniper", "juniper_codegen", "juniper_tests")

# Emitted by run-tests.sh on STDOUT immediately before each package's cargo
# invocation, and the ONLY thing parse_log uses to attribute a test to a crate.
_PKG_MARKER_PREFIX = "### harness: package "
_PKG_MARKER_SUFFIX = " ###"

# ─────────────────────────────────────────────────────────────────────────────
# DEPENDENCY PINNING
#
# juniper does NOT commit a Cargo.lock (it is listed in .gitignore), so a bare
# `cargo test` resolves every dependency to the newest semver-compatible release
# *at build time*. For a tree frozen in November 2020 that is not merely
# non-reproducible, it does not compile:
#
#   error[E0599]: no variant or associated item named `__Nonexhaustive` found
#                 for enum `syn::Type`
#     --> juniper_codegen/src/common/parse/mod.rs:199:18
#
# syn replaced its `__Nonexhaustive` enum variants with `#[non_exhaustive]` in
# 1.0.58; juniper_codegen matches on them, so any syn newer than 1.0.57 breaks
# the build of the very crate the fix patch edits.
#
# The pins below are the minimal set that makes the tree build, each one
# measured in-container rather than guessed:
#
#   syn@1  -> 1.0.57  REQUIRED. Last syn 1.x that still exposes `__Nonexhaustive`.
#   ctor@0.1 -> 0.1.16
#                     REQUIRED as a consequence of the above. ctor is a
#                     transitive dev-dependency (juniper -> pretty_assertions
#                     0.6.1 -> ctor); ctor >= 0.1.20 demands `syn = "^1.0.98"`,
#                     which makes the syn pin unsatisfiable. 0.1.16 is
#                     era-appropriate and needs only syn 1.0.x.
#   url@2  -> 2.5.0   STABILITY. url >= 2.5.1 moved to idna 1.x, which drags in
#                     the icu4x stack (icu_normalizer / icu_properties /
#                     icu_provider / icu_collections ...). Those crates raise
#                     their MSRV aggressively — at the time of writing they had
#                     already reached "requires rustc 1.88" — so leaving url
#                     unpinned makes the image build's success depend on how
#                     recently icu4x published. Pinning to 2.5.0 keeps idna 0.5
#                     and removes all 7 icu crates from the graph, which drops
#                     the graph's effective MSRV far below the pinned toolchain
#                     and leaves real headroom.
#
# The pkgid specs use a PARTIAL version (`syn@1`, not `syn@1.0.109`): the
# workspace graph contains four distinct `syn` majors (0.15, 1.x, 2.x, 3.x) and
# three `url`/`idna` lines, so a bare `-p syn` is ambiguous and cargo refuses it —
# while a fully-qualified `syn@1.0.109` would silently stop matching the moment
# resolution picks a different patch release. `syn@1` disambiguates the major
# without over-constraining the patch.
#
# None of these carry `|| true`: a pin that cannot be applied means the
# dependency graph has drifted out from under this config, and that must fail
# the image build loudly instead of silently producing a tree that will not
# compile and a 0/0/0 report.
# ─────────────────────────────────────────────────────────────────────────────
_PIN_DEPS_SH = """#!/bin/bash
# Generates a Cargo.lock and pins the dependencies that a 2020 tree cannot
# build against at their current releases. The caller has already cd'ed into the
# repository. See the DEPENDENCY PINNING note in the config for why each pin
# exists and why the pkgid specs use a partial version.
set -eo pipefail

export CARGO_TERM_COLOR=never
export CARGO_NET_RETRY=5

# Start from a resolution this script fully controls rather than whatever a
# previous stage may have left behind.
rm -f Cargo.lock
cargo generate-lockfile

# ORDER IS LOAD-BEARING: ctor must come first. The freshly generated lock holds
# ctor 0.1.26, which requires `syn = "^1.0.98"`, so pinning syn to 1.0.57 while
# that ctor is still locked aborts with
#   error: failed to select a version for the requirement `syn = "^1.0.98"`
#          candidate versions found which didn't match: 1.0.57
#          required by package `ctor v0.1.26`
# Downgrading ctor first drops that constraint and lets the syn pin resolve.
cargo update -p ctor@0.1 --precise 0.1.16
cargo update -p syn@1 --precise 1.0.57
cargo update -p url@2 --precise 2.5.0

# Fail the image build here, not three stages later, if a pin did not stick.
grep -q 'version = "1.0.57"' Cargo.lock
grep -q 'version = "0.1.16"' Cargo.lock
grep -q 'version = "2.5.0"' Cargo.lock
if grep -q 'name = "icu' Cargo.lock; then
    echo "### harness: icu crates unexpectedly present in the lock ###"
    exit 1
fi

echo "### harness: dependency pins applied ###"
"""

# ─────────────────────────────────────────────────────────────────────────────
# Shared test runner, used verbatim by run.sh / test-run.sh / fix-run.sh so the
# three graded stages cannot drift apart.
#
# WHY ONE CARGO INVOCATION PER PACKAGE, AND WHY THE MARKER
#
# All three packages emit the SAME libtest header text — `Running unittests
# src/lib.rs (target/debug/deps/<crate>-<hash>)` — so the crate name is the only
# thing that separates them, and a test id has to carry it.
#
# That header goes to cargo's STDERR while the `test <name> ... ok` lines go to
# its STDOUT. The harness runs each stage as `bash /home/run.sh >> <log> 2>&1`
# (build_dataset.py:771), which merges two independently-buffered streams into
# one file, and the merge is NOT order-preserving. Measured in a real pipeline
# run of this very instance, run.log came out as:
#
#     928: Running unittests ... (deps/juniper-327f4c57f3a69539)
#    1540: test result: ok. 608 passed ...
#    1542: Running unittests ... (deps/juniper_codegen-e2dda22b5625bb79)
#    1558: test result: ok. 12 passed ...
#    1559..1814: <juniper_tests' 251 result lines>
#    1816: Running unittests ... (deps/juniper_tests-d3e50e5a25674744)
#
# — the `juniper_tests` header arrived 250 lines AFTER the tests it announces.
# Attributing tests to the most recent header therefore labelled all 251 of them
# `juniper_codegen::...` in the run stage while the fix stage, which happened to
# interleave correctly, labelled them `juniper_tests::...`. The same test then
# appears under two different names across stages, which is precisely the
# cross-stage inconsistency that produces phantom NONE entries and silently
# corrupts the f2p/p2p split. The resulting report still reported `valid: true`
# — with `fixed_tests` inflated from 5 to 17.
#
# The fix is to stop relying on cargo's stream ordering. Each package gets its
# own cargo invocation, preceded by a marker this shell writes to STDOUT itself.
# Ordering is then guaranteed by process semantics rather than by buffering: the
# marker's write(2) completes before cargo is spawned, and cargo has exited —
# flushing both its streams — before the next iteration's marker is written.
# parse_log keys off that marker and ignores cargo's own headers entirely.
#
# Deliberately no `|| true` on the cargo line: a failure is recorded in $rc and
# re-raised by `exit $rc`, so a test failure still propagates to the caller. If
# cargo failed to START (missing target, unresolvable dependency) that must
# surface as a failed stage, not as an empty log that parse_log turns into a
# 0/0/0 TestResult. `set -e` is deliberately NOT used: every package must run
# even after an earlier one fails, which is what keeps the three stages
# comparable.
# ─────────────────────────────────────────────────────────────────────────────
_RUN_TESTS_SH = (
    """#!/bin/bash
# Shared by run.sh / test-run.sh / fix-run.sh. The caller has already cd'ed into
# the repository; this script inherits that working directory. Its exit status
# is the worst of the per-package cargo runs, which is how a test failure
# propagates to the caller.
set -uo pipefail

# Colour codes would have to be stripped back out in parse_log; never emit them.
export CARGO_TERM_COLOR=never
export CARGO_NET_RETRY=5
export RUST_BACKTRACE=1
# Signals a non-interactive automated run. Neither cargo nor libtest reads it,
# but build scripts in the dependency graph may, and it is the cross-language
# harness convention.
export CI=true
# NOT set: RUSTFLAGS="-C link-dead-code", which upstream CI uses for coverage
# instrumentation. It changes nothing about which tests run and only slows the
# link step down.

rc=0
for pkg in @PACKAGES@; do
    # Written by this shell, to stdout, before cargo is spawned. See the note in
    # the config: this is what makes crate attribution independent of how cargo's
    # stdout and stderr interleave in the merged stage log.
    echo "@MARKER_PREFIX@${pkg}@MARKER_SUFFIX@"
    cargo test -p "$pkg" --tests --no-fail-fast || rc=1
done

exit $rc
""".replace("@PACKAGES@", " ".join(_CARGO_PACKAGES))
    .replace("@MARKER_PREFIX@", _PKG_MARKER_PREFIX)
    .replace("@MARKER_SUFFIX@", _PKG_MARKER_SUFFIX)
)


class JuniperImageBase(Image):
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
        # The workspace declares `edition = "2018"` and no `rust-version` /
        # `rust-toolchain.toml`, so the repo itself sets no floor — the floor
        # comes from the resolved dependency graph. With the pins in
        # pin-deps.sh applied that graph builds comfortably below 1.90; 1.90 is
        # pinned rather than `rust:latest` so the toolchain cannot drift under
        # the config. Verified in-container: run, test and fix all behave as
        # expected under 1.90.
        return "rust:1.90"

    # Tagged per PR, not with a constant `base`. This image is hardened to
    # exactly ONE ${BASE_COMMIT} by DockerfileEnhancer (`git checkout --detach`
    # followed by `git gc --prune=now`), and build_dataset skips rebuilding an
    # image that already exists — so a repo-constant tag would make every future
    # juniper PR resolve to the same image and silently inherit a tree pinned to
    # the first one's commit. workdir() moves with it so the build context lands
    # in images/base-pr-<N>/.
    def image_tag(self) -> str:
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

        # `dependency()` returns a str, so DockerfileEnhancer rewrites the clone
        # line below into the standard `git clone "${REPO_URL}"` +
        # `git checkout ${BASE_COMMIT}` + history-hardening + CMD block, and
        # prepends the BuildKit syntax directive, the TARGETARCH / REPO_URL /
        # BASE_COMMIT and proxy ARGs, the shared ENV block, the OCI labels and
        # the CA-certificate symlinks. None of that is written here.
        # CARGO_INCREMENTAL=0 is set on the BASE so it governs every cargo
        # invocation downstream — prepare.sh's two warm builds and all three
        # graded stages — rather than having to be re-exported in each script.
        #
        # Incremental compilation is a developer-machine optimisation for
        # repeated edit-rebuild cycles; it buys nothing here (each stage applies
        # a different patch set and rebuilds) and costs a great deal. Measured on
        # the built pr-812 image: target/debug/incremental was 1.1 GB of a 2.0 GB
        # target/, pushing the final image to 5.04 GB — over the 4 GB budget.
        # Disabling it also removes a known class of rustc incremental-only
        # miscompilation bugs from the graded path, which is why CI builds
        # disable it as a matter of course.
        return f"""FROM {image_name}

{self.global_env}

ENV CARGO_INCREMENTAL=0

WORKDIR /home/

{code}

{self.clear_env}

"""


class JuniperImageDefault(Image):
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
        return JuniperImageBase(self.pr, self._config)

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
                "pin-deps.sh",
                _PIN_DEPS_SH,
            ),
            File(
                ".",
                "run-tests.sh",
                _RUN_TESTS_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Resolve and pin the dependency graph. Cargo.lock is listed in juniper's
# .gitignore, so writing it leaves the working tree pristine -- asserted rather
# than assumed, since a dirty tree here would make every later `git apply` in
# the graded stages unpredictable.
bash /home/pin-deps.sh
bash /home/check_git_changes.sh

# Warm the baseline build: populate the crates.io cache and fill target/ so the
# graded `run` stage does not have to compile the whole dependency tree from
# scratch. `|| true` because this is a cache-warming step, not a gate -- a
# failure here must not fail the image build, it must surface in the graded run.
bash /home/run-tests.sh || true

# Warm the fix-stage build too, then reverse both patches in reverse order.
# `set -e` makes a failed reverse fail the image build loudly rather than
# silently shipping a dirty checkout. Neither patch touches Cargo.toml or
# Cargo.lock, so the pinned resolution above survives this untouched.
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run-tests.sh || true
git apply -R --whitespace=nowarn /home/fix.patch /home/test.patch
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
bash /home/run-tests.sh

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


_RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# libtest result line: `test macros::tests::args::introspect_field_args ... ok`.
# The name is captured non-greedily and the trailing status is anchored, so any
# per-run suffix libtest may append after the status is not absorbed into the id.
_RE_TEST_LINE = re.compile(
    r"^test\s+(?P<name>.+?)\s+\.\.\.\s+(?P<status>ok|FAILED|ignored)\b"
)
# The package marker run-tests.sh writes to stdout before each cargo invocation.
# This — NOT cargo's own `Running unittests ...` header — is what attributes a
# test to a crate; see the note above _RUN_TESTS_SH for why the header is
# unusable. Nothing per-invocation (a `-<hash>` suffix, timings, counts) appears
# in it, so a test keeps the same id across the run / test / fix stages.
_RE_PKG_MARKER = re.compile(
    rf"^{re.escape(_PKG_MARKER_PREFIX)}(?P<pkg>\S+){re.escape(_PKG_MARKER_SUFFIX)}$"
)
# rustdoc names a doc-test after the source line it starts on; the line number is
# not part of the test's identity. Doc-tests are not selected by run-tests.sh
# (`--tests`), but the suffix is stripped anyway so an override run command
# cannot leak unstable ids into the report.
_RE_DOCTEST_LINE_SUFFIX = re.compile(r"\s*\(line \d+\)\s*$")


@Instance.register("graphql-rust", "juniper")
class Juniper(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JuniperImageDefault(self.pr, self._config)

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

        # Crate the tests currently being read belong to. Empty until the first
        # marker, in which case ids fall back to the bare libtest name rather
        # than being dropped — that fallback only applies to an overridden run
        # command, and applies identically in all three stages, so names still
        # line up across them.
        label = ""

        for raw in test_log.splitlines():
            line = _RE_ANSI.sub("", raw).strip()

            m = _RE_PKG_MARKER.match(line)
            if m:
                label = m.group("pkg")
                continue

            m = _RE_TEST_LINE.match(line)
            if not m:
                continue

            name = _RE_DOCTEST_LINE_SUFFIX.sub("", m.group("name").strip())
            test_id = f"{label}::{name}" if label else name
            status = m.group("status")

            if status == "ok":
                passed_tests.add(test_id)
            elif status == "FAILED":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        # A test that failed anywhere is failed, even if an earlier line said ok
        # (a retried or re-reported case), so TestResult's disjointness
        # invariants always hold.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
