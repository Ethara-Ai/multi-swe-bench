import re
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
        # Returning a string lets the base Image.dockerfile() build the clone,
        # ${BASE_COMMIT} checkout and hardening block (per image.py).
        return "python:3.8"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        base_sha = self.pr.base.sha[:8] if hasattr(self.pr.base, "sha") else "base"
        return f"base-{base_sha}"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        # Runs in WORKDIR /home/woodwork after the BASE_COMMIT checkout.
        # Install woodwork (editable) plus its test dependencies for this era.
        return (
            "RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel\n"
            "RUN python -m pip install --no-cache-dir -e .\n"
            "RUN if [ -f test-requirements.txt ]; then "
            "python -m pip install --no-cache-dir -r test-requirements.txt; fi\n"
            "RUN python -m pip install --no-cache-dir pytest"
        )


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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
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
                "run.sh",
                """#!/bin/bash
cd /home/{repo}
python -m pytest --ignore=docs -v woodwork/

""".format(repo=repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest --ignore=docs -v woodwork/

""".format(repo=repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest --ignore=docs -v woodwork/

""".format(repo=repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("alteryx", "woodwork")
class WOODWORK(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped

        # pytest -v output, e.g.:
        #   woodwork/tests/...::test_name[param] PASSED [ 12%]
        test_pattern = re.compile(r"^(.*?)\s+(PASSED|SKIPPED|FAILED)\s+\[\s*\d+%\]$")
        error_pattern = re.compile(r"^ERROR\s+(.*)$")
        for line in log.split("\n"):
            line = line.strip()
            test_match = test_pattern.match(line)
            if test_match:
                test_name = test_match.group(1)
                status = test_match.group(2)
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
                elif status == "FAILED":
                    failed_tests.add(test_name)
                continue
            error_match = error_pattern.match(line)
            if error_match:
                test_name = error_match.group(1)
                failed_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
