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
        return "node:20"

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

RUN apt update && apt install -y git

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
npm ci --ignore-scripts || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npm test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
npm test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
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


class ImageDefault1761(Image):
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
npm ci --ignore-scripts || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npm run build
npm test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
npm run build
npm test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
npm run build
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


@Instance.register("TypeStrong", "typedoc")
class Typedoc(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 1761:
            return ImageDefault1761(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_escape.sub("", test_log)

        # Mocha spec reporter: indentation encodes suite hierarchy (2-space per level).
        # Tests are suite-qualified as "Suite > SubSuite > test name" to disambiguate
        # duplicate leaf names across different suites (e.g. "[specs] matches specs").
        re_pass = re.compile(r"^(\s*)[✓✔]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        re_inline_fail = re.compile(r"^(\s*)\d+\)\s+(.+)$")
        re_suite = re.compile(r"^(\s+)(\S.*)$")
        re_summary_pass = re.compile(r"^\s*\d+\s+passing")
        re_summary_fail = re.compile(r"^\s*\d+\s+failing")
        re_fail_entry = re.compile(r"^\s+\d+\)\s+(.+)$")

        suite_stack: list[tuple[int, str]] = []
        in_failure_section = False

        def _qualified_name(stack: list[tuple[int, str]], test: str) -> str:
            parts = [s for _, s in stack]
            parts.append(test)
            return " > ".join(parts)

        lines = test_log.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if not stripped:
                i += 1
                continue

            if re_summary_fail.match(stripped):
                in_failure_section = True
                i += 1
                continue

            if re_summary_pass.match(stripped):
                i += 1
                continue

            if not in_failure_section:
                pass_match = re_pass.match(line)
                if pass_match:
                    indent = len(pass_match.group(1))
                    test_name = pass_match.group(2).strip()
                    while suite_stack and suite_stack[-1][0] >= indent:
                        suite_stack.pop()
                    passed_tests.add(_qualified_name(suite_stack, test_name))
                    i += 1
                    continue

                inline_fail = re_inline_fail.match(line)
                if inline_fail:
                    indent = len(inline_fail.group(1))
                    test_name = inline_fail.group(2).strip()
                    while suite_stack and suite_stack[-1][0] >= indent:
                        suite_stack.pop()
                    failed_tests.add(_qualified_name(suite_stack, test_name))
                    i += 1
                    continue

                suite_match = re_suite.match(line)
                if suite_match:
                    indent = len(suite_match.group(1))
                    name = suite_match.group(2).strip()
                    if name.startswith("(") or name.startswith(">") or name.startswith("Using ") or name.startswith("Documentation "):
                        i += 1
                        continue
                    while suite_stack and suite_stack[-1][0] >= indent:
                        suite_stack.pop()
                    suite_stack.append((indent, name))
                    i += 1
                    continue

            else:
                # Failure summary: "  1) Suite\n       SubSuite\n         test name:"
                fail_entry = re_fail_entry.match(line)
                if fail_entry:
                    parts = [fail_entry.group(1).strip()]
                    i += 1
                    while i < len(lines):
                        cont_line = lines[i]
                        cont_stripped = cont_line.strip()
                        if not cont_stripped:
                            break
                        if cont_stripped.endswith(":"):
                            parts.append(cont_stripped.rstrip(":").strip())
                            i += 1
                            break
                        if re_fail_entry.match(cont_line):
                            break
                        if any(cont_stripped.startswith(p) for p in [
                            "Assertion", "Error", "TypeError", "at ",
                            "+", "-", "Uncaught", "expected", "actual"
                        ]):
                            break
                        parts.append(cont_stripped)
                        i += 1
                    failed_tests.add(" > ".join(parts))
                    continue

            i += 1

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
