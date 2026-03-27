import re
import json
from typing import Optional

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

    def dependency(self) -> str:
        return "python:3.13-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-py313"

    def workdir(self) -> str:
        return "base-py313"

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

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y --no-install-recommends bash && rm -rf /var/lib/apt/lists/*;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/

{self.global_env}

{code}

{self.clear_env}

"""


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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self.config)

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
                """git reset --hard
###ACTION_DELIMITER###
git checkout {pr.base.sha}
###ACTION_DELIMITER###
pip install coverage pytest 'pytest-xdist>=3.0.2' 'pytest-cov>=4.1.0' || true
###ACTION_DELIMITER###
pip install -e .[d] || true
###ACTION_DELIMITER###
echo -e '#!/bin/bash
coverage erase
pytest tests --run-optional no_jupyter --numprocesses auto --cov -v
pytest tests --run-optional jupyter -m jupyter --numprocesses auto --cov --cov-append -v
coverage report' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
export CI=true
cd /home/{pr.repo}
coverage erase
pytest tests --run-optional no_jupyter --numprocesses auto --cov -v
pytest tests --run-optional jupyter -m jupyter --numprocesses auto --cov --cov-append -v
coverage report

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export CI=true
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
coverage erase
pytest tests --run-optional no_jupyter --numprocesses auto --cov -v
pytest tests --run-optional jupyter -m jupyter --numprocesses auto --cov --cov-append -v
coverage report

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export CI=true
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
coverage erase
pytest tests --run-optional no_jupyter --numprocesses auto --cov -v
pytest tests --run-optional jupyter -m jupyter --numprocesses auto --cov --cov-append -v
coverage report

""".format(pr=self.pr),
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


@Instance.register("psf", "black_5046_to_4884")
class BLACK_5046_TO_4884(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        import re
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        test_status = {}
        pattern = re.compile(
            r"\[gw\d+\]\s+\[\s*\d+%\]\s+(PASSED|SKIPPED|FAILED)\s+(tests/.*?)\s*$"
        )
        for line in log.split("\n"):
            match = pattern.search(line)
            if match:
                status = match.group(1)
                test_name = match.group(2).strip()
                test_status[test_name] = status
        for test, status in test_status.items():
            if status == "PASSED":
                passed_tests.add(test)
            elif status == "SKIPPED":
                skipped_tests.add(test)
            elif status == "FAILED":
                failed_tests.add(test)
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
