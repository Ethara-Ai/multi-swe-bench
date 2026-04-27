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


class NapiRsImageBase(Image):
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
        return "rust:latest"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y curl gnupg && \\
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \\
    apt-get install -y nodejs && \\
    corepack enable && \\
    rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class NapiRsImageDefault(Image):
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
        return NapiRsImageBase(self.pr, self.config)

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

yarn install --mode=skip-build || true
yarn build:test || true
yarn test || true
cargo test -p napi-examples || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn install --mode=skip-build
yarn build:test
yarn test
cargo test -p napi-examples

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
python3 /home/strip_binary_diffs.py /home/test.patch
git apply --whitespace=nowarn /home/test.patch
yarn install --mode=skip-build
yarn build:test
yarn test
cargo test -p napi-examples

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
yarn install --mode=skip-build
yarn build:test
yarn test
cargo test -p napi-examples

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


@Instance.register("napi-rs", "napi-rs")
class NapiRs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NapiRsImageDefault(self.pr, self._config)

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

        # ava: "  ✔ <test>" / "  ✔ <test> (123ms)"
        re_ava_pass = re.compile(r"^\s*\u2714\s+(.+?)(?:\s+\(\d+.*?\))?\s*$")
        # ava: "  ✘ <test>"
        re_ava_fail = re.compile(r"^\s*\u2718\s+(.+?)(?:\s+\(\d+.*?\))?\s*$")
        # ava skipped: "  - <test>" (must contain › to avoid matching non-ava lines)
        re_ava_skip = re.compile(r"^\s*-\s+(.+\u203a.+?)(?:\s+\(\d+.*?\))?\s*$")

        # rust: "test <name> ... ok/FAILED/ignored"
        re_rust_pass = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+ok$")
        re_rust_fail = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED$")
        re_rust_skip = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+ignored$")

        # TAP: "ok 1 - <name>" / "not ok 1 - <name>"
        re_tap_pass = re.compile(r"^ok\s+\d+\s+-\s+(.+)$")
        re_tap_fail = re.compile(r"^not ok\s+\d+\s+-\s+(.+)$")

        pass_regexes = [re_ava_pass, re_rust_pass, re_tap_pass]
        fail_regexes = [re_ava_fail, re_rust_fail, re_tap_fail]
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
