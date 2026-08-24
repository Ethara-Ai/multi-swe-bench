"""lldap/lldap - Rust 1.70 / cargo / workspace.

Every value came from running the toolchain in Docker at base commit 4576cf9f,
not from reading manifests:

  Cargo.toml (root)   workspace members: server, auth, app, migration-tool,
                      set-password;  default-members = ["server"]
                      [patch.crates-io] pulls opaque-ke and lber FROM GIT
  server/Cargo.toml   edition 2021, package name "lldap"
  no rust-toolchain   -> no pin in-repo; 1.70 matches the 2023-04 base commit
  .github/workflows   cargo test --verbose --workspace
                      image: nitnelave/rust-dev:latest
                      services: mariadb / mysql / postgres

Three things discovered by running it, each of which shapes this file:

1. `-p lldap` NOT `--workspace`. The workspace includes `app`, a Yew/WASM
   frontend that needs the wasm32 target; `default-members = ["server"]` and
   every test this PR adds lives in server/. Scoping to the server package
   builds cleanly and covers the tests that matter.

2. `-j 2`. With 8 CPUs cargo spawns 8 rustc jobs; on a Docker VM sized at
   ~2 GB that starved the daemon badly enough to kill the engine outright,
   twice. Two jobs is the difference between a slow build and no build.

3. THE TEST STAGE MUST NOT REPORT ZERO FOR THE 81 PRE-EXISTING TESTS, EVEN
   THOUGH THE PR'S OWN NEW TESTS CANNOT COMPILE AT THAT STAGE.

   This PR's fix patch touches only Cargo.toml + Cargo.lock - it adds the
   [dev-dependencies] the new e2e tests need (ldap3, reqwest, assert_cmd, nix,
   serial_test, uuid). Applying test.patch alone therefore fails to build:

       error[E0432]: unresolved import `reqwest::blocking`

   A single `cargo test -p lldap` builds every selected target before running
   any of them, so one broken integration file (server/tests/*.rs, added by
   this PR) took the entire invocation down with it - INCLUDING the 81
   pre-existing unit tests in server/src/main.rs, which do not depend on
   anything test.patch touches and would have compiled and passed on their
   own. First measured (wrong):

       run    exit 0    81 results
       test   exit 101   0 results   <- the 81 hidden behind an unrelated failure
       fix    exit 0    86 results

   `lldap` is a BINARY crate (server/src/main.rs, no src/lib.rs), so its own
   tests live under the `--bins` target selector, which does not pull in
   server/tests/ at all. Splitting the invocation into two separately-scoped
   `cargo test` calls - `--bins` then `--tests` - lets the first one succeed
   independently of whatever is broken in the second. Verified against the
   live image, test.patch applied, nothing else changed:

       cargo test -p lldap --bins  -j 2 --no-fail-fast   -> 81 passed  (unaffected)
       cargo test -p lldap --tests -j 2 --no-fail-fast   -> still fails to build

   Both lines run identically in run.sh / test-run.sh / fix-run.sh (P7 still
   holds - only the patches differ), so the corrected counts are:

       run    81 results  (81 passed)
       test   81 results  (81 passed - RECOVERED; the new e2e tests still can't
                            build, which is correct and stays invisible here)
       fix    86 results  (81 + 5 new)

   f2p is still 0 and n2p is still 5 - that part was never a parsing defect.
   `test.patch` cannot compile standalone because its dependencies live in
   `fix.patch`, and no config change reaches that; it is what the PR contains.
   What was wrong, and is now fixed, is that the 81 UNRELATED passing tests
   were being swallowed by cargo's all-or-nothing build gate instead of being
   reported. `--tests` is cargo's broader selector (bins + tests/, not tests/
   alone) - it re-attempts the bins as part of its own failing build, but that
   does not erase the results `--bins` already printed to the log in the
   first, independent invocation.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Two SEPARATE cargo invocations, not one. See module docstring point 3 for the
# measured failure this fixes: a single `cargo test -p lldap` builds every
# selected target before running any of them, so the integration tests this PR
# adds (server/tests/*.rs, broken until fix.patch supplies their dev-deps) took
# the 81 unrelated, independently-buildable unit tests down with them.
#
# `lldap` is a BINARY crate (server/src/main.rs, no src/lib.rs) - its own tests
# live under `--bins`, which does not touch server/tests/ at all, so it builds
# and runs cleanly regardless of what test.patch/fix.patch changed elsewhere.
# `--tests` is cargo's broader selector (bins + tests/, not tests/ alone); it
# re-attempts the bins as part of ITS OWN build, but by the time it runs,
# CARGO_TEST_BINS has already printed the bins' results to the log
# independently, so a build failure here cannot retract them.
#
# -j 2 caps parallel rustc jobs - see the module docstring point 2.
# --no-fail-fast so a failing test target does not stop the others from
# reporting: the stages are compared against each other, and a suite that
# shrinks when a test fails invents transitions that never happened.
#
# Both lines run identically, in this order, in ALL THREE graded scripts - only
# the patches applied beforehand differ - so the P7 command-consistency
# guarantee (a FAIL->PASS transition can only be attributed to the fix, never
# to the command changing) holds across run.sh / test-run.sh / fix-run.sh.
CARGO_TEST_BINS = "cargo test -p lldap --bins -j 2 --no-fail-fast"
CARGO_TEST_INTEGRATION = "cargo test -p lldap --tests -j 2 --no-fail-fast"

# Full, unsplit invocation - used only by prepare.sh's build-time warm-up. Safe
# there: prepare.sh runs at BASE_COMMIT, before either patch is applied, and
# server/tests/ does not exist yet at that commit (this PR creates it whole),
# so there is nothing for the all-or-nothing build gate to trip over.
CARGO_TEST_BASE = "cargo test -p lldap --no-fail-fast"


class LldapImageBase(Image):
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
        # No rust-toolchain file in the repo. 1.70 is contemporaneous with the
        # 2023-04-14 base commit and compiles this tree cleanly (verified:
        # "Finished test [unoptimized + debuginfo] target(s) in 4m 30s").
        return "rust:1.70-bookworm"

    def image_tag(self) -> str:
        # Per-PR. DockerfileEnhancer injects a hardening block that detaches at
        # one ${BASE_COMMIT} and prunes every other object, so a shared tag would
        # let whichever PR built first pin the commit for all the others.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

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

        return f"""\
FROM {image_name}

{self.global_env}

ENV CARGO_TERM_COLOR=never
ENV LC_ALL=C.UTF-8
ENV CI=true

# RUST_BACKTRACE=1 so a panicking test prints where it panicked. The integration
# tests here fail by panicking inside a helper (auth.rs), and without a backtrace
# the log gives you the message and nothing about the path that produced it.
ENV RUST_BACKTRACE=1

# pkg-config is kept because the vendored native crates in the graph
# (libsqlite3-sys, zstd-sys) consult it during their build scripts.
#
# libssl-dev is retained CONSERVATIVELY, not because OpenSSL is required. An
# earlier version of this comment claimed the graph reached OpenSSL through
# actix/reqwest; that is wrong. `grep -c 'name = "openssl-sys"' Cargo.lock`
# returns 0 both at the base commit and with both patches applied - this PR
# pins reqwest and ldap3 to rustls (`default-features = false`, `rustls-tls`),
# so OpenSSL is never linked. The package is harmless and the image is already
# built and verified against it; dropping it would need a fresh multi-arch
# build to confirm nothing regresses, which is not worth the arm64 hours.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates pkg-config libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class LldapImageDefault(Image):
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
        return LldapImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
# Assert the reset actually produced a clean tree rather than assuming it did.
# A stray modified file would flow into all three graded stages and corrupt the
# comparison with nothing in the log to explain why.
bash /home/check_git_changes.sh

git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Compile the baseline test targets into this image layer. This is the expensive
# step (~4m30s measured): actix-web, sea-orm, and the two crates the workspace
# patches straight from git - opaque-ke and lber - all build from source. Doing
# it here means the three scored stages are incremental and do not need GitHub.
#
# Both limits below are widened ONLY on aarch64. A multi-arch build runs the
# arm64 leg under QEMU emulation, which is roughly an order of magnitude slower
# than native, and this crate graph is the expensive kind: actix-web, sea-orm,
# opaque-ke and lber all compile from source. The measured ~4m30s native compile
# therefore lands plausibly between 45 and 90 minutes emulated, which would blow
# straight through a 3600s ceiling and mark a perfectly healthy build as
# INCOMPLETE. amd64 keeps its original timings untouched.
#
# -j 1 on arm64 for memory, not speed: each parallel rustc holds its own arena,
# and several at once under emulation is what has previously pushed this Docker
# VM into an OOM kill mid-compile.
if [ "$(uname -m)" = "aarch64" ]; then
  CARGO_JOBS=1
  BUILD_TIMEOUT=10800
else
  CARGO_JOBS=2
  BUILD_TIMEOUT=3600
fi

# `timeout` covers what `|| true` cannot: `|| true` handles a command that
# FAILS, but one that HANGS never returns and never reaches `||`. Docker has no
# per-step timeout, so a stalled crates.io fetch would block the build forever.
if timeout $BUILD_TIMEOUT {cargo_base} -j $CARGO_JOBS --no-run > /tmp/warm.log 2>&1; then
  echo "warm-up: OK" > /home/.warm_status
else
  echo "warm-up: INCOMPLETE (exit $?)" > /home/.warm_status
  tail -25 /tmp/warm.log || true
fi
cat /home/.warm_status

# Hard gate. The warm-up can "succeed" while leaving the target dir unusable,
# and that surfaces three stages later as an unexplained 0/0/0 rather than as a
# build error. Prove the baseline suite actually RUNS before sealing the image -
# a missing image is honest, a silently hollow one is not.
#
# This gate is now bounded too. Previously it was the ONE unbounded command in
# the file: when the warm-up above timed out, execution fell through to an
# untimed cargo run that could hang the build indefinitely with no diagnostic.
# `timeout` here still fails loudly under `set -e` (exit 124), which is the
# intended behaviour - a missing image is honest, a hung build is not.
timeout $BUILD_TIMEOUT {cargo_base} -j $CARGO_JOBS > /tmp/baseline.log 2>&1
grep -qE '^test result: ok\\.' /tmp/baseline.log
echo "baseline suite OK: $(grep -cE '^test .* \\.\\.\\. ok$' /tmp/baseline.log) tests"

# target/ is gitignored, so the build output does not dirty the tree.
git checkout -- . || true
bash /home/check_git_changes.sh
""".format(pr=self.pr, cargo_base=CARGO_TEST_BASE),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{cargo_bins}
{cargo_tests}
""".format(pr=self.pr, cargo_bins=CARGO_TEST_BINS, cargo_tests=CARGO_TEST_INTEGRATION),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{cargo_bins}
{cargo_tests}
""".format(pr=self.pr, cargo_bins=CARGO_TEST_BINS, cargo_tests=CARGO_TEST_INTEGRATION),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
{cargo_bins}
{cargo_tests}
""".format(pr=self.pr, cargo_bins=CARGO_TEST_BINS, cargo_tests=CARGO_TEST_INTEGRATION),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Generated from files() rather than hard-coded, so a file added there can
        # never be written into the build context yet left uncopied - which would
        # surface at build time as `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/{f.name}\n" for f in self.files())

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

"""


def parse_cargo_test_log(test_log: str) -> TestResult:
    """Parse `cargo test` output.

    Captured verbatim from the container at base commit 4576cf9f:

        running 81 tests
        test domain::handler::tests::test_uuid_time ... ok
        test domain::sql_backend_handler::tests::test_sql_injection ... ok
        test result: ok. 81 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out

    The test name is the full module path, which is unique across the whole
    binary AND stable across the three stages - that is what makes the run/test/
    fix comparison meaningful.

    Note `cargo test` runs several binaries (lib unittests, then each file in
    server/tests/) and prints a separate `running N tests` / `test result:` pair
    per binary. Names are collected across all of them; the `running` and
    `test result:` lines are summaries and are deliberately not counted.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # `test <path::to::test> ... ok`
    #
    # The name is `.+?`, NOT `\S+`. `\S+` looks right and silently loses tests,
    # because libtest does not always put the `...` directly after the name:
    #
    #     test domain::…::test_list_users_invalid_userid_filter - should panic ... ok
    #     test src/lib.rs - some_doc_test (line 42) ... ok
    #
    # A `#[should_panic]` test carries a ` - should panic` suffix and a doc-test
    # name contains spaces, so `\S+` matches neither and the line is skipped
    # entirely. That failure is invisible: the test does not appear as failed, it
    # simply ceases to exist, and since it vanishes from EVERY stage it silently
    # drops out of p2p instead of showing up as a transition. This exact bug hid
    # one passing test here (81 reported by cargo, 80 parsed).
    #
    # The ` - should panic` suffix is consumed by its own optional group rather
    # than folded into the name, so the recorded name stays the real test path
    # and matches the same test in the other two stages.
    #
    # Verified against all three stage logs: parsed counts now equal cargo's own
    # `test result:` totals exactly (81/81, 0/0, 86/86).
    result_re = re.compile(
        r"^test\s+(?P<name>.+?)(?:\s+-\s+should\s+panic)?\s+\.\.\.\s+"
        r"(?P<status>ok|FAILED|ignored)\b"
    )

    ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    for raw_line in test_log.split("\n"):
        line = ansi_escape.sub("", raw_line).strip()
        if not line:
            continue

        match = result_re.match(line)
        if not match:
            continue

        name = match.group("name").strip()
        status = match.group("status")

        if status == "FAILED":
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif status == "ok":
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)
        else:  # ignored
            if name not in failed_tests and name not in passed_tests:
                skipped_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("lldap", "lldap")
class Lldap(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LldapImageDefault(self.pr, self._config)

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
        return parse_cargo_test_log(test_log)