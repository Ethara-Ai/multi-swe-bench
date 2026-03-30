import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.typescript.pmndrs.jotai import JotaiImageBase


class JotaiVitestYarnImageDefault(Image):
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
        return JotaiImageBase(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh
yarn install || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
npx vitest run --reporter=verbose

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --exclude='*.png' --exclude='*.jpg' --exclude='*.ico' --exclude='*.woff2' --exclude='yarn.lock' --whitespace=nowarn /home/test.patch
yarn install || true
npx vitest run --reporter=verbose

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --exclude='*.png' --exclude='*.jpg' --exclude='*.ico' --exclude='*.woff2' --exclude='yarn.lock' --whitespace=nowarn /home/test.patch /home/fix.patch
yarn install || true
npx vitest run --reporter=verbose

""".format(repo=self.pr.repo),
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


@Instance.register("pmndrs", "jotai_2492_to_1919")
class JotaiVitestYarn(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JotaiVitestYarnImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # vitest --reporter=verbose output:
        # pass: " ✓ tests/vanilla/types.test.tsx > atom() should return the correct types"
        # fail: " ❯ tests/file.test.tsx > test name" or " ✗ tests/file.test.tsx > test name"
        # skip: " ↓ tests/file.test.tsx > test name"
        re_pass = re.compile(r"^\s*[✓✔]\s+(.+)$")
        re_fail = re.compile(r"^\s*[❯✗×✕]\s+(.+)$")
        re_skip = re.compile(r"^\s*[↓]\s+(.+)$")

        # Strip vitest timing metadata from test names: trailing "1ms", "150ms", "2s"
        timing_re = re.compile(r"\s+\d+m?s$")

        for line in test_log.splitlines():
            pass_match = re_pass.match(line)
            if pass_match:
                test_name = timing_re.sub("", pass_match.group(1).strip())
                if test_name not in failed_tests:
                    passed_tests.add(test_name)

            fail_match = re_fail.match(line)
            if fail_match:
                test_name = timing_re.sub("", fail_match.group(1).strip())
                failed_tests.add(test_name)
                passed_tests.discard(test_name)

            skip_match = re_skip.match(line)
            if skip_match:
                test_name = timing_re.sub("", skip_match.group(1).strip())
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
