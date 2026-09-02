import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# One config for all five openmls PRs in the dataset. They span 2020-2025, and the repo
# changed shape twice in that window, so a single hardcoded build command cannot serve
# them. Everything era-specific is isolated in _ERA below and read through _era(); the
# Dockerfiles, scripts and log parser are shared.
#
# What actually differs between the eras:
#
#   PR    date        crate layout                         graded scope
#   ----  ----------  -----------------------------------  ---------------------------
#   156   2020-10-26  ONE crate at the repo root, and it    cargo test        (no -p:
#                     is not even called openmls - the      there is no workspace, and
#                     package is `maelstrom`. Tests in      the package name is
#                     tests/*.rs.                           irrelevant to the command)
#   553   2021-11-15  Sibling crates, NO root Cargo.toml    cd openmls && cargo test
#                     at all, so no workspace exists.       (package chosen by cwd,
#                     Tests in openmls/tests/*.rs.          exactly as the era's CI did)
#   1619  2024-07-22  Real workspace at the root.           cargo test -p openmls
#   1825  2025-08-11  Same workspace, one extra member.     cargo test -p openmls
#   1901  2025-11-25  Same workspace, one extra member.     cargo test -p openmls
#
# The single most important fact about this repository: it commits NO Cargo.lock at any
# of the five base commits (verified - .gitignore lists Cargo.lock / **/Cargo.lock).
# Dependencies therefore resolve FRESH against today's crates.io on every build, no
# matter which year the PR is from. Two consequences drive the design below:
#
#   1. `--locked` is unavailable - there is no lock to honour - so prepare.sh must run
#      `cargo generate-lockfile` itself.
#   2. An era-matched compiler is the WRONG instinct. Modern transitive crates ship
#      manifests declaring `edition2024`, which only Cargo 1.85+ can parse. Building
#      the 2024 PR on the era-correct 1.80 fails before compiling a line of openmls:
#
#        error: failed to parse manifest at .../cpufeatures-0.3.1
#        Caused by: feature `edition2024` is required ... not stabilized in this
#        version of Cargo (1.80.1)
#
#      Since all five PRs share ONE base image (see image_tag), one toolchain has to
#      serve 2020 through 2025. rust:1.90 is that choice: >= 1.85 so today's manifests
#      parse, and contemporary with the 2025 pair. Rust is backward compatible within an
#      edition, so the edition-2018 (156) and edition-2021 (all others) sources still
#      build on it. No base commit pins a toolchain (no rust-toolchain.toml; CI just
#      says "stable"), so nothing is being overridden, and a fixed tag rather than
#      `rust:latest` keeps a future release from silently changing this image.
#
# The same unlocked-resolution property also breaks the two eras that depend on git
# repositories WITHOUT a rev pin: cargo resolves those at the remote's HEAD *today*,
# which has long since moved past the version the manifest asks for. _ERA["pins"]
# repairs that per era - see the comments on each entry.
#
# PR #156 IS A DROP - kept here for completeness, not expected to build. Its rev pins
# resolve correctly and prepare.sh reaches dependency resolution, then dies on:
#
#   error: failed to select a version for the requirement `aes-soft = "^0.4"`
#     version 0.4.0 is yanked
#     ... required by `aes v0.4.0` <- `aes-gcm v0.6.0` <- `evercrypt v0.0.3-dev3`
#
# aes-soft 0.4.0 AND 0.5.0 are both withdrawn from crates.io, and the parent aes 0.4.0
# requires ^0.4, so no version can satisfy it. Cargo tolerates a yanked crate only when
# it is already recorded in a Cargo.lock - which this repo never commits. The 2020
# environment is unreconstructable. It was independently unusable anyway: the test patch
# adds zero new test fns and calls CredentialBundle, a type only the FIX patch defines,
# so the test target cannot compile -> every test is NONE, never FAIL. No new names
# makes n2p impossible and NONE-not-FAIL makes f2p impossible.

# --- git dependency revisions, pinned to the state at each base commit ----------------
#
# PR #1619's root Cargo.toml declares
#   tls_codec = { version = "0.4.2-pre.1", ..., git = "https://github.com/rustcrypto/formats" }
# with no rev. That repo's HEAD now carries tls_codec 0.5.0, which does not satisfy the
# requirement, so resolution fails outright:
#   error: no matching package named `tls_codec` found
# This rev is 2024-07-25, four days before the base commit, and still carries
# 0.4.2-pre.1. Pinning it reconstructs what a committed Cargo.lock would have recorded
# and changes no code under test.
#
# A `[patch]` entry in $CARGO_HOME/config.toml was tried first and is NOT usable: cargo
# rejects it with "patch for `tls_codec` points to the same source, but patches must
# point to different sources". A `[source]` replacement was rejected too. The rev pin is
# the mechanism that was verified to work.
TLS_CODEC_REV = "9918fc7caad5d4ea2c6823b0dd1d0d2992659d06"

# PR #1619 has a SECOND unpinned git dependency, and it is easy to miss because it is not
# in the root manifest and not in a dependency list that applies to every target. The
# dev-dependencies at this base commit are target-gated:
#
#   [target.'cfg(not(any(target_arch = "wasm32", ...)))'.dev-dependencies]
#   openmls = { path = ".", features = ["test-utils", "libcrux-provider"] }
#
# On linux/amd64 and linux/arm64 that cfg is TRUE, so `libcrux-provider` is enabled for
# the test build, which pulls openmls_libcrux_crypto, whose manifest declares
#
#   libcrux = { git = "https://github.com/cryspen/libcrux", features = ["rand"] }
#
# with no rev - so cargo resolves it at cryspen/libcrux HEAD today, roughly 18 months of
# API drift past the base commit. This rev is 2024-07-22T11:17:34Z, the last commit before
# the base commit. Note this one is applied to libcrux_crypto/Cargo.toml, NOT the root.
LIBCRUX_REV = "2daee9ce208d05aa2a91bbdb4732083e2d31e92c"

# ...and pinning libcrux exposes a THIRD unpinned git dependency, one level deeper still.
# libcrux's own manifest at the rev above declares:
#
#   [target.'cfg(hax)'.dependencies]
#   hax-lib-macros = { version = "0.1.0-alpha.1", git = ".../hacspec/hax",  branch = "main" }
#   hax-lib        = { version = "0.1.0-alpha.1", git = ".../hacspec/hax/", branch = "main" }
#
# `branch = "main"` resolves at hax's HEAD today, which is version 0.4.0-rc.1 and does not
# satisfy ^0.1.0-alpha.1:
#   error: failed to select a version for the requirement `hax-lib = "^0.1.0-alpha.1"`
#
# Two things make this awkward. First, the dependency sits behind `cfg(hax)`, a cfg that
# is never enabled - but cargo resolves EVERY [target.*] section when generating a
# lockfile (the lock must cover all targets), so it cannot be dodged by not enabling it.
# Second, it lives in libcrux's manifest, not ours, so no sed of a file in this repo can
# reach it. A [patch] section appended to the ROOT manifest is the only lever, and it
# applies graph-wide.
#
# This rev is 2024-07-22, matching the base commit; its workspace version is 0.1.0-pre.1,
# which satisfies ^0.1.0-alpha.1 (pre-release ordering puts "alpha" before "pre", and both
# below 0.2.0). One [patch] section covers both dependency lines even though their URLs
# differ by a trailing slash, because cargo canonicalises git URLs when forming source ids.
#
# The patch MUST point at a local PATH, not at the same git URL with a rev. Pinning by rev
# was tried and cargo refuses it:
#
#   error: failed to resolve patches for `https://github.com/hacspec/hax`
#   Caused by: patch for `hax-lib` points to the same source, but patches must point to
#              different sources
#
# Canonicalisation makes `?rev=<x>` and `?branch=main` the same source for this rule - the
# same reason the [patch] approach failed for tls_codec. A path source is a genuinely
# different source kind, so prepare.sh clones hax at this rev into /home/hax and patches
# to those directories. Safe to vendor: hax-lib at this rev pulls only num-bigint,
# num-traits and its own workspace siblings - no git dependencies of its own, so this
# terminates the chain rather than extending it.
HAX_REV = "d2ebf7e676381d14d354e286a6f32a58af04541d"

# PR #156 (2020) has the same problem twice over, and worse: its git remotes have since
# been transferred to new owners.
#   hpke      = {git = "https://github.com/franziskuskiefer/hpke-rs", version = "0.0.2-dev2"}
#   [patch.crates-io] evercrypt / evercrypt-sys from franziskuskiefer/evercrypt-rust
# Neither carries a rev. franziskuskiefer/hpke-rs now redirects to celabshq/hpke-rs and
# its HEAD is many major versions past 0.0.2. Both revs below are the last commit on or
# before the 2020-10-26 base date, and each was checked to still carry a version that
# satisfies the manifest requirement:
#   hpke @ 8a701bda (2020-10-23) -> version 0.0.2, which satisfies "^0.0.2-dev2"
#   evercrypt-sys @ c8d5d0ae     -> version 0.0.3-dev3, the exact requirement
# The URLs are left as the original franziskuskiefer/* ones; git follows GitHub's
# transfer redirect, so rewriting them is unnecessary and would diverge from the PR.
HPKE_REV = "8a701bda5cb6aeb841d50a3ee7c6c691df40ba3c"
EVERCRYPT_REV = "c8d5d0aeb5c2b29a06aec5a3b0bf722a1f9433f4"


# Shell fragments spliced into prepare.sh. They edit Cargo.toml IN PLACE and then COMMIT
# the edit, because check_git_changes.sh runs afterwards and an uncommitted manifest
# would fail it. Committing also keeps the later `git apply` of test/fix patches clean,
# since no patch in this dataset touches these dependency lines.
#
# Every sed adds `rev = "..."` immediately after the git URL rather than rewriting the
# whole line, so it stays readable and cannot silently no-op on a reformatted manifest -
# the `grep -q` right after is the hard proof the substitution landed.
# Both of #1619's unpinned git dependencies. tls_codec is in the ROOT manifest; libcrux is
# in libcrux_crypto/Cargo.toml and is reached only because the target-gated
# dev-dependencies switch on `libcrux-provider` for non-wasm targets (see LIBCRUX_REV).
# Pinning only the first leaves the second resolving at today's HEAD.
_PIN_2024 = """
sed -i 's|git = "https://github.com/rustcrypto/formats" }|git = "https://github.com/rustcrypto/formats", rev = "__TLS_CODEC_REV__" }|' Cargo.toml
grep -q 'rev = "__TLS_CODEC_REV__"' Cargo.toml
sed -i 's|libcrux = { git = "https://github.com/cryspen/libcrux"|libcrux = { git = "https://github.com/cryspen/libcrux", rev = "__LIBCRUX_REV__"|' libcrux_crypto/Cargo.toml
grep -q 'rev = "__LIBCRUX_REV__"' libcrux_crypto/Cargo.toml

git clone --quiet https://github.com/hacspec/hax /home/hax
git -C /home/hax checkout --quiet __HAX_REV__
test -f /home/hax/hax-lib/Cargo.toml
test -f /home/hax/hax-lib-macros/Cargo.toml

cat >> Cargo.toml <<'PATCH_EOF'

# Added by the harness. See HAX_REV in the repo config for the full explanation.
[patch."https://github.com/hacspec/hax"]
hax-lib = { path = "/home/hax/hax-lib" }
hax-lib-macros = { path = "/home/hax/hax-lib-macros" }
PATCH_EOF
grep -q 'path = "/home/hax/hax-lib"' Cargo.toml
"""

# -g on the evercrypt sed: the URL appears twice in [patch.crates-io], once for the
# `evercrypt` package and once for `evercrypt-sys`, and both need the same rev.
_PIN_2020 = """
sed -i 's|git = "https://github.com/franziskuskiefer/hpke-rs"|git = "https://github.com/franziskuskiefer/hpke-rs", rev = "__HPKE_REV__"|' Cargo.toml
grep -q 'rev = "__HPKE_REV__"' Cargo.toml
sed -i 's|git = "https://github.com/franziskuskiefer/evercrypt-rust"|git = "https://github.com/franziskuskiefer/evercrypt-rust", rev = "__EVERCRYPT_REV__"|g' Cargo.toml
grep -c 'rev = "__EVERCRYPT_REV__"' Cargo.toml | grep -q '^2$'
"""


# Every key here is consumed by the PR image, never the base - the base is shared by all
# five PRs and so cannot carry anything era-specific (the toolchain used to live here and
# was moved out to RUST_IMAGE when the base became common).
#
# cargo_dir  : directory to run cargo from, relative to the repo root.
# pkg        : package selector. Empty where there is no workspace to select from.
# tests_glob : integration test sources, relative to cargo_dir. Each becomes its own
#              `--test <name>` invocation (see _RUN_TESTS_SH for why).
# pins       : shell run inside prepare.sh before generate-lockfile.
_ERA = {
    156: {
        "cargo_dir": ".",
        "pkg": "",
        "tests_glob": "tests/*.rs",
        "pins": _PIN_2020,
    },
    553: {
        "cargo_dir": "openmls",
        "pkg": "",
        "tests_glob": "tests/*.rs",
        "pins": "",
    },
    1619: {
        "cargo_dir": ".",
        "pkg": "-p openmls",
        "tests_glob": "openmls/tests/*.rs",
        "pins": _PIN_2024,
    },
    1825: {
        "cargo_dir": ".",
        "pkg": "-p openmls",
        "tests_glob": "openmls/tests/*.rs",
        "pins": "",
    },
    1901: {
        "cargo_dir": ".",
        "pkg": "-p openmls",
        "tests_glob": "openmls/tests/*.rs",
        "pins": "",
    },
}

# One toolchain for the shared base image. See the header for why 1.90 and not an
# era-matched compiler.
RUST_IMAGE = "rust:1.90-bookworm"


def _era(pr: PullRequest) -> dict:
    """Era settings for this PR, or a loud failure.

    Deliberately raises instead of falling back to a default. A silently wrong build
    command produces zero test results, and report.py treats a stage with zero results
    as vacuously satisfying its "fix something" check - so the instance would be graded
    valid with empty buckets rather than reported as broken. Failing here makes an
    unhandled PR number impossible to miss.
    """
    era = _ERA.get(pr.number)
    if era is None:
        raise ValueError(
            f"openmls: no era settings for PR #{pr.number}. "
            f"Known: {sorted(_ERA)}. Add an _ERA entry before building it."
        )
    return era


# The graded command, byte-identical in all three stages - only patch application
# differs between them. It lives in one script file rather than being inlined into
# run.sh/test-run.sh/fix-run.sh so the three cannot drift apart.
TEST_CMD = "bash /home/run_tests.sh"


class OpenMlsImageBase(Image):
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
        return RUST_IMAGE

    def image_tag(self) -> str:
        # ONE base image shared by all five PRs, which is the dominant convention in this
        # repo (283 of 600 configs with an ImageBase return a constant "base").
        #
        # THIS IMPOSES A BUILD-ORDER REQUIREMENT. DockerfileEnhancer rewrites the clone
        # line into `clone -> checkout ${BASE_COMMIT} -> history scrub`, and the scrub
        # deletes every ref then gc-prunes, so ONLY COMMITS REACHABLE FROM BASE_COMMIT
        # SURVIVE. BASE_COMMIT is taken from whichever PR triggers the build
        # (build_dataset passes image.pr.base.sha) and cannot be overridden from here.
        #
        # So this image MUST be created from the NEWEST PR in the dataset, #1901
        # (e136a642). All five base commits are ancestors of it - verified with
        # `git merge-base --is-ancestor` - so 1,775 commits survive and every PR can
        # still check its own base out. Seed it from any earlier PR and the later PRs
        # break silently: their base shas will already have been pruned, and prepare.sh
        # fails at `git checkout <sha>` with "reference is not a tree".
        #
        # Practical consequence: build #1901 first, then build the rest with
        # force_build=False so this image is reused rather than rebuilt at an older
        # commit.
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

        # DEBIAN_FRONTEND and LANG are deliberately NOT set here: DockerfileEnhancer
        # already injects both (with TZ and the proxy/CA wiring) right after FROM.
        #
        # The C toolchain is not optional in any era, only for different reasons:
        #   2020 - evercrypt-sys compiles HACL* C and generates bindings with bindgen,
        #          which needs libclang at runtime, and its asm needs nasm.
        #   2021 - ring (via openmls_rust_crypto) builds C and assembly.
        #   2024+ - openmls_libcrux_crypto is an unconditional dev-dependency of the
        #          openmls crate on every non-wasm target, and libcrux compiles C.
        # Omitting any of these surfaces as an opaque link or bindgen failure late in
        # the build rather than a clear "package not found".
        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV CARGO_TERM_COLOR=never
ENV RUST_BACKTRACE=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential clang libclang-dev cmake pkg-config nasm \\
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class OpenMlsImageDefault(Image):
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
        return OpenMlsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Built with .replace() on sentinel tokens rather than str.format(): these
        # scripts contain shell braces (${...}, $(...)), and str.format() would treat a
        # lone "}" as a format error. .replace() also keeps every backslash literal,
        # which avoids the octal-escape class of bug - a sed backreference "\1" written
        # inside a non-raw Python string becomes chr(1) and silently empties the
        # replacement.
        era = _era(self.pr)

        pins = (
            era["pins"]
            .replace("__TLS_CODEC_REV__", TLS_CODEC_REV)
            .replace("__LIBCRUX_REV__", LIBCRUX_REV)
            .replace("__HAX_REV__", HAX_REV)
            .replace("__HPKE_REV__", HPKE_REV)
            .replace("__EVERCRYPT_REV__", EVERCRYPT_REV)
        )

        prepare = (
            _PREPARE_SH.replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__PINS__", pins)
            .replace("__CARGO_DIR__", era["cargo_dir"])
            .replace("__PKG__", era["pkg"])
        )

        run_tests = (
            _RUN_TESTS_SH.replace("__REPO__", self.pr.repo)
            .replace("__CARGO_DIR__", era["cargo_dir"])
            .replace("__PKG__", era["pkg"])
            .replace("__TESTS_GLOB__", era["tests_glob"])
        )

        def stage(patch_cmd: str) -> str:
            return (
                _STAGE_SH.replace("__REPO__", self.pr.repo)
                .replace("__PATCH_CMD__", patch_cmd)
                .replace("__TEST_CMD__", TEST_CMD)
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

""",
            ),
            File(".", "run_tests.sh", run_tests),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", stage("")),
            File(
                ".",
                "test-run.sh",
                stage("git apply --whitespace=nowarn /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                stage("git apply --whitespace=nowarn /home/test.patch /home/fix.patch"),
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

        # RUSTFLAGS is set HERE, in the image, rather than exported inside the stage
        # scripts, for two reasons: it must be identical for prepare.sh's warm-up build
        # and for all three graded stages (RUSTFLAGS is part of cargo's fingerprint, so a
        # mismatch silently discards the warm cache and each stage pays a full cold
        # rebuild), and being in the Dockerfile makes it visible to a reviewer.
        #
        # WHY debug-assertions=off. openmls' own test fixtures deliberately construct
        # invalid Vecs to exercise error paths - e.g. binary_tree::tests::test_new_tree_error
        # and ::test_tree_basics. Rust 1.85+ added a `Vec::set_len` UB precondition check
        # that is gated on debug-assertions, and it fires a NON-UNWINDING panic:
        #
        #   unsafe precondition(s) violated: Vec::set_len requires that new_len <= capacity()
        #   thread caused non-unwinding panic. aborting.
        #   process didn't exit successfully: ... (signal: 6, SIGABRT)
        #
        # SIGABRT kills the whole test binary, so the lib target reported 5 of its 1093
        # tests and every test sorting after `binary_tree` - including all 11 that PR #1619
        # adds - never ran. That produced an empty n2p bucket and an instance the harness
        # rejected as an error. Skipping the offending tests was tried and is whack-a-mole:
        # skipping the first merely moves the abort to the second, and each skip silently
        # shrinks the graded set.
        #
        # This is a direct consequence of the shared base image: one base means one
        # toolchain, and 1.90 is required for the 2025 PRs' edition2024 manifests, whereas
        # #1619's era compiler (1.80) had no such check. Turning the check off restores the
        # runtime semantics the PR was written against. Measured effect: 5 -> 1092 tests
        # reported, 0 failures.
        #
        # The cost, stated plainly: this also disables openmls' own debug_assert!s, so a
        # defect that would trip one goes unseen. Accepted because the checks here fire on
        # deliberately-invalid test fixtures rather than on real defects, and because the
        # alternative - an era-matched toolchain - is what the shared base rules out.
        rustflags = 'ENV RUSTFLAGS="-C debug-assertions=off"'

        return f"""FROM {name}:{tag}

{self.global_env}

{rustflags}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


# Each test target is invoked SEPARATELY and its failure swallowed.
#
# This is the most important decision in the file. A single blanket `cargo test` aborts
# the ENTIRE run the moment one target fails to compile, and emits zero results for
# every other target. Zero results is not a neutral outcome: report.py's "fix something"
# check passes vacuously on an empty stage, so the instance would be graded valid with
# empty buckets instead of being reported as broken.
#
# A test patch referencing a symbol that only the fix patch introduces produces exactly
# that compile failure, and that is the NORMAL case at the test stage, not an edge case.
# Splitting the targets keeps every other binary reporting even when one is broken.
#
# The "===== TARGET x =====" banners let parse_log qualify each test name with its
# binary: the lib target reports module-qualified paths while integration targets report
# bare fn names, so two targets sharing a name would otherwise collapse into one result.
_RUN_TESTS_SH = """#!/bin/bash
cd /home/__REPO__/__CARGO_DIR__

set +e
RC=0

echo "===== TARGET lib ====="
cargo test __PKG__ --lib
if [ $? -ne 0 ]; then RC=1; fi

for f in __TESTS_GLOB__; do
  [ -e "$f" ] || continue
  t=$(basename "$f" .rs)
  echo "===== TARGET $t ====="
  cargo test __PKG__ --test "$t"
  if [ $? -ne 0 ]; then RC=1; fi
done

set -e
echo "CARGO_EXIT_CODE=$RC"
"""


_PREPARE_SH = """#!/bin/bash
set -e

cd /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __BASE_SHA__
bash /home/check_git_changes.sh

# Reconstruct the dependency state that existed at this base commit. Empty for eras
# whose dependencies are all crates.io version requirements (published versions are
# immutable, so those resolve identically today). See the *_REV constants for why the
# git-sourced eras cannot be left alone.
#
# The edit is COMMITTED, not left in the working tree: check_git_changes.sh runs right
# after, and later stages `git apply` the test/fix patches onto a tree that must be
# clean.
__PINS__
if [ -n "$(git status --porcelain)" ]; then
  git -c user.email=harness@local -c user.name=harness commit -q -am "pin git dependencies to the revisions current at the base commit"
fi
bash /home/check_git_changes.sh

cd __CARGO_DIR__

# Hard gates, deliberately NOT tolerant. A resolution or compile failure must surface
# HERE, at image build time, rather than three stages later behind a silently empty
# report that would be graded as a valid instance.
#
# generate-lockfile is required because this repo commits no Cargo.lock in any era. The
# generated file is gitignored, so it never dirties the tree for the stages that follow.
cargo generate-lockfile

# Warm the build cache so the three graded stages do not each pay a full cold build of
# the C-heavy crypto backends, and prove the base tree compiles its tests at all.
cargo test __PKG__ --no-run

echo "DEPS_OK"
"""


# `set -eo pipefail`, not bare `set -e`: no pipeline exists in this script today, but
# the graded command is spliced in from TEST_CMD and any future pipe there must not be
# able to mask a failure behind a successful tail process.
_STAGE_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
__PATCH_CMD__
set +e
__TEST_CMD__
STAGE_RC=$?
set -e
echo "STAGE_EXIT_CODE=$STAGE_RC"
"""


@Instance.register("openmls", "openmls")
class OpenMls(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenMlsImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # libtest emits "test <name> ... ok|FAILED|ignored". Names are prefixed with the
        # target from the banner run_tests.sh prints, so a name shared by the lib target
        # and an integration target cannot collapse into a single result.
        target_re = re.compile(r"^===== TARGET (\S+) =====$")
        case_re = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)$")

        target = "unknown"
        for line in test_log.splitlines():
            line = line.strip()

            m = target_re.match(line)
            if m:
                target = m.group(1)
                continue

            m = case_re.match(line)
            if not m:
                continue

            name = f"{target}::{m.group(1)}"
            status = m.group(2)
            if status == "ok":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        failed_tests -= passed_tests
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
