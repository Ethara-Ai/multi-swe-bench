import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.rust.wezterm.wezterm import VTPARSE_FIX


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
        return "rust:latest"

    def image_prefix(self) -> str:
        return "envagent"

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
git submodule update --init --recursive

TERM_CRATE=$(grep '^name' term/Cargo.toml | head -1 | sed 's/.*= *"\\(.*\\)"/\\1/')

# Create separate test workspace with symlinks
mkdir -p /home/test-workspace
cat > /home/test-workspace/Cargo.toml << TOML
[workspace]
members = ["term", "termwiz"]
TOML

ln -sf /home/{pr.repo}/term /home/test-workspace/term
ln -sf /home/{pr.repo}/termwiz /home/test-workspace/termwiz

cd /home/test-workspace
cargo test --no-fail-fast -p $TERM_CRATE || true
cargo test --no-fail-fast -p termwiz || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{vtparse_fix}

cd /home/test-workspace
cargo test --no-fail-fast

""".format(pr=self.pr, vtparse_fix=VTPARSE_FIX),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --reject --whitespace=nowarn /home/test.patch || true

REJ_COUNT=$(find . -name '*.rej' | wc -l)
if [ "$REJ_COUNT" -gt 0 ]; then
    echo "ERROR: $REJ_COUNT reject file(s) found:"
    find . -name '*.rej'
    exit 1
fi

{vtparse_fix}

cd /home/test-workspace
cargo test --no-fail-fast

""".format(pr=self.pr, vtparse_fix=VTPARSE_FIX),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --reject --whitespace=nowarn /home/test.patch /home/fix.patch || true

REJ_COUNT=$(find . -name '*.rej' | wc -l)
if [ "$REJ_COUNT" -gt 0 ]; then
    echo "ERROR: $REJ_COUNT reject file(s) found:"
    find . -name '*.rej'
    exit 1
fi

{vtparse_fix}

cd /home/test-workspace
cargo test --no-fail-fast

""".format(pr=self.pr, vtparse_fix=VTPARSE_FIX),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """FROM rust:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git cmake pkg-config libssl-dev python3

WORKDIR /home/

RUN git clone https://github.com/wezterm/wezterm.git /home/wezterm

{copy_commands}

RUN bash /home/prepare.sh

"""
        return dockerfile_content.format(pr=self.pr, copy_commands=copy_commands)


@Instance.register("wezterm", "wezterm_175_to_29")
class WEZTERM_175_TO_29(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in log.splitlines():
            line = line.strip()

            match = re.match(r"test (\S+) ... ok", line)
            if match:
                passed_tests.add(match.group(1))

            match = re.match(r"test (\S+) ... FAILED", line)
            if match:
                failed_tests.add(match.group(1))

            match = re.match(r"test (\S+) ... ignored", line)
            if match:
                skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
