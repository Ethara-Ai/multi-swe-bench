import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _clean_test_name(name: str) -> str:
    """Strip variable timing and metadata from test names for stable eval matching."""
    # Strip jest file-level metadata: (2 tests) 75ms, (1 test | 1 failed) 120ms
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    # Strip parenthesized timing: (75ms), (150 ms), (8.954 s)
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


class ObsidianTasksImageBase(Image):
    """Base Docker image: Node 18 with Yarn 1 (classic)."""

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
        return "node:18"

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
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git"
                f" /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive

{code}

{self.clear_env}
"""


class ObsidianTasksImageDefault(Image):
    """Per-PR image: checks out base SHA, installs deps, stages patches."""

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
        return ObsidianTasksImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

yarn install --frozen-lockfile
""".format(
                    repo=self.pr.repo, base_sha=self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
yarn test
""".format(
                    repo=self.pr.repo
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn --exclude='*.png' /home/test.patch || \
git apply --whitespace=nowarn --exclude='*.png' --reject /home/test.patch || true
yarn test
""".format(
                    repo=self.pr.repo
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn --exclude='*.png' /home/test.patch /home/fix.patch || \
git apply --whitespace=nowarn --exclude='*.png' --reject /home/test.patch /home/fix.patch || true
yarn test
""".format(
                    repo=self.pr.repo
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(
            f"COPY {file.name} /home/" for file in self.files()
        )

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("obsidian-tasks-group", "obsidian-tasks")
class ObsidianTasks(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ObsidianTasksImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes for clean parsing
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.splitlines():
            stripped = line.strip()

            # File-level PASS: "PASS tests/Sort.test.ts (3.456 s)"
            if stripped.startswith("PASS "):
                passed_tests.add(_clean_test_name(stripped[5:].strip()))
                continue

            # File-level FAIL: "FAIL tests/Sort.test.ts"
            if stripped.startswith("FAIL "):
                failed_tests.add(_clean_test_name(stripped[5:].strip()))
                continue

            # Individual test passed: "✓ sorts correctly by default order (11 ms)"
            m = re.match(
                r"\s*[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$", stripped
            )
            if m:
                passed_tests.add(_clean_test_name(m.group(1)))
                continue

            # Individual test failed: "✕ should fail (1 ms)"
            m = re.match(
                r"\s*[✕✗×]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$", stripped
            )
            if m:
                failed_tests.add(_clean_test_name(m.group(1)))
                continue

            # Individual test skipped/pending: "○ skipped test name"
            m = re.match(
                r"\s*○\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$", stripped
            )
            if m:
                skipped_tests.add(_clean_test_name(m.group(1)))
                continue

        # A test that ever failed is not passed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
