import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


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
        return "node:18-bullseye"

    def image_prefix(self) -> str:
        return "envagent"

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

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
       git ca-certificates firefox-esr xvfb xauth \\
    && rm -rf /var/lib/apt/lists/*

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

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
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
npm install --no-audit --no-fund || npm install --legacy-peer-deps --no-audit --no-fund

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export CI=true
xvfb-run npm test -- --browsers Firefox --single-run --verbose 2>&1; echo "TEST_DONE_WITH_EXIT: $?"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
xvfb-run npm test -- --browsers Firefox --single-run --verbose 2>&1; echo "TEST_DONE_WITH_EXIT: $?"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
xvfb-run npm test -- --browsers Firefox --single-run --verbose 2>&1; echo "TEST_DONE_WITH_EXIT: $?"

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("aframevr", "aframe_1748_to_1094")
class AFRAME_1748_TO_1094(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped
        import re
        import json

        # Helper function to strip ANSI escape codes
        def strip_ansi(s):
            return re.sub(r"\x1b\[[0-9;]*m", "", s)

        # Strip ANSI codes from the log
        stripped_log = strip_ansi(log)
        lines = stripped_log.split("\n")
        current_hierarchy = []
        for line in lines:
            # Strip line number prefix (e.g., [   1] )
            line = re.sub(r"^\[\s*\d+\]\s*", "", line)
            leading_spaces = len(line) - len(line.lstrip(" "))
            content = line.lstrip(" ")
            # Skip lines with log warnings/errors
            if "WARN" in content or "ERROR" in content:
                continue
            if "✔" in content:
                # Extract test description
                test_desc = content.split("✔", 1)[1].strip()
                # Skip summary phrases
                if "tests completed" in test_desc or "tests failed" in test_desc:
                    continue
                # Combine with current hierarchy
                full_test_name = ".".join(current_hierarchy + [test_desc])
                passed_tests.add(full_test_name)
            elif "✖" in content:
                test_desc = content.split("✖", 1)[1].strip()
                # Skip summary phrases
                if "tests failed" in test_desc:
                    continue
                full_test_name = ".".join(current_hierarchy + [test_desc])
                failed_tests.add(full_test_name)
            else:
                # Check if it's a section line (not a test)
                # Ignore empty lines
                if not content:
                    continue
                # Skip log lines, section headers, header commands, and summary timings
                if (
                    content.startswith(
                        (
                            "Firefox",
                            "LOG:",
                            "INFO",
                            "WARN",
                            "ERROR",
                            "15 09 2025",
                            "START:",
                            "SUMMARY:",
                            ">",
                        )
                    )
                    or "Finished in" in content
                ):
                    continue
                # Skip error messages and summary phrases
                if (
                    "Expected" in content
                    or "AssertionError" in content
                    or "tests completed" in content
                    or "tests failed" in content
                ):
                    continue
                # Handle FAILED TESTS section by resetting hierarchy
                if content == "FAILED TESTS:":
                    current_hierarchy = []
                    continue
                # Determine hierarchy level (assuming 2 spaces per level)
                level = leading_spaces // 2
                # Update current hierarchy
                if level == 0:
                    current_hierarchy = [content]
                else:
                    # Truncate to (level - 1) and append the new section
                    current_hierarchy = current_hierarchy[: level - 1] + [content]
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
