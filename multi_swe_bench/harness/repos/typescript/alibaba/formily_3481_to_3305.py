import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "node:16-bullseye"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
                """#!/bin/bash
set -eo pipefail
export NODE_OPTIONS=--max_old_space_size=4096
cd /home/{repo}
sed -i 's|https://registry.npm.taobao.org/[^"]*|https://registry.yarnpkg.com/|g' yarn.lock 2>/dev/null || true
sed -i 's|https://registry.npmmirror.com/[^"]*|https://registry.yarnpkg.com/|g' yarn.lock 2>/dev/null || true
yarn install --ignore-engines || true
yarn build || true
""".format(repo=repo_name),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export NODE_OPTIONS=--max_old_space_size=4096
cd /home/{repo}
./node_modules/.bin/jest --coverage --verbose
""".format(repo=repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export NODE_OPTIONS=--max_old_space_size=4096
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
./node_modules/.bin/jest --coverage --verbose
""".format(repo=repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export NODE_OPTIONS=--max_old_space_size=4096
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./node_modules/.bin/jest --coverage --verbose
""".format(repo=repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        repo_name = self.pr.repo
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """
FROM node:16-bullseye

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends git jq && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {base_sha}

{copy_commands}

RUN bash /home/prepare.sh
""".format(
            org=self.pr.org,
            repo=repo_name,
            base_sha=self.pr.base.sha,
            copy_commands=copy_commands,
        )


@Instance.register("alibaba", "formily_3481_to_3305")
class FORMILY_3481_TO_3305(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        current_file = ""
        describe_stack = []

        for line in log.splitlines():
            raw = ansi_re.sub("", line)
            clean = raw.strip()
            if not clean:
                continue

            # Track current test file: PASS/FAIL packages/core/...spec.ts
            file_match = re.match(r"(?:PASS|FAIL)\s+(.+\.(?:ts|tsx|js|jsx))", clean)
            if file_match:
                current_file = file_match.group(1).strip()
                describe_stack = []
                continue

            # Track describe blocks by indentation level
            indent = len(raw) - len(raw.lstrip())

            # Jest verbose: ✓ test name (X ms)
            pass_match = re.match(r"[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", clean)
            if pass_match:
                test_name = pass_match.group(1).strip()
                full_name = " > ".join(describe_stack + [test_name]) if describe_stack else test_name
                if current_file:
                    full_name = current_file + " > " + full_name
                passed_tests.add(full_name)
                continue

            # Jest verbose: ✕ test name (X ms)
            fail_match = re.match(r"[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", clean)
            if fail_match:
                test_name = fail_match.group(1).strip()
                full_name = " > ".join(describe_stack + [test_name]) if describe_stack else test_name
                if current_file:
                    full_name = current_file + " > " + full_name
                failed_tests.add(full_name)
                continue

            # Jest verbose: ○ skipped test name
            skip_match = re.match(r"[○]\s+skipped\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", clean)
            if skip_match:
                test_name = skip_match.group(1).strip()
                full_name = " > ".join(describe_stack + [test_name]) if describe_stack else test_name
                if current_file:
                    full_name = current_file + " > " + full_name
                skipped_tests.add(full_name)
                continue

            # Describe block: indented text without test markers
            if indent >= 2 and not re.match(r"(PASS|FAIL|Test Suites:|Tests:|Snapshots:|Time:|Ran all)", clean):
                if not re.match(r"[✓✔✕✗×○●]", clean) and not re.match(r"\d+\)", clean):
                    level = (indent - 2) // 2
                    describe_stack = describe_stack[:level]
                    describe_stack.append(clean)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
