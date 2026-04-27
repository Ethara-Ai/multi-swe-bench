import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks from a unified diff string.

    Binary hunks (e.g. .snap snapshot files) cannot be applied with
    ``git apply`` when the patch was generated without ``--full-index``.
    Stripping them is safe because snapshot tests regenerate these files.
    """
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )


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
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _strip_binary_diffs(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _strip_binary_diffs(self.pr.test_patch),
            ),
            File(
                ".",
                "strip_binary_diffs.py",
                '''#!/usr/bin/env python3
"""Strip binary diffs from a patch file so git apply doesn't choke on them."""
import re
import sys

def strip_binary_diffs(patch_path):
    with open(patch_path, 'r', errors='replace') as f:
        content = f.read()
    diffs = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
    text_diffs = [d for d in diffs if d.strip() and 'Binary files' not in d and 'GIT binary patch' not in d]
    with open(patch_path, 'w') as f:
        f.write(''.join(text_diffs))

if __name__ == '__main__':
    for path in sys.argv[1:]:
        strip_binary_diffs(path)
''',
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

yarn install --mode=skip-build || true
yarn build || true
yarn build:test || true
yarn test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn install --mode=skip-build
yarn build
yarn build:test
yarn test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
python3 /home/strip_binary_diffs.py /home/test.patch
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
yarn install --mode=skip-build
yarn build
yarn build:test
yarn test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
yarn install --mode=skip-build
yarn build
yarn build:test
yarn test

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """FROM rust:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV RUSTFLAGS="-A dependency_on_unit_never_type_fallback -A never_type_fallback_flowing_into_unsafe"

RUN apt-get update && apt-get install -y git curl gnupg build-essential python3 && \\
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \\
    apt-get install -y nodejs && \\
    corepack enable && \\
    rm -rf /var/lib/apt/lists/*

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


@Instance.register("napi-rs", "napi_rs_1658_to_730")
class NapiRs1658To730(Instance):
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

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_escape.sub("", test_log)

        # ava: "  ✔ <test>" / "  ✘ <test>"
        re_ava_pass = re.compile(r"^\s*\u2714\s+(.+?)(?:\s+\(\d+.*?\))?\s*$")
        re_ava_fail = re.compile(r"^\s*\u2718\s+(.+?)(?:\s+\(\d+.*?\))?\s*$")
        re_ava_skip = re.compile(r"^\s*-\s+(.+\u203a.+?)(?:\s+\(\d+.*?\))?\s*$")

        # rust: "test <name> ... ok/FAILED/ignored"
        re_rust_pass = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+ok$")
        re_rust_fail = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED$")
        re_rust_skip = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+ignored$")

        pass_regexes = [re_ava_pass, re_rust_pass]
        fail_regexes = [re_ava_fail, re_rust_fail]
        skip_regexes = [re_ava_skip, re_rust_skip]

        for line in test_log.splitlines():
            stripped = line.strip()
            matched = False

            for regex in pass_regexes:
                match = regex.match(stripped)
                if match:
                    passed_tests.add(match.group(1).strip())
                    matched = True
                    break

            if not matched:
                for regex in fail_regexes:
                    match = regex.match(stripped)
                    if match:
                        failed_tests.add(match.group(1).strip())
                        matched = True
                        break

            if not matched:
                for regex in skip_regexes:
                    match = regex.match(stripped)
                    if match:
                        skipped_tests.add(match.group(1).strip())
                        break

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
