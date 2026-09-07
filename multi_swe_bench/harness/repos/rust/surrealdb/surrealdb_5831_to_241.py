"""surrealdb/surrealdb -- era 1 of 2, PR interval 5831 -> 241 (pre-`crates/` layout,
workspace-wide `cargo test`).

Era boundary. Every PR in this interval predates the move of the graded code into
the `surrealdb-types` crate, so the whole workspace is the unit under test and the
toolchain is the one that still builds the `lib/tests/*.rs` integration suites:

    surrealdb_5831_to_241.py   this file    rust:1.68, `cargo test --workspace`
    surrealdb.py               PR 7121      rust:1.91, `cargo test -p surrealdb-types`

The toolchain gap between the two files is not cosmetic: this era's lockfiles pin
`geo-types 0.7.7` and `time 0.3.x`, neither of which compiles on a modern rustc.
See SurrealdbRangeImageBase.dependency() for the two exact errors.

Registration. This era answers to `surrealdb/surrealdb_5831_to_241`, which
Instance.create() (instance.py:41-49) builds only from a dataset row carrying
number_interval="surrealdb_5831_to_241". Rows that leave it empty compute the plain
`surrealdb/surrealdb` key and land on the PR-7121 config instead (R26).

Image split. The base is ONE tag for the whole interval, built once and shared by
every PR, so it holds only the toolchain and an unpinned clone -- no checkout, no
hardening. Pinning is per-PR by definition, so `git reset --hard`, `git checkout
${BASE_COMMIT}` and the full Image._HARDENING_BLOCK live in each `pr-<N>` image
instead:

    base-5831-to-241   rust:1.68 + `git clone` of the full history      built once
    pr-<N>             + COPY patches/scripts, pin to that PR's sha,    built per PR
                       run prepare.sh, then pin and harden

Putting the checkout in the shared base would pin it to whichever PR the scheduler
reached first and -- via the hardening block's `git gc --prune=now` -- delete every
other PR's base commit, so the remaining four would die on `git checkout <sha>`
with `fatal: unable to read tree`. Hardening per-PR avoids that entirely: each
pr-<N> is pinned to exactly one commit, so it can run the block unmodified,
`gc`/`repack` included, and the graded artifact ends up fully pruned.

Enhancer interaction. The base emits the BuildKit syntax directive as its first
line, which makes DockerfileEnhancer.enhance() (image.py:311-318) return it
verbatim -- otherwise _standardize_repo_fetch/_inject_final_sanitize would inject
exactly the checkout+prune this layout is avoiding. The infrastructure block the
enhancer would have prepended is therefore written out by hand below. The pr-<N>
image needs no such guard: its dependency() returns an Image, so enhance() returns
early on its own.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SurrealdbRangeImageBase(Image):
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
        # rust:1.68 (2023-03-09). Pinned below 1.80 for ONE reason, measured:
        #
        #   time 0.3.x  (every PR here)  error[E0282]: type annotations needed
        #                                for `Box<_>` -- breaks on rustc >= 1.80
        #
        # The geo-types 0.7.7 failure in PRs 241/1308 is NOT a toolchain problem
        # and no rustc version fixes it: `#[deprecated(since = 0.7.5)]` is
        # invalid Rust in every version (0.7.5 is not a token). Probed directly:
        # 1.65 REJECTS, 1.68 REJECTS, 1.71 REJECTS. It is repaired in prepare.sh
        # by quoting the literal in the vendored crate source -- see there.
        #
        #   geo-types 0.7.7  (PRs 241, 1308)   `#[deprecated(since = 0.7.5)]`
        #                                      -> error: expected `,`, found `.`
        #   time 0.3.x       (PRs 1824+)       -> error[E0282]: type annotations
        #                                         needed for `Box<_>` (rustc >=1.80)
        #
        # Both are upstream-fixed in later crate releases, but the lockfiles pin
        # the broken versions, and the lockfile is a tracked file that the gold
        # patches apply against -- so it cannot be bumped here (R22). Moving the
        # toolchain back is the only fix that leaves the repo untouched.
        return "rust:1.68"

    def image_tag(self) -> str:
        return "base-5831-to-241"

    def workdir(self) -> str:
        return "base-5831-to-241"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Toolchain + an unpinned clone. No checkout, no hardening.

        Opens with the BuildKit syntax directive so enhance() (image.py:311-318)
        returns this text verbatim. Without that guard the enhancer would run
        _standardize_repo_fetch and _inject_final_sanitize over a base shared by
        five PRs and bake in `git checkout ${BASE_COMMIT}` plus a
        `git gc --prune=now`, destroying four of the five base commits. The
        infrastructure block enhance() would otherwise have prepended is written
        out by hand here so nothing is lost by taking the early return.

        REPO_URL is supplied as a build arg because dependency() returns a str
        (build_dataset.py:624-629). BASE_COMMIT is declared but deliberately
        unused -- the same call site passes it, and an undeclared build arg draws
        a warning from BuildKit.
        """
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{repo}.git"
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

LABEL org.opencontainers.image.title="{self.pr.org}/{repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{repo}" \\
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

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class SurrealdbRangeImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return SurrealdbRangeImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # R3: the stage header and the test command are built ONCE here and
        # interpolated into all three run scripts, so they are byte-identical in
        # every stage. A flag that differs between stages renames tests and the
        # FAIL->PASS transition stops being visible.
        stage_header = """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export CARGO_BUILD_JOBS=4
"""

        # --no-fail-fast is load-bearing, not a nicety. `cargo test --workspace`
        # runs each test target in turn and STOPS at the first target that fails.
        # At these commits the lib unit-test binary has a pre-existing failure
        # (sql::value::value::tests::check_size), so cargo aborted with
        #     error: test failed, to rerun pass `-p surrealdb --lib`
        # and never reached the integration targets in lib/tests/*.rs -- which is
        # where every gold test in this dataset lives. Observed on pr-241: all
        # three stages reported the same 385 lib unit tests, complex.rs never
        # ran, and f2p came back empty on a valid instance.
        #
        # No `|| true` on a test command either: it would swallow a runner that
        # fails to START, parse_log would return 0/0/0, and Report.check() would
        # reject the instance as though the patch were inert. Collecting the
        # status into `rc` keeps the exit code truthful while still letting the
        # language-test suite run after a cargo failure.
        stage_test = """rc=0

cd /home/{pr.repo}
cargo test --workspace --no-fail-fast -- --skip telemetry --skip test_capabilities --skip api_integration 2>&1 || rc=$?

# The language-test runner moved into crates/ partway through this interval; run
# whichever path exists. Its failures feed the same rc so neither suite masks the
# other.
if [ -d "crates/language-tests" ]; then
  cd /home/{pr.repo}/crates/language-tests && cargo run -- run 2>&1 || rc=$?
  cd /home/{pr.repo}
elif [ -d "language-tests" ]; then
  cd /home/{pr.repo}/language-tests && cargo run -- run 2>&1 || rc=$?
  cd /home/{pr.repo}
fi

exit $rc
""".format(pr=self.pr)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# The checkout above is a no-op re-assertion: the Dockerfile layer that runs this
# script already pinned HEAD to {pr.base.sha} and ran the hardening block, so the
# repo arrives detached at the right commit with every ref, remote and reflog
# already gone. Kept so the invariant is stated where the setup is read.

# System dependencies. Discovered by building, not by reading a manifest -- this
# repo's CI has no apt-get step to copy (it builds via Docker/nix), so each entry
# below is here because its absence broke a build:
#
#   libclang-dev        bindgen
#   cmake               native deps
#   protobuf-compiler   `protoc`, required by opentelemetry-proto's build script:
#                       "Could not find `protoc` installation and this build crate
#                       cannot proceed without this knowledge" (exit 101)
#   pkg-config libssl-dev   openssl-sys probing, pre-emptive for the same class
#
# This runs at image BUILD time, where the network is available and the output is
# not parsed (R16). Retried, because Debian security drops superseded .deb files
# as soon as a new one lands: an index fetched moments earlier can already point
# at a version that 404s. Observed on the amd64 leg of a multi-arch build --
#   E: Failed to fetch .../libc-dev-bin_2.31-13+deb11u14_amd64.deb  404
# -- which installed NOTHING while the arm64 leg installed all 20 packages. A
# fresh `apt-get update` on each attempt re-resolves to versions that still
# exist.
# deb.debian.org is a CDN, and an edge can serve a Packages index advertising a
# .deb its pool has already replaced -- Debian deletes superseded security builds
# as soon as a new one lands. Seen here as a hard 404 on
#   libc-dev-bin_2.31-13+deb11u14_amd64.deb   [IP: 167.82.58.132]
# which clang depends on via libc6-dev, so it takes the whole install down.
# Disabling HTTP caching makes apt re-resolve against the edge's current index
# rather than reusing the response it already holds.
printf 'Acquire::http::No-Cache "true";\nAcquire::http::Pipeline-Depth "0";\n' > /etc/apt/apt.conf.d/99msb-nocache

apt-get update -qq 2>&1 || true

# Installed in groups, not one transaction. apt aborts the WHOLE install if any
# single .deb 404s, and deb.debian.org regularly advertises a version its pool
# has already replaced -- observed here as
#   E: Failed to fetch .../libssl1.1_1.1.1w-0+deb11u8_amd64.deb  404
# on the amd64 leg while arm64 fetched all 20 packages fine. Grouped this way a
# 404 in the openssl/build-essential group cannot take clang down with it, and
# the compile guard below still fails the build if something load-bearing is
# genuinely absent.
#
# Group 1 is the one that must succeed: bindgen/rquickjs-sys cannot build
# without libclang, and the FATAL check below enforces that.
if ! apt-get install -y -qq clang libclang-dev llvm-dev 2>&1; then
  # Same CDN edge keeps serving the same stale index, so retrying it is futile.
  # Point at the canonical hosts instead: security.debian.org for the security
  # suite (the one that 404s) and ftp.debian.org for main. Order matters -- the
  # security rule is the more specific prefix and must be rewritten first.
  echo "clang group failed on deb.debian.org; switching mirrors and retrying"
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] || continue
    sed -i -e 's|deb.debian.org/debian-security|security.debian.org/debian-security|g' -e 's|deb.debian.org/debian|ftp.debian.org/debian|g' "$f" 2>/dev/null || true
  done
  rm -rf /var/lib/apt/lists/*
  apt-get update -qq 2>&1 || true
  apt-get install -y -qq clang libclang-dev llvm-dev 2>&1 || true
fi

# Group 2: needed by build scripts (protoc for opentelemetry-proto, cmake and
# patch for the C dependencies). Best-effort; a miss surfaces as a named build
# failure rather than a silent one.
apt-get install -y -qq cmake protobuf-compiler pkg-config patch 2>&1 || true

# Group 3: openssl headers and the C toolchain. rust:1.68 already ships gcc, and
# this era's surrealdb links rustls rather than openssl, so these are defensive
# -- exactly the group that was 404ing, and exactly the one that can be skipped.
apt-get install -y -qq libssl-dev build-essential 2>&1 || true

# bindgen (via rquickjs-sys) dlopens libclang.so at build-script time and dies
# with "Unable to find libclang" when only the headers are present -- which is
# why --no-install-recommends was dropped above. Export an explicit path too,
# since the .so is versioned and its directory moves between Debian releases.
#
# Test the FIND RESULT, not dirname's output: `dirname ""` returns "." , which is
# both non-empty and a real directory, so an earlier `[ -n ] && [ -d ]` guard on
# it passed and exported LIBCLANG_PATH=. after apt had silently installed
# nothing. The build then ran six more minutes before clang-sys failed with an
# unrelated-looking `Option::unwrap() on a None value`. Missing libclang is
# fatal, so fail here where the cause is still legible.
LIBCLANG_SO=$(find /usr/lib /usr/lib64 -name 'libclang.so*' 2>/dev/null | head -1)
if [ -z "$LIBCLANG_SO" ]; then
  echo "FATAL: libclang.so not found after apt install -- bindgen/rquickjs-sys cannot build"
  exit 1
fi
export LIBCLANG_PATH="$(dirname "$LIBCLANG_SO")"
echo "LIBCLANG_PATH=$LIBCLANG_PATH"

# Disable debug symbols to reduce image size (~60-70% smaller)
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0

# Cap cargo's parallelism. The Docker VM is fixed at 8 GB (.wslconfig documents
# why it must not be raised: 11 GB starved Windows and crashed Docker Desktop),
# but it exposes 10 CPUs, so cargo defaults to -j 10. Ten concurrent rustc
# processes linking a workspace this size exhausted the VM and the buildkit
# daemon was OOM-killed mid-build:
#   "rpc error: code = Unavailable desc = error reading from server: EOF"
# observed while linking lib/examples/*, where several binaries link the whole
# surrealdb lib at once. Fewer jobs is slower but finishes.
export CARGO_BUILD_JOBS=4

# geo-types 0.7.7 (pinned by the 2022-era lockfiles, PRs 241 and 1308) ships a
# syntactically invalid attribute:
#     #[deprecated(since = 0.7.5, note = "...")]
# `0.7.5` is not a literal token, so this is a parse error on EVERY rustc --
# 1.65, 1.68 and 1.71 all reject it (probed directly). Upstream fixed it in
# 0.7.8, but the lockfile is a tracked file the gold patches apply against, so
# it cannot be bumped here (R22).
#
# Quoting the literal in the vendored dependency is the minimal repair: it is
# semantically identical, and it touches only ~/.cargo/registry -- never the
# repository under test -- so it cannot affect which surrealdb tests run or the
# FAIL->PASS signal. A no-op for PRs pinning geo-types 0.7.9/0.7.10.
cargo fetch 2>&1 || true
GEO_SRC=$(find /usr/local/cargo/registry/src -type d -name 'geo-types-0.7.7' 2>/dev/null | head -1)
if [ -n "$GEO_SRC" ]; then
  grep -rl 'since = 0.7.5' "$GEO_SRC" 2>/dev/null | while read -r file; do
    sed -i 's/since = 0.7.5/since = "0.7.5"/g' "$file"
    echo "patched invalid attribute in $file"
  done
else
  echo "geo-types 0.7.7 not vendored here (expected for the 2023-era PRs)"
fi

# Warm the build cache AND verify the workspace links, in one step.
#
# There used to be a separate warm-up here --
#     cargo test --workspace --no-fail-fast -- --skip ... || true
# -- whose only purpose was to populate target/ so the graded stages start warm.
# But it warmed the cache by compiling AND RUNNING the whole suite, and the
# `--no-run` check below already compiles every test binary, so the compilation
# was done twice and the test execution was pure waste: the three stages run
# those tests again, properly, and only their results are graded.
#
# On amd64 that cost ~15 min per image. On the arm64 leg of a multi-arch build it
# dominated: amd64 finished all of prepare.sh in 1190s while arm64 was still
# executing emulated tests 5500s in. Dropping it leaves target/ just as populated
# -- `--no-run` builds the binaries, it simply does not execute them.
#
# The check is also the build's safety net: without it a broken toolchain yields
# an image that looks healthy and then reports 0 tests in all three stages, a
# silent invalid instance. A compile failure must fail the image build here,
# loudly, rather than surfacing hours later as an empty report.
if ! cargo test --workspace --no-run > /tmp/compile_check.log 2>&1; then
  echo "FATAL: workspace does not compile at {pr.base.sha}"
  grep -E "^error" /tmp/compile_check.log | head -20
  tail -30 /tmp/compile_check.log
  exit 1
fi
echo "compile check: workspace builds at {pr.base.sha}"

# Pre-compile language test runner if present
if [ -d "crates/language-tests" ]; then
  cd /home/{pr.repo}/crates/language-tests && cargo build 2>&1 || true
  cd /home/{pr.repo}
elif [ -d "language-tests" ]; then
  cd /home/{pr.repo}/language-tests && cargo build 2>&1 || true
  cd /home/{pr.repo}
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                "{header}\n{test}".format(header=stage_header, test=stage_test),
            ),
            File(
                ".",
                "test-run.sh",
                "{header}\ncd /home/{pr.repo}\ngit apply --whitespace=nowarn /home/test.patch\n\n{test}".format(
                    header=stage_header, pr=self.pr, test=stage_test
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                # R6: the agent's patch is bind-mounted at /home/fix.patch during
                # evaluation, so this path is fixed. test.patch before fix.patch.
                "{header}\ncd /home/{pr.repo}\ngit apply --whitespace=nowarn /home/test.patch /home/fix.patch\n\n{test}".format(
                    header=stage_header, pr=self.pr, test=stage_test
                ),
            ),
        ]

    def dockerfile(self) -> str:
        """COPY, run prepare.sh, then pin this PR's commit and harden.

        Ordering matches the reference layout in the tree (see e.g.
        neherlab/covid19_scenarios pr-75): prepare.sh runs FIRST, so the
        history scrub is the last thing the image does and its four asserts
        therefore describe the artifact as shipped. prepare.sh does its own
        `git reset --hard` + `git checkout <sha>` before installing, so the
        repo is already at the right commit while the workspace compiles; the
        checkout below is a re-assertion, not the first pin.

        The base handed over an unpinned clone of the full history, so the
        checkout belongs in this layer -- it is the only one that knows which
        single commit it is for. Being pinned to one commit is also what makes
        it safe to run Image._HARDENING_BLOCK unmodified, `git gc --prune=now
        --aggressive` and `git repack` included: there are no sibling PRs'
        commits left to destroy, and the graded artifact ends up with nothing
        in it but this PR's base history.

        BASE_COMMIT is declared with a literal default because dependency()
        returns an Image, so build_dataset.py:624-629 passes no build args to this
        layer and an undefaulted ${BASE_COMMIT} would expand to the empty string.

        This whole file is emitted verbatim: enhance() returns early for a
        non-str dependency (image.py:313-315), so no infrastructure block is
        prepended and nothing is rewritten.
        """
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

{prepare_commands}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("surrealdb", "surrealdb_5831_to_241")
class SURREALDB_5831_TO_241(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SurrealdbRangeImageDefault(self.pr, self._config)

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

        re_pass_tests = [
            re.compile(r"test (\S+)\s+\.\.\. ok"),
            re.compile(r"Finished (\S+\.surql) Success"),
        ]
        re_fail_tests = [
            re.compile(r"test (\S+)\s+\.\.\. FAILED"),
            re.compile(r"Finished (\S+\.surql) Error"),
        ]
        re_skip_tests = [re.compile(r"test (\S+)\s+\.\.\. ignored")]

        # Strip ANSI ONCE, before the loop. cargo colours the trailing keyword
        # whenever it believes it has a terminal, and the escape bytes land
        # between "... " and "ok"/"FAILED" -- exactly where these patterns match.
        # Without this the whole log parses as 0/0/0 and the instance is dropped
        # with no error anywhere.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for line in clean_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        # R2: TestResult.__post_init__ RAISES ValueError on any overlap, which
        # kills the entire run rather than just this instance. `cargo test
        # --workspace` prints the same `module::test` path once per binary target
        # that links it, so a single log can legitimately report one name both
        # `... ok` and `... FAILED`. Failure wins.
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
