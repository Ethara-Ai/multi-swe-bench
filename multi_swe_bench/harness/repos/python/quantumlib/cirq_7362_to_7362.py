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
        return "python:3.11-slim"

    def image_prefix(self) -> str:
        return "envagent"

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
set -e
cd /home/[[REPO_NAME]]
apt-get update && apt-get install -y --no-install-recommends curl gnupg
###ACTION_DELIMITER###
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
###ACTION_DELIMITER###
apt-get install -y nodejs
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
pip install -e "./cirq-core" -e "./cirq-google" -e "./cirq-ionq" -e "./cirq-aqt" -e "./cirq-pasqal" -e "./cirq-web" -e "./cirq-rigetti" 2>/dev/null || pip install -e "./cirq-core" -e "./cirq-google" -e "./cirq-web"
###ACTION_DELIMITER###
cd /home/[[REPO_NAME]]/cirq-web/cirq_ts && npm install
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]/cirq-web/cirq_ts
npx mocha --reporter spec --ignore 'e2e/**' 2>&1
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# test_patch renames cirq_ts/ -> cirq_web/, so cd into cirq_web after apply
cd /home/[[REPO_NAME]]/cirq-web/cirq_web
npm install
npx mocha --reporter spec --ignore 'e2e/**' 2>&1
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# both patches rename cirq_ts/ -> cirq_web/, so cd into cirq_web after apply
cd /home/[[REPO_NAME]]/cirq-web/cirq_web
npm install
npx mocha --reporter spec --ignore 'e2e/**' 2>&1
""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = f"""
FROM python:3.11-slim

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential pkg-config libpixman-1-dev libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content


@Instance.register("quantumlib", "Cirq_7362_to_7362")
class CIRQ_7362_TO_7362(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Mocha spec reporter: "    ✓ test description" (pass) / "    N) test description" (fail)
        passing_pattern = re.compile(r"^\s+[✓✔]\s+(.+)$")
        failing_pattern = re.compile(r"^\s+\d+\)\s+(.+)$")
        pending_pattern = re.compile(r"^\s+-\s+(.+)$")

        current_suite = ""
        for line in log.split("\n"):
            suite_match = re.match(r"^\s{2}(\S.+)$", line)
            if suite_match and not passing_pattern.match(line) and not failing_pattern.match(line) and not pending_pattern.match(line):
                current_suite = suite_match.group(1).strip()
                continue

            pass_match = passing_pattern.match(line)
            if pass_match:
                test_name = pass_match.group(1).strip()
                if current_suite:
                    test_name = f"{current_suite} > {test_name}"
                passed_tests.add(test_name)
                continue

            fail_match = failing_pattern.match(line)
            if fail_match:
                test_name = fail_match.group(1).strip()
                if current_suite:
                    test_name = f"{current_suite} > {test_name}"
                failed_tests.add(test_name)
                continue

            pend_match = pending_pattern.match(line)
            if pend_match:
                test_name = pend_match.group(1).strip()
                if current_suite:
                    test_name = f"{current_suite} > {test_name}"
                skipped_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
