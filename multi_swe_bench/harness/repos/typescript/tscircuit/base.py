import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TscircuitCoreImageBase(Image):
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

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    curl \
    git \
    unzip \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:$PATH"

{code}

{self.clear_env}

"""


class TscircuitCoreImageDefault(Image):
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
        return TscircuitCoreImageBase(self.pr, self.config)

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

bun install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
bun test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='bun.lockb' --exclude='*.png' /home/test.patch
bun test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='bun.lockb' --exclude='*.png' /home/test.patch /home/fix.patch
bun test

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


@Instance.register("tscircuit", "core")
@Instance.register("tscircuit", "checks")
@Instance.register("tscircuit", "circuit-json-to-gltf")
@Instance.register("tscircuit", "circuit-json-to-step")
@Instance.register("tscircuit", "circuit-to-svg")
@Instance.register("tscircuit", "cli")
@Instance.register("tscircuit", "common")
@Instance.register("tscircuit", "contribution-tracker")
@Instance.register("tscircuit", "easyeda-converter")
@Instance.register("tscircuit", "eval")
@Instance.register("tscircuit", "footprinter")
@Instance.register("tscircuit", "jscad-electronics")
@Instance.register("tscircuit", "jscad-fiber")
@Instance.register("tscircuit", "jscad-to-gltf")
@Instance.register("tscircuit", "kicad-component-converter")
@Instance.register("tscircuit", "poppygl")
@Instance.register("tscircuit", "runframe")
@Instance.register("tscircuit", "schematic-trace-solver")
@Instance.register("tscircuit", "solver-utils")
@Instance.register("tscircuit", "stepts")
@Instance.register("tscircuit", "svg.tscircuit.com")
@Instance.register("tscircuit", "template-api-fake")
@Instance.register("tscircuit", "tscircuit-autorouter")
@Instance.register("tscircuit", "tscircuit.com")
class TSCIRCUIT_CORE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return TscircuitCoreImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Track current test file for unique test identification
        # bun test outputs file headers like: "tests/pcb/pcb-silkscreen-text.test.ts:"
        current_file: str = ""
        re_file_header = re.compile(
            r"^([^\s:]+\.(?:test\.ts|test\.tsx|spec\.ts|spec\.tsx)):$"
        )

        # Color/TTY format: ✓ test name [1.23ms]
        re_pass_color = re.compile(r"^\s*✓\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")
        re_fail_color = re.compile(r"^\s*✗\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")
        re_skip_color = re.compile(r"^\s*»\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")

        # Non-color/Docker format: (pass) test name [1.23ms]
        re_pass_plain = re.compile(
            r"^\s*\(pass\)\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$"
        )
        re_fail_plain = re.compile(
            r"^\s*\(fail\)\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$"
        )
        re_skip_plain = re.compile(
            r"^\s*\(skip\)\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$"
        )

        # Todo format (bun's test.todo() - count as skipped)
        re_todo_color = re.compile(r"^\s*✎\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$")
        re_todo_plain = re.compile(
            r"^\s*\(todo\)\s+(.+?)(?:\s+\[[\d.]+(?:µs|ms|s)\])?\s*$"
        )

        # Strip ANSI escape codes that bun emits in color mode
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        def make_unique_name(test_name: str) -> str:
            if current_file:
                return f"{current_file} > {test_name}"
            return test_name

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()

            # Check for file header line (e.g., "tests/sch/resistor.test.tsx:")
            file_match = re_file_header.match(line)
            if file_match:
                current_file = file_match.group(1)
                continue

            match = re_pass_color.match(line)
            if match:
                passed_tests.add(make_unique_name(match.group(1)))
                continue

            match = re_fail_color.match(line)
            if match:
                failed_tests.add(make_unique_name(match.group(1)))
                continue

            match = re_skip_color.match(line)
            if match:
                skipped_tests.add(make_unique_name(match.group(1)))
                continue

            match = re_pass_plain.match(line)
            if match:
                passed_tests.add(make_unique_name(match.group(1)))
                continue

            match = re_fail_plain.match(line)
            if match:
                failed_tests.add(make_unique_name(match.group(1)))
                continue

            match = re_skip_plain.match(line)
            if match:
                skipped_tests.add(make_unique_name(match.group(1)))
                continue

            match = re_todo_color.match(line)
            if match:
                skipped_tests.add(make_unique_name(match.group(1)))
                continue

            match = re_todo_plain.match(line)
            if match:
                skipped_tests.add(make_unique_name(match.group(1)))
                continue

        # Dedup: worst result wins (failed > skipped > passed)
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
