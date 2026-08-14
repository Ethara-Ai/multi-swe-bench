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
        return "python:3.9-slim"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest -v -rA pysus/tests/
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v -rA pysus/tests/
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v -rA pysus/tests/
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {self.dependency()}
ENV DEBIAN_FRONTEND=noninteractive
# cffi C-extension needs libffi-dev + gcc; geopandas/georasters need GDAL/GEOS/PROJ.
RUN apt-get update && apt-get install -y \\
    git build-essential libffi-dev \\
    libgdal-dev gdal-bin libgeos-dev libproj-dev proj-bin \\
    && rm -rf /var/lib/apt/lists/*
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
# Base image `git clone` can land an incomplete packfile; fetch the base commit by URL if missing.
RUN git cat-file -e {self.pr.base.sha} 2>/dev/null || git fetch --no-tags https://github.com/{self.pr.org}/{self.pr.repo}.git {self.pr.base.sha}
RUN git checkout {self.pr.base.sha}

# --- Environment baked in so human_mode=True works.
RUN pip install --no-cache-dir -U pip setuptools wheel cffi
# match the GDAL python binding to the system libgdal (needed by georasters)
RUN pip install --no-cache-dir "GDAL==$(gdal-config --version)" || true
RUN pip install --no-cache-dir -e . || pip install --no-cache-dir .
RUN pip install --no-cache-dir pytest flake8

{copy_commands}"""


@Instance.register("AlertaDengue", "PySUS")
class PySUS(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for raw in log.split("\n"):
            line = raw.strip()
            m = re.match(r"^(PASSED|FAILED|ERROR|SKIPPED)\s+(\S+)", line)
            if m:
                status, name = m.group(1), m.group(2)
            else:
                m = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)", line)
                if not m:
                    continue
                name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
