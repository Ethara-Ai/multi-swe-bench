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
        return "python:3.8-slim"

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
                """python -m pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
python -m pip install "setuptools-scm[toml]>=6.2.3"
###ACTION_DELIMITER###
python -m pip install tox virtualenv
###ACTION_DELIMITER###
python -m pip install -e ".[testing]" || python -m pip install -e .
###ACTION_DELIMITER###
python -m pip install --force-reinstall "attrs==24.2.0" "iniconfig==2.0.0" "hypothesis==6.113.0" "xmlschema"
###ACTION_DELIMITER###
python -m pip install --no-cache-dir pytest-json-report
###ACTION_DELIMITER###
echo 'python -m pytest --no-header -rA --tb=short -v testing/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/pytest
python -m pytest --no-header -rA --tb=short -v testing/

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/pytest
if ! git -C /home/pytest apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest --no-header -rA --tb=short -v testing/

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/pytest
if ! git -C /home/pytest apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest --no-header -rA --tb=short -v testing/

""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.8-slim

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y git build-essential

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/pytest-dev/pytest.git /home/pytest

WORKDIR /home/pytest
RUN git reset --hard
RUN git fetch origin {pr.base.sha} 2>/dev/null || git fetch --unshallow 2>/dev/null || true
RUN git checkout {pr.base.sha}

RUN python -m pip install --upgrade pip setuptools wheel
RUN python -m pip install "setuptools-scm[toml]>=6.2.3"
RUN python -m pip install tox virtualenv

ENV VIRTUALENV_CREATE=false

RUN python -m pip install -e ".[testing]" || python -m pip install -e .
RUN python -m pip install --force-reinstall "attrs==24.2.0" "iniconfig==2.0.0" "hypothesis==6.113.0" "xmlschema"

RUN python -m pip install --no-cache-dir pytest-json-report \
    && printf '#!/bin/bash\\nexec python -m pytest "$@"\\n' > /usr/local/bin/pytest \
    && chmod +x /usr/local/bin/pytest

RUN python -c "import pytest; print('pytest', pytest.__version__)" || echo "WARNING: pytest import check failed"
"""
        dockerfile_content += f"""
{copy_commands}
"""
        dockerfile_content += """
CMD ["/bin/bash"]
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("pytest-dev", "pytest_13533_to_11864")
class PYTEST_13533_TO_11864(Instance):
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

        # Strip ANSI escape codes before parsing
        log = re.sub(r'\x1b\[[0-9;]*m', '', log)

        pattern = re.compile(
            r"^\s*(?:([^\s]+)\s+(PASSED|FAILED|SKIPPED)|(PASSED|FAILED|SKIPPED)\s+([^\s]+))(?:\s+\[.*?\])?\s*$",
            re.MULTILINE,
        )
        for match in pattern.finditer(log):
            test_name = (match.group(1) or match.group(4)).strip()
            status = match.group(2) or match.group(3)
            # Filter out inner subprocess test output — real tests start with "testing/"
            if not test_name.startswith("testing/"):
                continue
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Deduplicate: if a test appears in both passed and failed, keep only failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
