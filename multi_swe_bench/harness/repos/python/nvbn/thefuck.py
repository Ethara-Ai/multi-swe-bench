import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TheFuckImageDefault(Image):
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
                """ls -la
###ACTION_DELIMITER###
pip install -Ur requirements.txt
###ACTION_DELIMITER###
pip install 'setuptools<71' 'pytest<8'
###ACTION_DELIMITER###
python setup.py develop""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --no-header -rA --tb=no -p no:cacheprovider -v tests/

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
pytest --no-header -rA --tb=no -p no:cacheprovider -v tests/

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
pytest --no-header -rA --tb=no -p no:cacheprovider -v tests/

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install -Ur requirements.txt
RUN pip install 'setuptools<71' 'pytest<8'
RUN python setup.py develop
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("nvbn", "thefuck")
class TheFuck(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TheFuckImageDefault(self.pr, self._config)

    _PYTEST_CMD = (
        "pytest --no-header -rA --tb=no -p no:cacheprovider -v tests/"
    )

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash -c 'cd /home/{repo} ; git reset --hard HEAD ; {pytest}'".format(
            repo=self.pr.repo, pytest=self._PYTEST_CMD
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git reset --hard HEAD ; "
            "git apply --whitespace=nowarn /home/test.patch || "
            "git apply --whitespace=nowarn --3way /home/test.patch || true ; "
            "{pytest}"
            "'".format(
                repo=self.pr.repo, pytest=self._PYTEST_CMD
            )
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git reset --hard HEAD ; "
            "git apply --whitespace=nowarn /home/test.patch || "
            "git apply --whitespace=nowarn --3way /home/test.patch || true ; "
            "git apply --whitespace=nowarn /home/fix.patch || "
            "git apply --whitespace=nowarn --3way /home/fix.patch || true ; "
            "{pytest}"
            "'".format(
                repo=self.pr.repo, pytest=self._PYTEST_CMD
            )
        )

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", log)
        lines = clean_log.splitlines()

        passed_patterns = [
            re.compile(r"^(.*?)\s+PASSED\s+\[\s*\d+%\]$"),
            re.compile(r"^PASSED\s+(.*?)$"),
        ]
        failed_patterns = [
            re.compile(r"^(.*?)\s+FAILED\s+\[\s*\d+%\]$"),
            re.compile(r"^FAILED\s+(.*?)(?: - .*)?$"),
        ]
        skipped_pattern = re.compile(
            r"^SKIPPED\s+(?:\[\d+\]\s+)?(tests/.*?(?:::\S+|\.py:\d+))\s*", re.MULTILINE
        )

        for line in lines:
            line = line.strip()
            for pattern in passed_patterns:
                match = pattern.match(line)
                if match:
                    passed_tests.add(match.group(1).strip())
                    break
            else:
                for pattern in failed_patterns:
                    match = pattern.match(line)
                    if match:
                        failed_tests.add(match.group(1).strip())
                        break
                else:
                    match = skipped_pattern.match(line)
                    if match:
                        skipped_tests.add(match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
