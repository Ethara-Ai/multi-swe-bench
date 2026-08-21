import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class CocogittoImageBase(Image):
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
        # Pinned to the repo's declared MSRV at BASE_COMMIT (Cargo.toml:
        # rust-version = "1.78.0"; there is no rust-toolchain.toml). Cargo.lock is
        # lockfile v4, which also requires cargo >= 1.78. Verified: 1.78 resolves the
        # crate graph and runs the full suite. A floating tag would not be reproducible.
        return "rust:1.78"

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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class CocogittoImageDefault(Image):
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
        return CocogittoImageBase(self.pr, self.config)

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

cargo test --all --no-fail-fast || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
cargo test --all --no-fail-fast

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cargo test --all --no-fail-fast

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cargo test --all --no-fail-fast

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


@Instance.register("cocogitto", "cocogitto")
class Cocogitto(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CocogittoImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # libtest writes "test NAME ... " WITHOUT a trailing newline, then the
        # result token. cocogitto's tests shell out through cmd_lib `run_cmd!`,
        # and those child processes inherit fd 1, so git/[INFO ] output lands
        # between the two pieces. Three shapes occur in real logs:
        #   1. "test NAME ... ok"                    (clean)
        #   2. "test NAME ... Reinitialized ... ok"  (token at end of line)
        #   3. "test NAME ... Reinitialized ..."     (token alone, later line)
        #      "[INFO ] ..."
        #      "ok"
        # A plain `test (\S+) \.\.\. ok$` silently drops shapes 2 and 3 - about
        # 1-4 tests per stage, a different set each run. Tracking the pending
        # name across the pollution recovers every one of them.
        re_head = re.compile(r"^test (\S+) \.\.\.(.*)$")
        re_token = re.compile(r"^(ok|FAILED|ignored)$")
        re_summary = re.compile(r"^test result:")

        def record(name: str, token: str) -> None:
            if token == "ok":
                passed_tests.add(name)
            elif token == "FAILED":
                failed_tests.add(name)
            elif token == "ignored":
                skipped_tests.add(name)

        pending = None
        for line in clean_log.splitlines():
            line = line.strip()

            match = re_head.match(line)
            if match:
                name, rest = match.group(1), match.group(2).strip()
                if rest in ("ok", "FAILED", "ignored"):
                    record(name, rest)
                    pending = None
                else:
                    tail = rest.rsplit(" ", 1)[-1] if rest else ""
                    if tail in ("ok", "FAILED", "ignored"):
                        record(name, tail)
                        pending = None
                    else:
                        pending = name
                continue

            if pending is not None:
                if re_token.match(line):
                    record(pending, line)
                    pending = None
                elif re_summary.match(line):
                    # Target finished without ever emitting this test's token.
                    pending = None

        # Deduplicate: ensure no test appears in multiple categories
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
