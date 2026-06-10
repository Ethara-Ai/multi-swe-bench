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
        return "python:3.12-bookworm"

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
                """ls -la
###ACTION_DELIMITER###
apt-get update && apt-get install -y libsasl2-dev libldap2-dev libssl-dev
###ACTION_DELIMITER###
pip install poetry
###ACTION_DELIMITER###
poetry install || (poetry lock && poetry install)
###ACTION_DELIMITER###
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/test.patch ) || true
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/test.patch ) || true
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/fix.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/fix.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/fix.patch ) || true
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential patch libsasl2-dev libldap2-dev libssl-dev

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/mealie-recipes/mealie.git /home/mealie

WORKDIR /home/mealie
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install --upgrade pip && pip install poetry
RUN poetry install || (poetry lock && poetry install)
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("mealie-recipes", "mealie_poetry_py312")
class MEALIE_POETRY_PY312(Instance):
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

        # pytest `-rA` short test summary lines:
        #   PASSED tests/unit_tests/test_config.py::test_name[a b]
        #   FAILED tests/unit_tests/test_config.py::test_name - AssertionError: ...
        #   ERROR  tests/unit_tests/test_x.py::test_y - ...
        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(.+?)\s*$", re.MULTILINE
        )
        for status, name in summary_pattern.findall(log):
            if status in ("FAILED", "ERROR"):
                name = re.sub(r"\s+-\s.*$", "", name).strip()
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name.strip())
            # XFAIL / XPASS: expected-fail bookkeeping, not real pass/fail

        # Grouped skip summary: SKIPPED [6] tests/unit_tests/test_x.py:18: reason
        for m in re.finditer(
            r"^SKIPPED\s+\[\d+\]\s+(\S+?):(\d+):", log, re.MULTILINE
        ):
            skipped_tests.add(f"{m.group(1)}:{m.group(2)}")

        # Defensive fallback: verbose per-test lines `nodeid STATUS [ 12%]`
        verbose_pattern = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$",
            re.MULTILINE,
        )
        for name, status in verbose_pattern.findall(log):
            name = name.strip()
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

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
