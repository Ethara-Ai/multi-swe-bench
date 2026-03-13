import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult


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
                """ls
###ACTION_DELIMITER###
ls tests
###ACTION_DELIMITER###
pip install -e ".[testing]"
###ACTION_DELIMITER###
pip install -e ".[dev]"
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
echo 'python -m pytest -v --no-header -rA --tb=short tests/' > test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo_name}
# Extract test files that are referenced in test.patch and already exist at base commit
TEST_FILES=$(grep -oP '(?<=diff --git a/)tests/[^\\s]*test_[^\\s]+\\.py' /home/test.patch 2>/dev/null | sort -u | tr '\\n' ' ')
EXISTING_FILES=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        EXISTING_FILES="$EXISTING_FILES $f"
    fi
done
if [ -n "$EXISTING_FILES" ]; then
    python -m pytest -v --no-header -rA --tb=short $EXISTING_FILES 2>&1
else
    echo "No pre-existing test files found in test patch - all tests are new"
fi
""".format(
                    repo_name=repo_name
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply for test.patch failed" >&2
    exit 1
fi
TEST_FILES=$(grep -oP '(?<=diff --git a/)tests/[^\\s]*test_[^\\s]+\\.py' /home/test.patch 2>/dev/null | sort -u | tr '\\n' ' ')
if [ -n "$TEST_FILES" ]; then
    python -m pytest -v --no-header -rA --tb=short $TEST_FILES 2>&1
else
    python -m pytest -v --no-header -rA --tb=short tests/ 2>&1
fi
""".format(
                    repo_name=repo_name
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
TEST_FILES=$(grep -oP '(?<=diff --git a/)tests/[^\\s]*test_[^\\s]+\\.py' /home/test.patch 2>/dev/null | sort -u | tr '\\n' ' ')
if [ -n "$TEST_FILES" ]; then
    python -m pytest -v --no-header -rA --tb=short $TEST_FILES 2>&1
else
    python -m pytest -v --no-header -rA --tb=short tests/ 2>&1
fi
""".format(
                    repo_name=repo_name
                ),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    curl \\
    build-essential \\
    libssl-dev \\
    libffi-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then \\
    if command -v apk >/dev/null 2>&1; then \\
        apk add --no-cache bash; \\
    elif command -v apt-get >/dev/null 2>&1; then \\
        apt-get update && apt-get install -y bash; \\
    fi \\
fi

WORKDIR /home/
{copy_commands}
RUN git clone https://github.com/huggingface/transformers.git /home/transformers

WORKDIR /home/transformers

RUN git reset --hard
RUN git checkout {base_sha}

RUN pip install --no-cache-dir pytest pytest-xdist timeout-decorator psutil parameterized

RUN pip install --no-cache-dir -e ".[testing]" 2>/dev/null || \\
    pip install --no-cache-dir -e ".[dev]" 2>/dev/null || \\
    (pip install --no-cache-dir numpy tokenizers datasets sentencepiece regex sacremoses filelock requests 2>/dev/null && \\
     pip install --no-deps --no-cache-dir -e . 2>/dev/null) || \\
    echo "Warning: all install strategies failed"

RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu 2>/dev/null || \\
    pip install --no-cache-dir torch 2>/dev/null || \\
    true

CMD ["/bin/bash"]
""".format(
            copy_commands=copy_commands,
            base_sha=self.pr.base.sha,
        )


@Instance.register("huggingface", "transformers")
class HuggingFaceTransformers(Instance):
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

        # PASSED tests: "PASSED tests/test_trainer.py::Class::test_method[param]"
        passed_pattern = re.compile(r"PASSED\s+(\S+)")
        passed_tests.update(passed_pattern.findall(log))

        # XFAIL treated as passed (expected failure that actually xfailed)
        xfail_pattern = re.compile(r"XFAIL\s+(\S+)")
        passed_tests.update(xfail_pattern.findall(log))

        # FAILED tests from summary line
        failed_pattern = re.compile(r"FAILED\s+([\w/.\-]+\.py::[^\s]+)")
        failed_tests.update(failed_pattern.findall(log))

        # ERROR tests from summary line
        error_pattern = re.compile(r"ERROR\s+([\w/.\-]+\.py::[^\s]+)")
        failed_tests.update(error_pattern.findall(log))

        # SKIPPED tests from -rA short test summary
        skipped_pattern = re.compile(r"SKIPPED\s+(\S+)")
        skipped_tests.update(skipped_pattern.findall(log))

        # Ensure strict disjointness: passed takes priority over failed, failed over skipped
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
