import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageDefault928(Image):
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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "mswebench"

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
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
pip install setuptools==46.1.3
pip install asynctest==0.13.0 freezegun==0.3.15 pytest==5.4.1 pytest-asyncio==0.10.0 pytest-timeout==1.3.4 coverage==5.0.4
pip install --no-build-isolation -e .
pip install 'packaging<22' keystoneauth1 pbr python-swiftclient
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
python -m pytest src/bandersnatch/tests/ -v --timeout=600
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
pip install --no-build-isolation -e .
python -m pytest src/bandersnatch/tests/ -v --timeout=600
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pip install --no-build-isolation -e .
python -m pytest src/bandersnatch/tests/ -v --timeout=600
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """FROM python:3.9-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then yum install -y bash; \
        else exit 1; fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

{copy_commands}

RUN bash /home/prepare.sh
""".format(pr=self.pr, copy_commands=copy_commands)


@Instance.register("pypa", "bandersnatch_928_to_469")
class BANDERSNATCH_928_TO_469(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault928(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        pattern = r"^(.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAILED|XPASSED)\s+\[\s*\d+%\s*\]$"
        regex = re.compile(pattern)
        for line in test_log.split("\n"):
            line = line.strip()
            match = regex.match(line)
            if match:
                test_name = match.group(1)
                status = match.group(2)
                if status in ("PASSED", "XPASSED"):
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR", "XFAILED"):
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
