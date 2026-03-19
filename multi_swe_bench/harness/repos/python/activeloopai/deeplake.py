import re

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
        return "python:3.8"

    def image_prefix(self) -> str:
        return "envagent"

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
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/deeplake
git reset --hard
###ACTION_DELIMITER###
git checkout {base_sha}
###ACTION_DELIMITER###
pip install -r hub/requirements/common.txt || true
###ACTION_DELIMITER###
pip install -r hub/requirements/tests.txt || true
###ACTION_DELIMITER###
pip install -e . || true""".format(base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
pytest --no-header -rA --tb=short -p no:cacheprovider --local --ignore=hub/auto/tests/test_ingestion.py --ignore=hub/integrations/tests/test_huggingface.py

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest --no-header -rA --tb=short -p no:cacheprovider --local --ignore=hub/auto/tests/test_ingestion.py --ignore=hub/integrations/tests/test_huggingface.py

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest --no-header -rA --tb=short -p no:cacheprovider --local --ignore=hub/auto/tests/test_ingestion.py --ignore=hub/integrations/tests/test_huggingface.py

""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# Dockerfile for activeloopai/deeplake harness environment

FROM {dep}

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
RUN git clone https://github.com/{org}/{repo}.git /home/{repo}
"""
        dockerfile_content += f"""
{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content.format(
            dep=self.dependency(),
            org=self.pr.org,
            repo=self.pr.repo,
        )


@Instance.register("activeloopai", "deeplake")
class Deeplake(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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

    def parse_log(self, test_log: str) -> TestResult:
        # Parse the log content and extract test execution results.
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape.sub("", test_log)

        # Patterns for pytest -rA output
        patterns = {
            "passed_tests": re.compile(r"^PASSED (.+)$", re.MULTILINE),
            "failed_tests": re.compile(r"^FAILED (.+)$", re.MULTILINE),
            "skipped_tests": re.compile(r"^SKIPPED (.+)$", re.MULTILINE),
        }

        for status, pattern in patterns.items():
            for match in pattern.findall(test_log):
                test_name = match.strip()
                if status == "passed_tests":
                    passed_tests.add(test_name)
                elif status == "failed_tests":
                    failed_tests.add(test_name)
                elif status == "skipped_tests":
                    skipped_tests.add(test_name)

        # Dedup: worst result wins
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
