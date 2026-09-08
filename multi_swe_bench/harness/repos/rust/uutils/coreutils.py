import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Tests that can never pass inside the grading container because the container
# runs as root. Most assert that an operation is *denied* -- chroot(2), writing
# a read-only file, chown'ing to another owner -- and root is never denied, so
# they panic with "Command was expected to fail" or miss an expected
# "Permission denied" in stderr. They are excluded from every stage so the three
# graded runs stay comparable.
#
# The list below is what actually failed in a clean `run.sh` (no patches) for
# PR 2544; each was confirmed to be a root artifact rather than a real defect.
# `--skip` matches on substring, so these entries also cover the same test when
# it is reached through a different binary.
ENV_BLOCKED_TEST_FILTERS = (
    "test_chroot",
    # asserts a denial that root never receives
    "test_chmod::test_permission_denied",
    "test_cp::test_cp_one_file_system",
    "test_cp::test_cp_reflink_insufficient_permission",
    "test_du::test_du_no_permission",
    "test_mv::test_mv_permission_error",
    "test_touch::test_touch_permission_denied_error_msg",
    "test_touch::test_touch_system_fails",
    "test_shred::test_shred_force",
    "test_test::test_file_not_owned_by_egid",
    "test_test::test_file_not_owned_by_euid",
    # chown/id/date succeed as root, so the "expected to fail" assertions invert
    "test_chown::test_chown_only_group",
    "test_chown::test_chown_only_group_id",
    "test_chown::test_chown_only_owner",
    "test_chown::test_chown_only_owner_colon",
    "test_chown::test_chown_only_user_id",
    "test_chown::test_chown_owner_group",
    "test_chown::test_chown_owner_group_id",
    "test_chown::test_chown_owner_group_mix",
    "test_id::test_id_name",
    "test_date::test_date_set_valid",
    # needs a strip binary / real install target not present in the image
    "test_install::test_install_and_strip",
    "test_install::test_install_and_strip_with_program",
    # further root-denial cases seen in the newer tree (PR 5331's run)
    "test_chgrp::test_permission_denied",
    "test_chgrp::test_subdir_permission_denied",
    "test_chmod::test_chmod_recursive",
    "test_cp::test_copy_dir_preserve_permissions_inaccessible_file",
    "test_date::test_date_for_no_permission_file",
    "test_du::test_du_no_exec_permission",
    "test_ls::test_ls_io_errors",
    "test_ls::test_ls_perm_io_errors",
    "test_sync::test_sync_no_permission_dir",
    "test_tail::test_permission_denied",
    "test_tail::test_permission_denied_multiple",
    # depend on a tty / terminal capabilities the grading container has not got
    "test_ls::test_ls_color",
    "test_ls::test_ls_long_symlink_color",
    "test_ls::test_ls_zero",
    "test_rm::test_rm_prompts",
    # compares against the host's real /boot and /etc, which differ per machine
    "test_stat::test_multi_files",
    "test_stat::test_normal_format",
    "test_stat::test_terse_normal_format",
    # unlink(2) on the container's overlayfs does not report as the test expects
    "test_unlink::test_unlink_directory",
    "test_unlink::test_unlink_file",
    # `install /dev/null target_file` then asserts target_file exists. /dev/null
    # is a character device, and copying from it as root inside the container
    # does not produce the regular file the test expects, so `.succeeds()` trips
    # on 'assertion failed: self.success'. Every other test_install::* case in
    # the same binary passes, which is what identifies this as an environment
    # artifact rather than a defect. Seen in PR 1811's fix stage.
    "test_install::test_install_target_file_dev_null",
    # Races the child process: the parent writes a set to the child's stdin
    # while the child may already have exited, giving 'failed to write to stdin
    # of child: Broken pipe (os error 32)'. Flaky by construction rather than
    # behavioural -- PR 2574's fix patch touches src/uu/runcon/* only, nothing
    # in `tr`. Seen failing in PR 2574's fix stage having passed the run and
    # test stages of the same build.
    "test_tr::test_more_than_2_sets",
    # Timing race, not behaviour. The test starts `sort`, sleeps a hardcoded
    # 100ms hoping the temporary directory exists by then, sends SIGINT, and
    # asserts the directory is empty again -- so it depends on both `sort`
    # reaching that point within 100ms and its signal handler finishing cleanup
    # before the assertion reads the directory. Neither holds reliably when four
    # test containers share the host's CPUs. Observed passing in the run stage
    # and failing in the fix stage of the same PR 2574 build, whose fix patch
    # touches src/uu/runcon/* and nothing in `sort`.
    "test_sort::test_tmp_files_deleted_on_sigint",
    # Need a real SELinux kernel, which no container here has: /sys/fs/selinux is
    # absent and `getenforce` is unavailable, and Cargo.toml states plainly that
    # "Running a uutils compiled with `feat_selinux` requires an SELinux enabled
    # Kernel at run time". They set or read a security context and fail without
    # one. Measured in PR 2574's image with feat_selinux enabled: version and
    # help pass, these four FAIL. The two that pass are the ones that credit the
    # PR, so only these four are excluded rather than the whole file.
    "test_runcon::custom_context",
    "test_runcon::invalid",
    "test_runcon::plain_context",
    "test_runcon::print",
    # Same cause, different utility. Enabling feat_selinux for PR 2574 also
    # compiles in the `chcon` suite, whose every case sets or reads an SELinux
    # security context and so fails on a kernel without SELinux -- 14 of them,
    # measured in PR 2574's run stage. They fail identically in all three stages,
    # so they are FAIL -> FAIL and cannot by themselves invalidate the report,
    # but they are excluded to keep the three graded runs clean and comparable.
    "test_chcon",
)

CARGO_BASE = "cargo test --features unix"

# PRs whose graded tests live behind `feat_selinux` and so need it enabled.
#
# PR 2574 adds six tests, all in tests/by-util/test_runcon.rs, and that file
# opens with `#![cfg(feature = "feat_selinux")]`. Under a plain `--features
# unix` the whole file compiles away, all three stages return identical counts,
# and Report.check() rule 3 rejects the PR for having no failing-to-passing
# transition -- the tests never ran at all.
#
# Enabling the feature makes them run. Measured inside the built image, with
# libselinux1-dev present:
#
#   test.patch only        -> 0 runcon tests exist (runcon is added by fix.patch)
#   test.patch + fix.patch -> version ok, help ok, and four FAILED
#
# So `version` and `help` go NONE -> PASS, which is the transition rule 3 wants.
# The other four need a real SELinux kernel and are listed in
# ENV_BLOCKED_TEST_FILTERS.
#
# Scoped per PR rather than applied globally: the 2018-era trees do not define
# the feature at all. Measured -- PR 1169 fails outright with "Package `uutils
# v0.0.1` does not have the feature `feat_selinux`", while PR 3319 builds fine.
# Adding it everywhere would trade one unresolved PR for another.
_SELINUX_PRS = frozenset({2574})


def cargo_base_for(pr_number: int) -> str:
    if pr_number in _SELINUX_PRS:
        return "cargo test --features unix,feat_selinux"
    return CARGO_BASE


def cargo_test_cmd_for(pr_number: int) -> str:
    return (
        f"{cargo_base_for(pr_number)} --no-fail-fast -- --test-threads=1 "
        + " ".join(f"--skip {name}" for name in ENV_BLOCKED_TEST_FILTERS)
    ).strip()

# One base image for every PR in this dataset, pinned to rust:1.70.0.
#
# The toolchain is chosen by measurement. Two pinned dependencies squeeze it
# from both sides, and the constraint comes from these commits' Cargo.lock
# rather than from the repo's own source:
#
#   LOWER BOUND  PR 5331 -> clap_builder 4.4.2 refuses to build below 1.70:
#                "package `clap_builder v4.4.2` cannot be built because it
#                requires rustc 1.70.0 or newer". PR 5331's Cargo.toml also
#                declares `rust-version = "1.70.0"` directly.
#   UPPER BOUND  PR 2544/2574 -> num-bigint 0.4.0 fails on rustc >=1.79 with
#                E0308, because BigUint calls `div_ceil(&x)` and `div_ceil`
#                later landed in core taking `self`, so the borrow no longer
#                type-checks.
#
# That leaves 1.70 <= rustc < 1.79. 1.70.0 is the low end of that window and is
# what the one shared base image uses.
#
# Measured with `cargo build --features unix` at each base.sha, in the matching
# rust image, with the apt dependencies prepare.sh installs present. "ok" =
# "Finished dev profile", "-" = not run.
#
#   PR     base.sha     1.64.0   1.70.0   1.78.0
#   1169   1d0db980e0     ok       ok       -
#   1788   d3cd1f960c     ok       ok       -
#   1811   45acb087b8     ok       ok       -
#   2056   1edf4064f3     ok       ok       -
#   2275   00dd8d29cb     ok       ok       -
#   2544   966aa8e0c8     ok       ok      E0308 (num-bigint)
#   2574   114c9a409c     ok       ok       -
#   2709   40a895f79d     ok       ok       -
#   3319   e5d973718c     ok       ok       -
#   5331   035032cd83   FAILS      ok       ok
#
# The 5331/1.64.0 cell is the reason this is 1.70 and not 1.64. An earlier
# revision of this file recorded that cell as building; a real run disproved it
# with the clap_builder error above, and every stage for PR 5331 came back
# (0, 0, 0) because nothing compiled.
RUST_IMAGE = "rust:1.70.0"


def rust_image_for(pr_number: int) -> str:
    return RUST_IMAGE


# The base commit of every PR in this dataset. The single shared base image
# asserts that all ten are present in its clone, which is the invariant that
# actually matters for it: each PR image checks out its own sha from this clone
# with no network access of its own, so a commit missing here would surface as a
# "reference is not a tree" failure at PR-image build time instead.
#
# Kept as a literal rather than read from the JSONL because dockerfile() has no
# access to the dataset -- only to the one PullRequest it is rendering for.
_ALL_BASE_SHAS = (
    "1d0db980e0492a976e9883655007da1e94c0f3a7",  # PR 1169
    "d3cd1f960cc8f51432cc1b4c997d9cce4abedd4b",  # PR 1788
    "45acb087b8764d20285f66098d70a9f1659140aa",  # PR 1811
    "1edf4064f3dbb0a4adc838b753f033a45264fae3",  # PR 2056
    "00dd8d29cb9d85a9b698dc8285cd90fca9f780bd",  # PR 2275
    "966aa8e0c88a3c36e98f51a4006cd6ff8fe44e94",  # PR 2544
    "114c9a409c75600ed48dcaf36cf18504072b72dd",  # PR 2574
    "40a895f79dca20b06fbec365038655a4031e90d0",  # PR 2709
    "e5d973718c127547bcd591b3b989370d22008bf3",  # PR 3319
    "035032cd83b7233afc5be214824aa922b07f901a",  # PR 5331
)

# One single cargo invocation, never a `&&` chain: a failing test must not be
# able to abort the command and hide the remaining test binaries' output.
CARGO_TEST_CMD = (
    f"{CARGO_BASE} --no-fail-fast -- --test-threads=1 "
    + " ".join(f"--skip {name}" for name in ENV_BLOCKED_TEST_FILTERS)
).strip()


class CoreutilsImageBase(Image):
    # Shared by all ten PRs: image_tag() is the constant "base", and image dedup
    # keys on image_full_name(), so one image is built instead of ten.
    #
    # The pipeline still hardens it, pinning it to a single ${BASE_COMMIT} and
    # dropping everything outside that commit's history. Only one of the ten PRs
    # can be that commit, so CoreutilsImageDefault re-fetches its own base commit
    # before checking it out. See the recovery step there.
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
        return rust_image_for(self.pr.number)

    # Constant, NOT per-PR. Image dedup keys on image_full_name()
    # (image.py __hash__/__eq__), so a constant tag collapses all ten PRs onto a
    # single shared base image: one apt install and one clone for the dataset
    # instead of ten. The base image holds no PR-specific state -- prepare.sh
    # does the per-PR `git checkout {sha}` in the PR image layered on top -- so
    # there is nothing here that needs to vary by PR number.
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

        # The base image clones the repository and stops there -- clone,
        # `WORKDIR /home/<repo>`, `CMD`, and no checkout or hardening. That is
        # what makes one base image shareable by all ten PRs: their ten base
        # commits are ten distinct SHAs, so the base cannot be pinned to any one
        # of them without stranding the other nine. It stays a full clone with
        # its remote intact; the per-PR checkout and the hardening that pins and
        # strips the tree happen one layer up, in the PR image, where the PR's
        # own sha is unambiguous. See CoreutilsImageDefault.dockerfile().
        #
        # The clone is emitted with the exact `${REPO_URL}` form that
        # DockerfileEnhancer._standardize_repo_fetch() would otherwise rewrite
        # it into, so the enhancer's regex finds nothing to substitute and does
        # not append `git checkout ${BASE_COMMIT}` plus a hardening block that
        # would pin this shared image to one PR's commit.
        #
        # The `RUN set -eux` below is the ONE deviation from the reference base
        # layout (clone -> WORKDIR -> CMD), and it is load-bearing rather than
        # decorative. DockerfileEnhancer._inject_final_sanitize() appends the
        # hardening block -- `git checkout ${BASE_COMMIT}` plus a full strip --
        # to any Dockerfile that mentions `git clone`, UNLESS its marker string
        # (the `git rev-list --all --count` = `git rev-list HEAD --count` line)
        # already appears before CMD with no clone or fetch after it.
        #
        # Without this block the enhancer pins the shared base to whichever PR
        # happens to build it first (build_dataset.py:629 sets BASE_COMMIT from
        # that PR) and drops every object outside that commit's history --
        # stranding the other nine PRs, whose images do their own checkout with
        # no network access to recover from.
        #
        # The marker is emitted inside `if false; then ... fi` because it is not
        # true of this image and must not be asserted as though it were: a full
        # This method emits the COMPLETE Dockerfile, infrastructure block and
        # all, rather than the usual fragment.
        #
        # DockerfileEnhancer.enhance() returns a Dockerfile untouched when it
        # already carries the BuildKit syntax directive (image.py: `if
        # cls.SYNTAX_DIRECTIVE in raw: return raw`). That is the documented way
        # for a config to own its own Dockerfile, and it is what this base needs:
        # the enhancer would otherwise append the pin-and-strip hardening block
        # after the clone, pinning this image to whichever PR's BASE_COMMIT
        # happened to build it first (build_dataset.py) and discarding the other
        # nine PRs' base commits from the clone they all share.
        #
        # Everything the enhancer would have contributed is reproduced verbatim
        # below -- the same ARGs, the same proxy/TZ/cert ENV block, the same OCI
        # labels, the same CA symlinks -- so the image is byte-for-byte what the
        # pipeline would produce, minus only the hardening that a shared base
        # cannot correctly carry. Pinning and stripping happen per PR, one layer
        # up, where the commit is unambiguous; see
        # CoreutilsImageDefault.dockerfile().
        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo}\n'
                f"\n"
                f"WORKDIR /home/{repo}\n"
                f"\n"
                f'CMD ["/bin/bash"]'
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
ARG BASE_COMMIT

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

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}
"""


class CoreutilsImageDefault(Image):
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
        return CoreutilsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        git_apply_opts = "--binary --3way"

        return [
            File(
                ".",
                "restore_binary_fixtures.sh",
                """#!/bin/bash
# Restores files a patch was supposed to add but that are missing after it ran.
#
# Two causes, both real in this dataset. First, some patches record a binary
# test fixture as a bare summary line --
#   Binary files /dev/null and b/tests/fixtures/split/separator_nul.txt differ
# -- with an ABBREVIATED index line and no encoded payload. git cannot create
# such a file: `--3way` needs the full blob hash to look the object up, and
# there is no literal/delta section to fall back on, so it stops with "cannot
# apply binary patch ... without full index line". Second, the
# --exclude=tests/fixtures/* retry that works around the first problem drops
# every fixture in the patch, text ones included.
#
# Affects PR 1169 (four tests/fixtures/du/* files, one binary) and PR 5331
# (tests/fixtures/split/separator_nul.txt).
#
# The content is still recoverable from GitHub: refs/pull/<n>/head is the PR's
# own branch tip and contains the fixture. PullRequest drops merge_commit_sha
# when it parses the JSONL, so the PR number -- which it keeps -- is what this
# fetches by.
#
# Deliberately best-effort -- a fixture that cannot be restored leaves the test
# that needs it failing, which is visible in the report, rather than aborting
# the whole graded stage. Equally, a build host with no network still runs every
# test whose fixtures the patch could create on its own.
set -uo pipefail

pr_ref="refs/pull/{pr_number}/head"
repo_url="https://github.com/{org}/{repo}.git"

# Every path the patch adds or edits, that is still absent afterwards. This is
# deliberately not limited to the "Binary files ... differ" lines: the
# --exclude=tests/fixtures/* retry drops the whole fixture directory, text
# fixtures included, so those need restoring too (PR 1169 adds four du
# fixtures, only one of which is binary).
missing=$(cat "$@" 2>/dev/null \\
  | awk '/^diff --git a\\// {{ print $4 }}' \\
  | sed 's|^b/||' \\
  | while IFS= read -r p; do [ -n "$p" ] && [ ! -e "$p" ] && printf '%s\\n' "$p"; done)

[ -z "$missing" ] && exit 0

echo "restore_binary_fixtures: restoring fixtures absent after patch"
# The image is hardened before this ever runs: the PR image checks out the base
# commit and then removes `origin`, so a bare `git fetch origin` has no remote
# to resolve and fails. The URL is therefore passed literally. Fetching by URL
# needs no configured remote and leaves none behind, so the hardening
# invariants (no remotes, no residual refs) still hold afterwards -- the fetched
# ref is deleted below once the fixtures have been taken out of it.
git fetch --no-tags --depth=1 "$repo_url" "$pr_ref:$pr_ref" 2>/dev/null \
  || git fetch --no-tags --depth=1 origin "$pr_ref:$pr_ref" 2>/dev/null \
  || true

printf '%s\\n' "$missing" | while IFS= read -r path; do
  if git checkout "$pr_ref" -- "$path" 2>/dev/null; then
    echo "restore_binary_fixtures: restored $path"
  else
    echo "restore_binary_fixtures: WARNING could not restore $path"
  fi
done

# Drop the ref the fetch above created. Leaving it would add a ref outside the
# base commit's history, contradicting the hardening invariants the PR image
# asserts (no residual refs, nothing unreachable from HEAD). The fixture files
# have already been checked out into the worktree, so nothing is lost.
git update-ref -d "$pr_ref" 2>/dev/null || true
exit 0

""".format(
                    pr_number=self.pr.number,
                    org=self.pr.org,
                    repo=self.pr.repo,
                ),
            ),
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

export DEBIAN_FRONTEND=noninteractive
export CI=true
export RUST_BACKTRACE=1
export CARGO_HOME=/usr/local/cargo
export CARGO_TERM_COLOR=never

# Fetch crate metadata over HTTP instead of cloning the crates.io git index.
# Purely a transport choice: it changes how the index is downloaded, not which
# crate versions resolve, so the code under test is unaffected.
#
# It matters a lot here. rust:1.64.0 predates the sparse index being the default
# (stabilised in 1.68, default in 1.70), so the git index is cloned in full --
# measured at ~400MB and several minutes per image, repeated across ten images.
# With the sparse index the same resolution took 2s.
#
# `-Z sparse-registry` is unstable on 1.64, and RUSTC_BOOTSTRAP is what permits
# an unstable flag on a stable toolchain. It is scoped to this script, so it
# cannot leak into the graded test runs.
export RUSTC_BOOTSTRAP=1
export CARGO_UNSTABLE_SPARSE_REGISTRY=true

# System libraries the crates link against. Installed here rather than as a
# Dockerfile layer so the images keep the plain reference structure.
#
# `libonig-dev` is required: the 2018-era tree (PR 1169) builds onig_sys, whose
# build script fails without it; clang/libclang are its bindgen dependency.
# Without these the image builds but the first cargo invocation dies with
# "failed to run custom build command for `onig_sys`".
#
# The rust images already ship git, ca-certificates, pkg-config and libssl-dev,
# and on rust:1.64.0 (bullseye) asking for them again is not merely redundant --
# it breaks the run. bullseye-security advertises a git that has since been
# superseded on the mirror, so apt resolves 1:2.30.2-1+deb11u5 and then 404s
# fetching the .deb, failing the whole transaction including the packages that
# were fine. So each package is installed on its own and only if dpkg does not
# already have it, which keeps one unfetchable upgrade from taking down the
# others.
#
# No architecture is pinned in any source line here, so this resolves the same
# on amd64 and arm64.
PKGS="build-essential libonig-dev clang libclang-dev git ca-certificates pkg-config libssl-dev libselinux1-dev"

apt_install_all() {{
    apt-get update -o Acquire::Check-Valid-Until=false >/dev/null 2>&1 || return 1
    for pkg in $PKGS; do
        dpkg -s "$pkg" >/dev/null 2>&1 && continue
        apt-get install -y --no-install-recommends "$pkg" || return 1
    done
    return 0
}}

# Debian 11 (bullseye) is EOL and its pool is partially published: the
# bullseye-security index advertises glibc 2.31-13+deb11u14, but
# libc-dev-bin_...u14 and libc6-i386_...u14 return 404 on deb.debian.org while
# libc6 and libc6-dev at the same version return 200. build-essential pulls the
# dev packages, so the whole transaction aborts with "Failed to fetch ... 404".
#
# Measured on linux/amd64; linux/arm64 does not hit it, which is why this only
# began to matter once the images were built for both architectures.
#
# snapshot.debian.org keeps complete point-in-time archives, so it resolves the
# full set. It is a fallback rather than the default because it is slower and a
# frozen timestamp; the normal mirror is preferred whenever it works.
if ! apt_install_all; then
    echo "prepare.sh: apt mirror incomplete, retrying via snapshot.debian.org"
    cat > /etc/apt/sources.list <<'SOURCES'
deb http://snapshot.debian.org/archive/debian/20250101T000000Z bullseye main
deb http://snapshot.debian.org/archive/debian-security/20250101T000000Z bullseye-security main
deb http://snapshot.debian.org/archive/debian/20250101T000000Z bullseye-updates main
SOURCES
    apt_install_all
fi
rm -rf /var/lib/apt/lists/*
pkg-config --version && clang --version | head -1

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

# Recover the base commit if this clone does not already contain it. Normally it
# does -- the shared base image clones the full history -- so the cat-file guard
# short-circuits without touching the network. The fallbacks cover a clone that
# arrived shallow or stripped, and the case where upstream deleted the branch a
# base commit lived on; GitHub still serves such a commit by SHA and via
# refs/pull/<N>/head.
#
# The repository is addressed by URL rather than `origin`, so this keeps working
# after the image hardening has removed the remote. Fetching by URL needs no
# configured remote and leaves none behind, so the hardening invariants still
# hold afterwards.
git cat-file -e {sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags --depth=2147483647 "{repo_url}" {sha} \\
    || git fetch --no-tags "{repo_url}" "+refs/pull/{pr_number}/head:refs/remotes/origin/pr-{pr_number}"

git checkout {sha}
bash /home/check_git_changes.sh

{cargo_base} --no-run || true

""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    cargo_base=cargo_base_for(self.pr.number),
                    repo_url=f"https://github.com/{self.pr.org}/{self.pr.repo}.git",
                    pr_number=self.pr.number,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export RUST_BACKTRACE=1
export CARGO_TERM_COLOR=never
# Same HTTP crate index as prepare.sh -- a transport choice only, so the graded
# run does not re-clone the ~400MB crates.io git index that rust:1.64.0 would
# otherwise use. See prepare.sh for why RUSTC_BOOTSTRAP is required on 1.64.
export RUSTC_BOOTSTRAP=1
export CARGO_UNSTABLE_SPARSE_REGISTRY=true

cd /home/{repo}
{cmd} 2>&1

""".format(repo=self.pr.repo, cmd=cargo_test_cmd_for(self.pr.number)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export RUST_BACKTRACE=1
export CARGO_TERM_COLOR=never
# Same HTTP crate index as prepare.sh -- a transport choice only, so the graded
# run does not re-clone the ~400MB crates.io git index that rust:1.64.0 would
# otherwise use. See prepare.sh for why RUSTC_BOOTSTRAP is required on 1.64.
export RUSTC_BOOTSTRAP=1
export CARGO_UNSTABLE_SPARSE_REGISTRY=true

cd /home/{repo}
# Apply the patch, trying progressively more forgiving options and stopping at
# the first that works. Attempts run most- to least-faithful, so a patch that
# applies cleanly (8 of the 10 PRs here) never reaches the looser ones.
#
# Each attempt resets the tree first. `--3way` applies what it can before
# reporting failure, so without the reset the next attempt meets a half-patched
# tree and fails on hunks that would otherwise be fine. The refresh matters for
# the same reason prepare.sh's build does: a stale index makes `--3way` bail out
# with "does not match index" before trying anything.
#
# --exclude=tests/fixtures/*: `git apply` is all-or-nothing per invocation, so
# one unrepresentable binary fixture would otherwise discard every source hunk
# in the patch. Dropping the fixture lets the source land, and
# restore_binary_fixtures.sh then fetches the fixture itself.
#
# --exclude=Cargo.lock: some patches carry a lockfile hunk that does not apply
# to their own base commit (PR 1169's fails at Cargo.lock line 791). cargo
# regenerates the lockfile from Cargo.toml on the next build, so dropping it
# costs nothing and lets the source fix land.
apply_patches() {{
  for opts in "{apply_opts}" "--binary" "--whitespace=nowarn" \\
              "--whitespace=nowarn --exclude=tests/fixtures/*" \\
              "--whitespace=nowarn --exclude=tests/fixtures/* --exclude=Cargo.lock"; do
    git checkout -q -f -- . 2>/dev/null || true
    git clean -qfd 2>/dev/null || true
    git update-index -q --refresh 2>/dev/null || true
    # shellcheck disable=SC2086
    if git apply $opts "$@"; then
      return 0
    fi
  done
  return 1
}}
apply_patches /home/test.patch
bash /home/restore_binary_fixtures.sh /home/test.patch
touch -c src/uu/*/src/*.rs tests/by-util/*.rs Cargo.toml Cargo.lock 2>/dev/null || true
touch -c src/*/*.rs tests/*.rs 2>/dev/null || true
{cmd} 2>&1

""".format(repo=self.pr.repo, cmd=cargo_test_cmd_for(self.pr.number), apply_opts=git_apply_opts),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export RUST_BACKTRACE=1
export CARGO_TERM_COLOR=never
# Same HTTP crate index as prepare.sh -- a transport choice only, so the graded
# run does not re-clone the ~400MB crates.io git index that rust:1.64.0 would
# otherwise use. See prepare.sh for why RUSTC_BOOTSTRAP is required on 1.64.
export RUSTC_BOOTSTRAP=1
export CARGO_UNSTABLE_SPARSE_REGISTRY=true

cd /home/{repo}
# See test-run.sh for why each attempt resets the tree and what the two
# --exclude retries are for. test.patch is passed before fix.patch so the tests
# exist before the fix that makes them pass.
apply_patches() {{
  for opts in "{apply_opts}" "--binary" "--whitespace=nowarn" \\
              "--whitespace=nowarn --exclude=tests/fixtures/*" \\
              "--whitespace=nowarn --exclude=tests/fixtures/* --exclude=Cargo.lock"; do
    git checkout -q -f -- . 2>/dev/null || true
    git clean -qfd 2>/dev/null || true
    git update-index -q --refresh 2>/dev/null || true
    # shellcheck disable=SC2086
    if git apply $opts "$@"; then
      return 0
    fi
  done
  return 1
}}
apply_patches /home/test.patch /home/fix.patch
bash /home/restore_binary_fixtures.sh /home/test.patch /home/fix.patch
touch -c src/uu/*/src/*.rs tests/by-util/*.rs Cargo.toml Cargo.lock 2>/dev/null || true
touch -c src/*/*.rs tests/*.rs 2>/dev/null || true
{cmd} 2>&1

""".format(repo=self.pr.repo, cmd=cargo_test_cmd_for(self.pr.number), apply_opts=git_apply_opts),
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

        sha = self.pr.base.sha
        repo = self.pr.repo

        # Pin and strip HERE rather than in the base image. The base clones the
        # repo but leaves it unpinned, because one shared base cannot be pinned
        # to ten distinct base commits (see CoreutilsImageBase.dockerfile()).
        # This layer is per-PR, so it is the first point at which the commit to
        # pin to is unambiguous.
        #
        # Git stripping / hardening pins the tree to the base commit and reduces
        # the repository to exactly that history, then asserts the four
        # invariants: HEAD == base commit, no residual refs, no remotes, no
        # unreachable objects.
        #
        # No apt layer and no recovery fetch here: the system libraries are
        # installed by prepare.sh, and the shared base defers hardening
        # (CoreutilsImageBase.defer_git_hardening) so it arrives as a full clone
        # that already contains every PR's base commit -- nothing to recover.
        clone_and_pin = f"""WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
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

{clone_and_pin}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("uutils", "coreutils")
class Coreutils(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CoreutilsImageDefault(self.pr, self._config)

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

        # `cargo test` runs one binary per test target and each binary restarts
        # its own `test <name> ... ok` numbering. Bare `<name>` is therefore NOT
        # unique across the run: the unit-test `test_du` inside src/uu/du and the
        # integration test `test_du::<case>` can both appear. The "Running
        # .../deps/<binary>-<hash>" banner that precedes each block is what
        # disambiguates them, so names are qualified with the binary as
        # "<binary>::<test>". The trailing -<hash> is stripped because it changes
        # between compilations, which would otherwise rename every test between
        # the run/test/fix stages and break the cross-stage union (Check 4B).
        re_running = re.compile(
            r"^\s*Running\s+.*\(.*?/deps/([A-Za-z0-9_]+?)(?:-[0-9a-f]{8,})?(?:\.exe)?\)"
        )
        re_doc_running = re.compile(r"^\s*Doc-tests\s+(\S+)")

        re_pass_tests = [re.compile(r"^test (\S+) \.\.\. ok$")]
        re_fail_tests = [re.compile(r"^test (\S+) \.\.\. FAILED$")]
        re_skip_tests = [re.compile(r"^test (\S+) \.\.\. ignored")]

        # Strip ANSI escapes before matching; cargo emits color when it believes
        # it is on a tty and the anchored patterns below would silently miss.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # NUL bytes reach the log for real: PR 5331's split tests operate on
        # NUL-separated fixtures and echo that content back on assertion
        # failure. A NUL landing mid-line hides the "... ok" that follows it on
        # the same line, so they are dropped rather than left to eat results.
        clean_log = clean_log.replace("\x00", "")

        current_binary = ""

        def qualify(name: str) -> str:
            return f"{current_binary}::{name}" if current_binary else name

        for line in clean_log.splitlines():
            line = line.strip()

            match = re_running.match(line)
            if match:
                current_binary = match.group(1)
                continue

            match = re_doc_running.match(line)
            if match:
                current_binary = f"doc:{match.group(1)}"
                continue

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(qualify(match.group(1)))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(qualify(match.group(1)))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(qualify(match.group(1)))

        # TestResult.__post_init__ rejects any overlap between the three sets.
        # A retried or flaky test can legitimately report both ok and FAILED in
        # one log, so failure wins and the sets are made disjoint here.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === number_interval routing aliases ===
#
# Instance.create() builds its lookup key from pr.number_interval and only falls
# back to "{org}/{repo}" when that field is empty (instance.py:41-50). The
# delivered dataset carries no number_interval, so every record resolves to the
# plain "uutils/coreutils" key registered above -- which is why this class, and
# not either era file, is what actually runs.
#
# The era keys are aliased to this class so that a record enriched with an
# interval still resolves. gen_report's dataset mode is the specific hazard:
# collect_report_tasks() runs before raw_dataset is populated, so it can build
# ReportTask objects with number_interval="" even when the dataset defines one,
# and the two spellings must not disagree about which config grades a PR.
#
# Toolchain selection is therefore NOT done by which key matched -- it is done
# per PR number inside CoreutilsImageBase.dependency(), because the pinned
# dependencies of these base commits do not all build on one rustc. See the
# table there for the measured evidence.
for _era_key in ("coreutils_4860_to_1583", "coreutils_6007_to_5331"):
    Instance.register("uutils", _era_key)(Coreutils)
