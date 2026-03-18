import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        return "node:20-bookworm"

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

WORKDIR /home/

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
        return ImageBase(self.pr, self._config)

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

npm install --legacy-peer-deps || true
npm install tslib 2>/dev/null || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
npm test
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm test

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


@Instance.register("markedjs", "marked_0_to_3086")
class MARKED_0_TO_3086(Instance):
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
        """Parse jasmine test output. Uses table section names as test identifiers
        for consistent f2p detection across stages."""
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_escape.sub("", test_log)

        lines = test_log.splitlines()

        current_flavor = ""
        # Regex: | Section N of M | X% | or | Section N of M X% |
        table_re = re.compile(
            r"^\|\s+(.+?)\s+(\d+)\s+of\s+(\d+)\s*(?:\|\s*)?(\d+)%\s*\|$"
        )
        flavor_re = re.compile(r"^\|\s+(CommonMark|GFM|Marked|New)\s*\|$")

        for line in lines:
            line = line.strip()

            flavor_match = flavor_re.match(line)
            if flavor_match:
                current_flavor = flavor_match.group(1)
                continue

            table_match = table_re.match(line)
            if table_match:
                section = table_match.group(1).strip()
                passed = int(table_match.group(2))
                total = int(table_match.group(3))
                failed_count = total - passed

                test_name = f"{current_flavor} > {section}" if current_flavor else section

                if failed_count == 0:
                    passed_tests.add(test_name)
                else:
                    failed_tests.add(test_name)
                continue

        # Strategy 2: Parse custom test runner output (v0.3.x)
        # Format: #N. Test name.
        #             passed/failed in X seconds
        custom_test_re = re.compile(r"^#(\d+)\.\s+Test\s+(.+)\.$")
        custom_result_re = re.compile(r"^\s+(passed|failed)\s+")

        last_test_name = None
        for line in lines:
            custom_match = custom_test_re.match(line)
            if custom_match:
                last_test_name = custom_match.group(2)
                continue

            if last_test_name:
                result_match = custom_result_re.match(line)
                if result_match:
                    status = result_match.group(1)
                    if status == "passed":
                        passed_tests.add(last_test_name)
                    else:
                        failed_tests.add(last_test_name)
                    last_test_name = None

        # Parse jasmine summary to get total counts
        # Format: "1578 specs, 0 failures" or "1578 specs, 1 failure"
        summary_re = re.compile(r"(\d+)\s+specs?,\s+(\d+)\s+failures?")
        total_specs = 0
        total_failures = 0
        for line in lines:
            summary_match = summary_re.search(line)
            if summary_match:
                total_specs = int(summary_match.group(1))
                total_failures = int(summary_match.group(2))
                break

        if not passed_tests and not failed_tests and total_specs > 0:
            total_passed = total_specs - total_failures
            for i in range(total_passed):
                passed_tests.add(f"spec_{i + 1}")
            for i in range(total_failures):
                failed_tests.add(f"failed_spec_{i + 1}")

        failed_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
