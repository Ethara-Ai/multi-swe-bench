import re
import json
from typing import Optional, Union

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
        return "python:3.10-slim"

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
                """ls -la
###ACTION_DELIMITER###
apt-get update && apt-get install -y build-essential python3-dev libyaml-dev
###ACTION_DELIMITER###
pip install pdm
###ACTION_DELIMITER###
pdm config python.use_venv false
###ACTION_DELIMITER###
pdm lock --update-reuse || true
###ACTION_DELIMITER###
pdm install --dev -G:all --frozen-lockfile || pdm install --dev -G:all --no-lock || true
###ACTION_DELIMITER###
pip install -e . --no-deps || true
###ACTION_DELIMITER###
cp -r /usr/local/lib/python3.10/site-packages/litestar*.dist-info /home/litestar/__pypackages__/3.10/lib/ 2>/dev/null || true
###ACTION_DELIMITER###
echo '#!/bin/bash' > test_commands.sh && echo 'pdm run pytest tests docs/examples -v -rA --tb=no -p no:cacheprovider' >> test_commands.sh
###ACTION_DELIMITER###
chmod +x test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/[[REPO_NAME]]
pdm run pytest tests docs/examples -v -rA --tb=no -p no:cacheprovider

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/[[REPO_NAME]]
# Strip binary hunks from patches before applying
python3 -c "
import re, sys
for f in sys.argv[1:]:
    c = open(f).read()
    c = re.sub(r'diff --git[^\\n]*\\n(?:(?:(?!diff --git).)*)GIT binary patch.*?(?=diff --git|\\Z)', '', c, flags=re.DOTALL)
    c = re.sub(r'diff --git[^\\n]*\\n(?:(?:(?!diff --git).)*)Binary files[^\\n]*differ\\n?(?:(?:(?!diff --git).)*)(?=diff --git|\\Z)', '', c, flags=re.DOTALL)
    open(f, 'w').write(c)
" /home/test.patch
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.lock' /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pdm run pytest tests docs/examples -v -rA --tb=no -p no:cacheprovider

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/[[REPO_NAME]]
# Strip binary hunks from patches before applying
python3 -c "
import re, sys
for f in sys.argv[1:]:
    c = open(f).read()
    c = re.sub(r'diff --git[^\\n]*\\n(?:(?:(?!diff --git).)*)GIT binary patch.*?(?=diff --git|\\Z)', '', c, flags=re.DOTALL)
    c = re.sub(r'diff --git[^\\n]*\\n(?:(?:(?!diff --git).)*)Binary files[^\\n]*differ\\n?(?:(?:(?!diff --git).)*)(?=diff --git|\\Z)', '', c, flags=re.DOTALL)
    open(f, 'w').write(c)
" /home/test.patch /home/fix.patch
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.lock' /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pdm run pytest tests docs/examples -v -rA --tb=no -p no:cacheprovider

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

# Choose an appropriate base image based on the project's requirements - replace python:3.11-slim with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM python:3.10-slim

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/litestar-org/litestar.git /home/litestar

WORKDIR /home/litestar
RUN git reset --hard
RUN git checkout {pr.base.sha}

# Install dependencies
RUN apt-get update && apt-get install -y build-essential python3-dev libyaml-dev
RUN pip install pdm
ENV PDM_CHECK_UPDATE=false
RUN pdm config python.use_venv false
RUN pdm lock --update-reuse || true
RUN pdm install --dev -G:all --frozen-lockfile || pdm install --dev -G:all --no-lock || true
RUN pip install -e . --no-deps || true
RUN cp -r /usr/local/lib/python3.10/site-packages/litestar*.dist-info /home/litestar/__pypackages__/3.10/lib/ 2>/dev/null || true
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("litestar-org", "litestar_3169_to_2363")
class LITESTAR_3169_TO_2363(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        # Parse the log content and extract test execution results.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed_tests = set[str]()  # Tests that passed successfully
        failed_tests = set[str]()  # Tests that failed
        skipped_tests = set[str]()  # Tests that were skipped

        # Regex pattern to match test cases with their statuses
        # Captures status (PASSED, FAILED, etc.) and test name, ignoring trailing error messages
        pattern = re.compile(
            r".*?\b(PASSED|FAILED|SKIPPED|ERROR|XFAILED|XPASSED|RERUN)\b.*?(tests/[\w/:\.\[\]@,-]+)",
            re.IGNORECASE,  # Case-insensitive to handle any case variations
        )
        # Find all matches in the log content
        matches = pattern.findall(test_log)
        for status, test_name in matches:
            status = status.upper()
            test_name = test_name.strip()  # Remove any leading/trailing whitespace
            if status in {"PASSED", "XPASSED"}:
                passed_tests.add(test_name)
            elif status in {"FAILED", "ERROR", "XFAILED", "RERUN"}:
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)
        passed_tests -= failed_tests
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
