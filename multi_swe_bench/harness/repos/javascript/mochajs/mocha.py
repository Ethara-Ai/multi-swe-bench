import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Mocha test commands vary by PR era:
#
# Era 1 (PR 2746): bin/mocha, test/acceptance/interfaces/, test/*.spec.js,
#                   test/integration/*.js, test/reporters/*.js
# Era 2 (PRs 3212-5132): bin/mocha, test/interfaces/, test/unit/*.spec.js +
#                          test/node-unit/**/*.spec.js, test/integration/**/*.spec.js,
#                          test/reporters/*.spec.js
# Era 3 (PRs 5254-5408): bin/mocha.js, same paths as era 2

_ERA1_PRS = {2746}

_ERA3_PRS = {5254, 5351, 5379, 5383, 5408}


def _get_mocha_bin(pr_number: int) -> str:
    if pr_number in _ERA3_PRS:
        return "bin/mocha.js"
    return "bin/mocha"


def _get_test_commands(pr_number: int) -> str:
    mocha = _get_mocha_bin(pr_number)

    if pr_number in _ERA1_PRS:
        return f"""\
node {mocha} --reporter json --ui bdd test/acceptance/interfaces/bdd.spec 2>/dev/null || true
node {mocha} --reporter json --ui tdd test/acceptance/interfaces/tdd.spec 2>/dev/null || true
node {mocha} --reporter json --ui qunit test/acceptance/interfaces/qunit.spec 2>/dev/null || true
node {mocha} --reporter json --ui exports test/acceptance/interfaces/exports.spec 2>/dev/null || true
node {mocha} --reporter json test/acceptance/*.spec.js 2>/dev/null || true
node {mocha} --reporter json test/*.spec.js 2>/dev/null || true
node {mocha} --reporter json --timeout 10000 test/integration/*.js 2>/dev/null || true
node {mocha} --reporter json test/reporters/*.js 2>/dev/null || true"""

    # Era 2 and 3: same paths, different binary
    return f"""\
node {mocha} --reporter json --ui bdd test/interfaces/bdd.spec 2>/dev/null || true
node {mocha} --reporter json --ui tdd test/interfaces/tdd.spec 2>/dev/null || true
node {mocha} --reporter json --ui qunit test/interfaces/qunit.spec 2>/dev/null || true
node {mocha} --reporter json --ui exports test/interfaces/exports.spec 2>/dev/null || true
node {mocha} --reporter json "test/unit/*.spec.js" "test/node-unit/**/*.spec.js" 2>/dev/null || true
node {mocha} --reporter json --timeout 10000 "test/integration/**/*.spec.js" 2>/dev/null || true
node {mocha} --reporter json test/reporters/*.spec.js 2>/dev/null || true"""


def _get_node_version(pr_number: int) -> str:
    if pr_number <= 2746:
        return "node:8"
    if pr_number <= 4419:
        return "node:10"
    if pr_number <= 5132:
        return "node:14"
    return "node:20-bookworm"


class MochaImageBase(Image):
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
        return _get_node_version(self.pr.number)

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

RUN npm ci || npm install || true

{self.clear_env}
"""


class MochaImageDefault(Image):
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
        return MochaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_commands = _get_test_commands(self.pr.number)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {sha}

npm install || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash

cd /home/{repo}
{test_commands}
""".format(repo=self.pr.repo, test_commands=test_commands),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash

cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_commands}
""".format(repo=self.pr.repo, test_commands=test_commands),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash

cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_commands}
""".format(repo=self.pr.repo, test_commands=test_commands),
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


@Instance.register("mochajs", "mocha")
class Mocha(Instance):
    """Mocha test framework (mochajs/mocha). Uses JSON reporter, aggregates across multiple test commands."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MochaImageDefault(self.pr, self._config)

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
        """Parse mocha JSON reporter output. Multiple JSON blocks are aggregated."""
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Find all JSON blocks in the output (one per test command)
        # Each starts with { and ends with } at top level
        depth = 0
        start = None
        json_blocks = []

        for i, ch in enumerate(clean_log):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    block = clean_log[start : i + 1]
                    json_blocks.append(block)
                    start = None

        for block in json_blocks:
            try:
                data = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(data, dict):
                continue

            # Standard mocha JSON reporter format
            for test in data.get("passes", []):
                title = test.get("fullTitle", test.get("title", ""))
                if title:
                    passed_tests.add(title)

            for test in data.get("failures", []):
                title = test.get("fullTitle", test.get("title", ""))
                if title:
                    failed_tests.add(title)

            for test in data.get("pending", []):
                title = test.get("fullTitle", test.get("title", ""))
                if title:
                    skipped_tests.add(title)

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
