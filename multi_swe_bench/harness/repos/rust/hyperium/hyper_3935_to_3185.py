import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# futures-util is declared with default-features=false, missing "alloc" needed for join_all.
# Also pin to 0.3.31 since 0.3.32 further restricts the alloc feature gate.
_DEP_PIN = """
python3 /home/fix_futures_util.py
cargo generate-lockfile
cargo update -p futures-util --precise 0.3.31 || true
"""


class ImageDefault(Image):
    """v1.x era (PRs 3935-3185): hyper 1.x on rust:latest with futures-util pin."""

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
                "fix_futures_util.py",
                "#!/usr/bin/env python3\n"
                "import re\n"
                "with open('Cargo.toml', 'r') as f:\n"
                "    content = f.read()\n"
                "line = content.split('futures-util')[1].split('\\n')[0] if 'futures-util' in content else ''\n"
                "if 'alloc' not in line:\n"
                "    content = re.sub(\n"
                "        r'(futures-util\\s*=\\s*\\{[^}]*)}',\n"
                "        r'\\1, features = [\"alloc\"]}',\n"
                "        content,\n"
                "        count=1,\n"
                "    )\n"
                "    with open('Cargo.toml', 'w') as f:\n"
                "        f.write(content)\n",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

export RUSTFLAGS="--cap-lints warn"
{dep_pin}
cargo test --features full || true

""".format(pr=self.pr, dep_pin=_DEP_PIN),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export RUSTFLAGS="--cap-lints warn"
{dep_pin}
cargo test --features full

""".format(pr=self.pr, dep_pin=_DEP_PIN),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
export RUSTFLAGS="--cap-lints warn"
{dep_pin}
cargo test --features full

""".format(pr=self.pr, dep_pin=_DEP_PIN),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
export RUSTFLAGS="--cap-lints warn"
{dep_pin}
cargo test --features full

""".format(pr=self.pr, dep_pin=_DEP_PIN),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """FROM rust:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git python3

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr, copy_commands=copy_commands)


@Instance.register("hyperium", "hyper_3935_to_3185")
class HYPER_3935_TO_3185(Instance):
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

        passed_pattern = re.compile(r"test (.*) \.\.\. ok")
        failed_pattern = re.compile(r"test (.*) \.\.\. FAILED")
        ignored_pattern = re.compile(r"test (.*) \.\.\. ignored")

        for line in log.splitlines():
            if passed_match := passed_pattern.search(line):
                passed_tests.add(passed_match.group(1).strip())
            elif failed_match := failed_pattern.search(line):
                failed_tests.add(failed_match.group(1).strip())
            elif ignored_match := ignored_pattern.search(line):
                skipped_tests.add(ignored_match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
