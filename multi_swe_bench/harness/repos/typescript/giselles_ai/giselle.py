import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GiselleImageBase(Image):
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
        return "node:22.14.0"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

RUN npm install -g pnpm@10.0.0

{code}

{self.clear_env}

"""


class GiselleImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return GiselleImageBase(self.pr, self.config)

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

pnpm install --no-frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

export CI=true
cd /home/{pr.repo}
pnpm install --no-frozen-lockfile || true
pnpm exec vitest run --reporter=verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

export CI=true
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || (git checkout . && git clean -fd && git apply --exclude pnpm-lock.yaml --exclude bun.lockb --whitespace=nowarn /home/test.patch)
pnpm install --no-frozen-lockfile || true
pnpm exec vitest run --reporter=verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

export CI=true
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || (git checkout . && git clean -fd && git apply --exclude pnpm-lock.yaml --exclude bun.lockb --whitespace=nowarn /home/test.patch /home/fix.patch)
pnpm install --no-frozen-lockfile || true
pnpm exec vitest run --reporter=verbose || true

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


@Instance.register("giselles-ai", "giselle")
class Giselle(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GiselleImageDefault(self.pr, self._config)

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
        # Trailing per-test duration printed by the vitest reporter, e.g. " 405ms".
        re_duration = re.compile(r"\s+\d+(?:\.\d+)?\s*(?:ms|s)$")
        # vitest --reporter=verbose emits one line per test:
        #   "<marker> <file> > <suite> > <test name>"
        # The test id is normalized to "<file>::<suite>::<test name>", so every
        # level of the test tree is separated the same way (pytest convention).
        re_test = re.compile(
            r"^(?P<marker>[✓×✗↓»])\s+"
            r"(?P<file>\S.*?\.(?:test|spec)\.[cm]?[jt]sx?)"
            r"(?:\s+>\s+(?P<name>.+))?$"
        )

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            line = re_duration.sub("", line)

            match = re_test.match(line)
            if not match:
                continue

            test = match.group("file")
            name = match.group("name")
            if name:
                suite_path = "::".join(
                    part.strip() for part in name.split(">") if part.strip()
                )
                test = f"{test}::{suite_path}"

            marker = match.group("marker")
            if marker == "✓":
                passed_tests.add(test)
            elif marker in ("×", "✗"):
                failed_tests.add(test)
            else:
                skipped_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
