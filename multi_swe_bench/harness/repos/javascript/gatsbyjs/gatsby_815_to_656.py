import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class ImageDefault(Image):
    """Gatsby v0 / pre-monorepo era (PRs 656-815): single package, top-level
    test/ directory, AVA test runner, Node 6."""

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
npm install --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund 2>/dev/null || true
###ACTION_DELIMITER###
./node_modules/.bin/ava --version 2>/dev/null || npm install --no-save ava@0.19 2>/dev/null || true""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
./node_modules/.bin/ava --tap test
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
./node_modules/.bin/ava --tap test
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
./node_modules/.bin/ava --tap test
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
RUN npm install --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund 2>/dev/null || true
RUN ./node_modules/.bin/ava --version 2>/dev/null || npm install --no-save ava@0.19 2>/dev/null || true

{copy_commands}
""".format(pr=self.pr, copy_commands=copy_commands)


def parse_ava_tap(log: str) -> TestResult:
    """Parse AVA TAP output: `ok N - title` / `not ok N - title` / `# SKIP`."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_ok = re.compile(r"^ok \d+ - (.+)$")
    re_not_ok = re.compile(r"^not ok \d+ - (.+)$")

    # AVA machinery messages emitted as TAP points but not real tests.
    _noise = ("No tests found", "Couldn't find any", "Error: ")

    for line in log.splitlines():
        line = ANSI_ESCAPE.sub("", line).strip()
        m = re_not_ok.match(line)
        if m:
            name = m.group(1).strip()
            if any(name.startswith(p) or p in name for p in _noise):
                continue
            failed_tests.add(name)
            continue
        m = re_ok.match(line)
        if m:
            name = m.group(1).strip()
            if "# SKIP" in name or "# skip" in name:
                skipped_tests.add(re.split(r"# [Ss][Kk][Ii][Pp]", name)[0].strip())
            else:
                passed_tests.add(name)

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


@Instance.register("gatsbyjs", "gatsby_815_to_656")
class GATSBY_815_TO_656(Instance):
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
        return parse_ava_tap(log)
