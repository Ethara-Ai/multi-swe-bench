import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TradingAgentsImageBase(Image):
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
        return "python:3.12-slim"

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
ENV CI=true

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
"""


class TradingAgentsImageDefault(Image):
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
        return TradingAgentsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git diff --name-only
git diff --name-only --cached
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{repo}

git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install deps — handle both eras
if [ -f pyproject.toml ]; then
    pip install --no-cache-dir -e . 2>&1 || true
fi
if [ -f requirements.txt ] && [ "$(head -1 requirements.txt)" != "." ]; then
    pip install --no-cache-dir -r requirements.txt 2>&1 || true
fi
# Test framework — not declared in pyproject.toml
pip install --no-cache-dir pytest 2>&1 || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
echo "No run stage — gold data only"
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

# Ensure clean state
git checkout -- go.mod go.sum 2>/dev/null || rm -f go.mod go.sum 2>/dev/null || true

# Apply test patch
git apply --reject /home/test.patch 2>&1 || true

# Install any new deps
if [ -f pyproject.toml ]; then
    pip install --no-cache-dir -e . 2>&1 || true
fi

# Run tests — handle both eras
if [ -d tests/ ]; then
    python -m pytest tests/ -v --tb=short 2>&1
fi

# PR#13: no standard tests, test_patch adds test.py
if [ -f test.py ] && [ ! -d tests/ ]; then
    python test.py 2>&1
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

git checkout -- go.mod go.sum 2>/dev/null || rm -f go.mod go.sum 2>/dev/null || true

git apply --reject /home/test.patch 2>&1 || true
git apply --reject /home/fix.patch 2>&1 || true

if [ -f pyproject.toml ]; then
    pip install --no-cache-dir -e . 2>&1 || true
fi

if [ -d tests/ ]; then
    python -m pytest tests/ -v --tb=short 2>&1
fi

if [ -f test.py ] && [ ! -d tests/ ]; then
    python test.py 2>&1
fi
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(
            f"COPY {f.name} /home/" for f in self.files()
        )
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("TauricResearch", "TradingAgents")
class TradingAgents(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TradingAgentsImageDefault(self.pr, self._config)

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        log = ansi_re.sub("", log)

        # pytest verbose: tests/xxx.py::test_name PASSED/FAILED/SKIPPED
        pytest_verbose_re = re.compile(
            r"^(tests/\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)",
            re.MULTILINE,
        )
        for m in pytest_verbose_re.finditer(log):
            name = m.group(1)
            status = m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        # pytest compact: tests/xxx.py .F.sxX...
        pytest_compact_re = re.compile(
            r"^(tests/\S+\.py)\s+([.FEsxX]+)", re.MULTILINE
        )
        for m in pytest_compact_re.finditer(log):
            test_file = m.group(1)
            results = m.group(2)
            for i, symbol in enumerate(results):
                test_name = f"{test_file}::test_{i + 1}"
                if symbol == ".":
                    passed_tests.add(test_name)
                elif symbol in ("F", "E"):
                    failed_tests.add(test_name)
                elif symbol in ("s", "x", "X"):
                    skipped_tests.add(test_name)

        # FAILED tests/xxx.py::test_name - AssertionError (summary lines)
        failed_summary_re = re.compile(
            r"^FAILED\s+(tests/\S+::\S+)", re.MULTILINE
        )
        for m in failed_summary_re.finditer(log):
            name = m.group(1)
            failed_tests.add(name)
            passed_tests.discard(name)

        # test.py (PR #13): check for tracebacks or error indicators
        traceback_re = re.compile(
            r"^Traceback \(most recent call last\):", re.MULTILINE
        )
        has_traceback = bool(traceback_re.search(log))

        error_re = re.compile(r"^(ERROR|FAIL|Failure|failed|Error)", re.MULTILINE)
        has_error = bool(error_re.search(log))

        test_py_ran = re.search(r"test\.py", log) or re.search(
            r"test_yfinance_dataflow", log
        )
        if test_py_ran:
            if has_traceback or has_error:
                failed_tests.add("test_yfinance_dataflow")
            else:
                passed_tests.add("test_yfinance_dataflow")

        # Deduplication: same test in both passed and failed → it failed
        passed_tests -= failed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
