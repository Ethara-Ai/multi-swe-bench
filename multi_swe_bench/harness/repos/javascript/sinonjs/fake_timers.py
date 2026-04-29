import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FakeTimersImageBase(Image):
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

        return f"""\
FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

RUN npm install --ignore-scripts

{self.clear_env}
"""


class FakeTimersImageDefault(Image):
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
        return FakeTimersImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = "npx mocha --timeout 200 test/ integration-test/ --reporter json --check-leaks"

        npm_install_cmd = "npm install --ignore-scripts"

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
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
git checkout {sha}

{npm_install_cmd}
""".format(repo=self.pr.repo, sha=self.pr.base.sha, npm_install_cmd=npm_install_cmd),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --3way --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        name = dep.image_name()
        tag = dep.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""\
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("sinonjs", "fake-timers")
class FakeTimers(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FakeTimersImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Mocha JSON reporter outputs a JSON object.
        # The log may contain non-JSON preamble (e.g., npm output), so find the
        # outermost JSON object that contains a "tests" array.
        json_match = re.search(r"\{.*\"tests\"\s*:\s*\[.*\}", clean_log, re.DOTALL)
        if not json_match:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=set(),
                failed_tests=set(),
                skipped_tests=set(),
            )

        try:
            log_data = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=set(),
                failed_tests=set(),
                skipped_tests=set(),
            )

        tests = log_data.get("tests", [])
        for test in tests:
            test_name = test.get("fullTitle", test.get("title", "Unknown Test"))

            if test.get("pending", False) or test.get("skipped", False):
                skipped_tests.add(test_name)
                continue

            err = test.get("err", {})
            if err and (err.get("message") or err.get("stack")):
                failed_tests.add(test_name)
            else:
                passed_tests.add(test_name)

        # Deduplicate: worst result wins
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
