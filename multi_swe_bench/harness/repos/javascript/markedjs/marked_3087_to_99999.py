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
        return "base-nodetest"

    def workdir(self) -> str:
        return "base-nodetest"

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

npm install || true
npm run build || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
npm run build 2>/dev/null || true
npm run test:specs 2>&1
npm run test:unit 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm run build 2>/dev/null || true
npm run test:specs 2>&1
npm run test:unit 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm run build 2>/dev/null || true
npm run test:specs 2>&1
npm run test:unit 2>&1

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


@Instance.register("markedjs", "marked_3087_to_99999")
class MARKED_3087_TO_99999(Instance):
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

        lines = test_log.splitlines()

        # Parse node --test spec reporter output
        # Pass: ✔ test name (Xms)
        # Fail: ✗ test name (Xms)
        # Skip: ℹ skipped: test name
        # Suite start: ▶ Suite Name
        # Suite pass: ✔ Suite Name (Xms)
        # Suite fail: ✗ Suite Name (Xms)

        # Track nesting to build full test paths
        current_path = []
        indent_stack = []

        for line in lines:
            # Calculate indentation
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            stripped = stripped.strip()

            if not stripped:
                continue

            # Suite start
            if stripped.startswith("▶ "):
                suite_name = stripped[2:].strip()
                # Adjust nesting based on indentation
                while indent_stack and indent <= indent_stack[-1]:
                    indent_stack.pop()
                    if current_path:
                        current_path.pop()
                current_path.append(suite_name)
                indent_stack.append(indent)
                continue

            # Passing test
            pass_match = re.match(
                r"^[✔✓]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?(?:ms|s)\))?$", stripped
            )
            if pass_match:
                test_name = pass_match.group(1).strip()
                # Check if this is a suite completion (matches a current_path entry)
                if current_path and test_name == current_path[-1]:
                    # Suite completion, pop the stack
                    if indent_stack:
                        indent_stack.pop()
                    current_path.pop()
                    continue
                # Individual test
                if current_path:
                    full_name = " > ".join(current_path) + " > " + test_name
                else:
                    full_name = test_name
                passed_tests.add(full_name)
                continue

            # Failing test
            fail_match = re.match(
                r"^[✗✕×✖]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?(?:ms|s)\))?$", stripped
            )
            if fail_match:
                test_name = fail_match.group(1).strip()
                # Check if this is a suite completion
                if current_path and test_name == current_path[-1]:
                    if indent_stack:
                        indent_stack.pop()
                    current_path.pop()
                    continue
                # Individual test
                if current_path:
                    full_name = " > ".join(current_path) + " > " + test_name
                else:
                    full_name = test_name
                failed_tests.add(full_name)
                continue

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

        # Remove any overlap (prefer failed status)
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
