import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "python:3.6-slim"

    def image_tag(self) -> str:
        return "base-py36"

    def workdir(self) -> str:
        return "base-py36"

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

        parts = [f"FROM {image_name}"]
        if self.global_env:
            parts.append(self.global_env)
        parts.append("WORKDIR /home/")
        parts.append("RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*")
        parts.append("RUN pip install --upgrade pip setuptools wheel")
        parts.append(code)
        if self.clear_env:
            parts.append(self.clear_env)
        return "\n\n".join(parts) + "\n"


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}
pip install -e . || true
pip install "pytest<7" pytest-xprocess requests pyopenssl greenlet watchdog pytest-timeout || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
sed -i 's/^\\[pytest\\]/[tool:pytest]/' setup.cfg || true
pytest --no-header -rA --tb=no -p no:cacheprovider --timeout=300

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
sed -i 's/^\\[pytest\\]/[tool:pytest]/' setup.cfg || true
pytest --no-header -rA --tb=no -p no:cacheprovider --timeout=300

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
sed -i 's/^\\[pytest\\]/[tool:pytest]/' setup.cfg || true
pytest --no-header -rA --tb=no -p no:cacheprovider --timeout=300

""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("pallets", "werkzeug_1065_to_1062")
class WERKZEUG_1065_TO_1062(Instance):
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
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        passed_pattern = re.compile(
            r"^PASSED\s+(.+?)$", re.MULTILINE
        )
        failed_pattern = re.compile(
            r"^FAILED\s+(.+?)(?:\s+-\s+.*)?$", re.MULTILINE
        )
        skipped_pattern = re.compile(
            r"^SKIPPED\s+\[\d+\]\s+([\w/\.\-]+):(\d+):", re.MULTILINE
        )
        error_pattern = re.compile(
            r"^ERROR\s+(.+?)(?:\s+-\s+.*)?$", re.MULTILINE
        )

        for match in passed_pattern.finditer(clean_log):
            passed_tests.add(match.group(1).strip())

        for match in failed_pattern.finditer(clean_log):
            failed_tests.add(match.group(1).strip())

        for match in skipped_pattern.finditer(clean_log):
            test_name = f"{match.group(1)}::{match.group(2).strip()}"
            skipped_tests.add(test_name)

        for match in error_pattern.finditer(clean_log):
            failed_tests.add(match.group(1).strip())

        # Dedup: passed -= failed, passed -= skipped, skipped -= failed
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
