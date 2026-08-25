import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DataImageBase(Image):
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
        return "node:16"

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


class DataImageDefault(Image):
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
        return DataImageBase(self.pr, self.config)

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
                r"""#!/bin/bash
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

""",
            ),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PUPPETEER_SKIP_DOWNLOAD=1
export CI=1

yarn install --frozen-lockfile
yarn build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}

export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PUPPETEER_SKIP_DOWNLOAD=1
export CI=1

yarn build

npx jest --verbose --runInBand --forceExit --colors=false --testPathIgnorePatterns '/node_modules/|test/performance/|test/extensions/sync\.test\.ts|test/regressions/112-event-emitter-leak/|test/primaryKey\.test\.ts'

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}

export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PUPPETEER_SKIP_DOWNLOAD=1
export CI=1

git apply /home/test.patch

yarn build

npx jest --verbose --runInBand --forceExit --colors=false --testPathIgnorePatterns '/node_modules/|test/performance/|test/extensions/sync\.test\.ts|test/regressions/112-event-emitter-leak/|test/primaryKey\.test\.ts'

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}

export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PUPPETEER_SKIP_DOWNLOAD=1
export CI=1

git apply /home/test.patch /home/fix.patch

yarn build

npx jest --verbose --runInBand --forceExit --colors=false --testPathIgnorePatterns '/node_modules/|test/performance/|test/extensions/sync\.test\.ts|test/regressions/112-event-emitter-leak/|test/primaryKey\.test\.ts'

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


@Instance.register("mswjs", "data")
class Data(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DataImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", test_log)

        re_pass_suite = re.compile(r"^PASS\s+(\S+)(?:\s+\(.+\))?$")
        re_fail_suite = re.compile(r"^FAIL\s+(\S+)(?:\s+\(.+\))?$")
        re_pass_test = re.compile(r"^\s*[\u2713\u2714]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
        re_fail_test = re.compile(r"^\s*[\u2715\u00d7\u2717]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
        re_skip_test = re.compile(
            r"^\s*[\u25cb\u25cc]\s+(?:skipped\s+)?(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"
        )
        re_code_frame = re.compile(r"^\s*\d+\s*\|")

        current_suite = None
        describe_stack: list[tuple[int, str]] = []
        in_error_section = False
        seen_names: dict[str, int] = {}

        def record(name: str, bucket: set) -> None:
            occurrence = seen_names.get(name, 0) + 1
            seen_names[name] = occurrence
            bucket.add(name if occurrence == 1 else f"{name}#{occurrence}")

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            suite_match = re_pass_suite.match(line) or re_fail_suite.match(line)
            if suite_match:
                current_suite = suite_match.group(1)
                describe_stack = []
                in_error_section = False
                continue

            if (
                stripped.startswith("Test Suites:")
                or stripped.startswith("Tests:")
                or stripped.startswith("Snapshots:")
                or stripped.startswith("Ran all test suites")
            ):
                current_suite = None
                describe_stack = []
                in_error_section = False
                continue

            if stripped.startswith("\u25cf"):
                in_error_section = True
                continue

            if in_error_section or current_suite is None:
                continue

            indent = len(line) - len(line.lstrip())

            pass_match = re_pass_test.match(line)
            fail_match = re_fail_test.match(line)
            skip_match = re_skip_test.match(line)

            if pass_match or fail_match or skip_match:
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()

                if pass_match:
                    leaf = pass_match.group(1).strip()
                    bucket = passed_tests
                elif fail_match:
                    leaf = fail_match.group(1).strip()
                    bucket = failed_tests
                else:
                    leaf = skip_match.group(1).strip()
                    bucket = skipped_tests

                parts = [current_suite] + [name for _, name in describe_stack] + [leaf]
                record(" > ".join(parts), bucket)
                continue

            if indent < 2:
                continue

            if (
                stripped.startswith("at ")
                or stripped.startswith("console.")
                or stripped.startswith("expect(")
                or stripped.startswith("Expected")
                or stripped.startswith("Received")
                or re_code_frame.match(line)
            ):
                continue

            while describe_stack and describe_stack[-1][0] >= indent:
                describe_stack.pop()
            describe_stack.append((indent, stripped))

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
