import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class ImageDefault(Image):
    """Gatsby v1.x lerna-monorepo era (PRs 1537-1611): packages/*, Jest test
    runner with tests in packages/*/src/__tests__/, Node 8."""

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
        return "node:8"

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
                """npm install -g npm@6 || true
###ACTION_DELIMITER###
npm install --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund --ignore-scripts 2>/dev/null || true
###ACTION_DELIMITER###
./node_modules/.bin/lerna bootstrap 2>/dev/null || true
###ACTION_DELIMITER###
./node_modules/.bin/jest --version 2>/dev/null || npm install --no-save jest 2>/dev/null || true""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
./node_modules/.bin/jest --verbose --ci 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
./node_modules/.bin/jest --verbose --ci 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
./node_modules/.bin/jest --verbose --ci 2>&1
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """
FROM node:8

ENV DEBIAN_FRONTEND=noninteractive
ENV CI=true
ENV npm_config_audit=false
ENV npm_config_fund=false

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN npm install -g npm@6 || true
RUN npm install --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund --ignore-scripts 2>/dev/null || true
RUN ./node_modules/.bin/lerna bootstrap 2>/dev/null || true
RUN ./node_modules/.bin/jest --version 2>/dev/null || npm install --no-save jest 2>/dev/null || true

{copy_commands}
""".format(pr=self.pr, copy_commands=copy_commands)


def parse_jest_log(log: str) -> TestResult:
    """Parse Jest --verbose output: `✓ title` / `✕ title` / `○ skipped title`."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass = re.compile(r"^[✓√]\s+(.+?)\s*(?:\(\d+\s*m?s\))?$")
    re_fail = re.compile(r"^[✕✗×]\s+(.+?)\s*(?:\(\d+\s*m?s\))?$")
    re_skip = re.compile(r"^[○✎]\s+(?:skipped\s+)?(.+?)\s*(?:\(\d+\s*m?s\))?$")
    # Jest --verbose prints a `PASS|FAIL <file>` header before each file's
    # test lines; qualify leaf test names with the file (monorepo: leaf names
    # like "handles empty configs" recur across packages).
    re_file = re.compile(r"^(?:PASS|FAIL)\s+(\S+\.(?:js|jsx|ts|tsx|snap)?\S*)")
    cur_file = ""

    def q(name: str) -> str:
        return f"{cur_file}::{name}" if cur_file else name

    for line in log.splitlines():
        line = ANSI_ESCAPE.sub("", line).strip()
        m = re_file.match(line)
        if m:
            cur_file = m.group(1)
            continue
        m = re_fail.match(line)
        if m:
            failed_tests.add(q(m.group(1).strip()))
            continue
        m = re_pass.match(line)
        if m:
            passed_tests.add(q(m.group(1).strip()))
            continue
        m = re_skip.match(line)
        if m:
            skipped_tests.add(q(m.group(1).strip()))

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


@Instance.register("gatsbyjs", "gatsby_1611_to_1537")
class GATSBY_1611_TO_1537(Instance):
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
        return parse_jest_log(log)
