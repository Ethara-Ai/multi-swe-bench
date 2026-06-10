import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Era: vercel-labs/agent-browser PRs >= 564 (the "Rust native rewrite").
#
# At these base commits the CLI is a Rust binary crate under `cli/` (no root
# Cargo workspace).  The relevant regression tests added by the dataset's
# test_patch live in `cli/src/native/e2e_tests.rs` and are `#[ignore]`d --
# they launch a real Chrome via CDP, so they only run with `-- --ignored`.
#
# Discovery (Docker, host arch arm64, verified):
#   * `rust:1-bookworm` -> rustc 1.95, Debian chromium 148 + ffmpeg via apt.
#   * Chrome-for-Testing has no Linux ARM64 build, but the e2e tests resolve
#     the browser through `find_chrome()` (which $PATH chromium) /
#     `AGENT_BROWSER_EXECUTABLE_PATH`, so system chromium works on amd64+arm64.
#   * In a container Chrome auto-gets --no-sandbox / --disable-dev-shm-usage
#     (root + /.dockerenv detection); `CI=true` makes that explicit.
#   * `cargo test --profile ci --manifest-path cli/Cargo.toml` builds the bin
#     unittests (it is a binary crate -- `--lib` fails).  Running with
#     `-- --include-ignored --test-threads=1` runs the normal suite *and* the
#     serial Chrome e2e tests in one pass.
#   * Verified `e2e_launch_navigate_evaluate_close` and the full `e2e` suite
#     pass; `git apply --whitespace=nowarn` applies the PR test/fix patches.
# ---------------------------------------------------------------------------


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

    def dependency(self) -> str:
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", and appends the _HARDENING_BLOCK that
        # strips every other ref/commit so the fix can't be read out of git
        # history. DockerfileEnhancer then injects the proxy/cert infra and the
        # final sanitize pass. None of that fires when dockerfile() is
        # overridden, which is why the previous two-stage build bypassed it.
        return "rust:1-bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        # git + ca-certificates are already in the default package set baked by
        # Image.dockerfile(); add the headless-Chrome stack the e2e tests need.
        return ["pkg-config", "chromium", "ffmpeg"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. We set the browser env, stage the runtime helper scripts +
        # patches into /home/, and warm the cargo build cache. The copied files
        # live outside /home/{repo}, so the hardening pass (which only operates
        # inside the git tree) leaves them untouched.
        return (
            "ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium\n"
            "ENV CI=true\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
                "prepare.sh",
                """#!/bin/bash
# Warm the cargo build cache at image-build time so the eval runs don't need
# network. The repo is already checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout
# itself. The cargo build is allowed to fail (|| true) because its only
# purpose here is to populate the target/ cache; the real pass/fail signal
# comes from the run/test-run/fix-run scripts.
set -e

cd /home/{pr.repo}
git reset --hard || true
# [profile.ci] was introduced mid-era (absent at PR 594 / v0.15.3); fall back
# to the default profile when it is missing.  Binary crate: no --lib.
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci"
cargo test $PROFILE --manifest-path cli/Cargo.toml --no-run || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci"
cargo test $PROFILE --manifest-path cli/Cargo.toml -- --include-ignored --test-threads=1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci"
cargo test $PROFILE --manifest-path cli/Cargo.toml -- --include-ignored --test-threads=1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci"
cargo test $PROFILE --manifest-path cli/Cargo.toml -- --include-ignored --test-threads=1

""".format(pr=self.pr),
            ),
        ]


@Instance.register("vercel-labs", "agent_browser_564_to_99999")
class AGENT_BROWSER_564_TO_99999(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_re.sub("", test_log)

        # Standard libtest output, e.g.:
        #   test native::e2e_tests::e2e_launch_navigate_evaluate_close ... ok
        #   test some::unit::test ... FAILED
        #   test other::test ... ignored
        re_pass = re.compile(r"^test (\S+) \.\.\. ok\b")
        re_fail = re.compile(r"^test (\S+) \.\.\. FAILED\b")
        re_skip = re.compile(r"^test (\S+) \.\.\. ignored\b")

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue
            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue
            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

        # Also harvest the trailing "failures:" summary block as a safety net
        # (a panicking test still lists its name there even if the per-test
        # line was interleaved with captured stdout).
        if "\nfailures:\n" in test_log or test_log.startswith("failures:\n"):
            for block in re.findall(r"\nfailures:\n((?:    \S+\n)+)", test_log):
                for name in block.splitlines():
                    name = name.strip()
                    if name and not name.startswith("----"):
                        failed_tests.add(name)

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
