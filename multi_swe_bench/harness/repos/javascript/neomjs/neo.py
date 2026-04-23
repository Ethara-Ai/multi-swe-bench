import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class NeoImageBase(Image):
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


class NeoImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return NeoImageBase(self.pr, self._config)

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
apt-get update --fix-missing || true
npx playwright install-deps chromium || true
npx playwright install chromium || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npx playwright test --reporter=list 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npx playwright test --reporter=list 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npx playwright test --reporter=list 2>&1 || true
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


@Instance.register("neomjs", "neo")
class Neo(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NeoImageDefault(self.pr, self._config)

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

        # Playwright list reporter format:
        #   ✓  1 [chromium] › test/playwright/unit/vdom/Advanced.spec.mjs:5:5 › test name (2.5s)
        #   ✘  2 [chromium] › test/playwright/unit/state/Provider.spec.mjs:3:5 › test name (500ms)
        #   -  3 [chromium] › test/playwright/unit/vdom/Advanced.spec.mjs:8:5 › test name

        pass_patterns = [
            re.compile(r"^[\s]*[✔✓]\s+(.*?)(?:\s+\([\d\.]+\s*\w+\))?$"),
        ]

        fail_patterns = [
            re.compile(r"^[\s]*[✘✗×✖]\s+(.*?)(?:\s+\([\d\.]+\s*\w+\))?$"),
            re.compile(r"^\s*\d+\)\s*(.+)$"),
        ]

        skip_patterns = [
            re.compile(r"^\s*-\s+(\d+\s+\[.*?\]\s+›\s+.*)$"),
        ]

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            matched = False

            for fail_re in fail_patterns:
                m = fail_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                    skipped_tests.discard(test_name)
                    matched = True
                    break
            if matched:
                continue

            for skip_re in skip_patterns:
                m = skip_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    if test_name not in failed_tests:
                        skipped_tests.add(test_name)
                        passed_tests.discard(test_name)
                    matched = True
                    break
            if matched:
                continue

            for pass_re in pass_patterns:
                m = pass_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    if test_name not in failed_tests and test_name not in skipped_tests:
                        passed_tests.add(test_name)
                    break

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
