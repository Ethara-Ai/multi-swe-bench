import re
from typing import Optional, Union

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


class ImageBase(Image):
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
        return "rust:1-bookworm"

    def image_tag(self) -> str:
        return "base-rust-564"

    def workdir(self) -> str:
        return "base-rust-564"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV CI=true

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates pkg-config chromium ffmpeg \\
    && rm -rf /var/lib/apt/lists/*

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
        return ImageBase(self.pr, self.config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
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
