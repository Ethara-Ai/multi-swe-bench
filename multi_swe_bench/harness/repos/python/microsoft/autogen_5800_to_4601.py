"""microsoft/autogen — era 0.4 (monorepo: python/, uv workspace).

Covers number_interval: autogen_5800_to_4601 (autogen v0.4.x; PR numbers
~#4601..#5800, e.g. #4920/#4940 v0.4.x and the autogenstudio/magentic-one
transitional PRs #5164/#5181).

Discovered interactively in Docker (python:3.11-slim), base sha 78ac9f8507
(#4940, v0.4.0..v0.4.1) and 9494ac97a0 (#5181):
  pip install uv
  cd python
  uv venv .venv
  uv pip install --python .venv "setuptools<81" wheel pip
  uv sync --dev --no-build-isolation-package openai-whisper
  .venv/bin/python -m pytest -v --no-header -rA --tb=no -p no:cacheprovider \
      --continue-on-collection-errors $(find ./packages -type d -name tests)

`uv sync` resolves autogen-ext[video-surfer] -> openai-whisper@20240930, whose
setup.py imports pkg_resources; modern setuptools (>=81) dropped pkg_resources,
so the isolated build fails. Fix: seed .venv with setuptools<81 and build that
one package without isolation. Result: #5181 -> 264 passed / 5 failed /
33 skipped / 3 collection errors.
"""

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
                """pip install uv
###ACTION_DELIMITER###
cd /home/[[REPO_NAME]]/python && uv venv .venv
###ACTION_DELIMITER###
cd /home/[[REPO_NAME]]/python && uv pip install --python .venv "setuptools<81" wheel pip
###ACTION_DELIMITER###
cd /home/[[REPO_NAME]]/python && uv sync --dev --no-build-isolation-package openai-whisper || true
###ACTION_DELIMITER###
echo '#!/bin/bash
cd /home/[[REPO_NAME]]/python
export OPENAI_API_KEY=dummy
.venv/bin/python -m pytest -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors $(find ./packages -type d -name tests)' > /home/[[REPO_NAME]]/test_commands.sh && chmod +x /home/[[REPO_NAME]]/test_commands.sh""".replace(
                    "[[REPO_NAME]]", repo_name
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/[[REPO_NAME]]/python
export OPENAI_API_KEY=dummy
.venv/bin/python -m pytest -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors $(find ./packages -type d -name tests)

""".replace(
                    "[[REPO_NAME]]", repo_name
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.zip' --exclude='*.whl' --exclude='*.gz' --exclude='*.jar' --exclude='*.bin' /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
cd /home/[[REPO_NAME]]/python
export OPENAI_API_KEY=dummy
.venv/bin/python -m pytest -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors $(find ./packages -type d -name tests)

""".replace(
                    "[[REPO_NAME]]", repo_name
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.zip' --exclude='*.whl' --exclude='*.gz' --exclude='*.jar' --exclude='*.bin' /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
cd /home/[[REPO_NAME]]/python
export OPENAI_API_KEY=dummy
.venv/bin/python -m pytest -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors $(find ./packages -type d -name tests)

""".replace(
                    "[[REPO_NAME]]", repo_name
                ),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git build-essential ffmpeg && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/microsoft/autogen.git /home/autogen

WORKDIR /home/autogen
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("microsoft", "autogen_5800_to_4601")
class AUTOGEN_5800_TO_4601(Instance):
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

        # Strip ANSI colour codes first so the regexes match coloured output.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # Verbose per-test line, e.g.
        #   test/x.py::test_a[p q] PASSED            [ 12%]
        #   test/x.py::test_b SKIPPED (reason) [  0%]
        # Anchored on the trailing "[ NN%]" so parametrised ids that contain
        # spaces are captured intact.
        verbose = re.compile(
            r"^(?P<name>.+?\.py::.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b.*\[\s*\d+%\]\s*$"
        )
        # pytest -rA short test summary, e.g.
        #   PASSED test/x.py::test_a
        #   FAILED test/x.py::test_b - AssertionError: ...
        summary = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|XPASS|XFAIL)\s+(?P<name>.+?\.py::.+?)(?:\s+-\s.*)?\s*$"
        )
        for raw in log.split("\n"):
            line = raw.strip()
            m = verbose.match(line) or summary.match(line)
            if not m:
                continue
            name = m.group("name")
            status = m.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # Enforce TestResult disjointness invariants: a test reported both
        # passed and failed (e.g. a rerun) counts as failed; a skip never
        # overrides a real pass/fail.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
