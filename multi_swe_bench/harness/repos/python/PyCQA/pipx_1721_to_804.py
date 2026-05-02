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
                """ls
###ACTION_DELIMITER###
apt-get update && apt-get install -y git
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
pip install pytest pytest-cov
###ACTION_DELIMITER###
python -m pytest tests/ -v --tb=short --net-pypiserver
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
sed -i 's/if "python" in args and not Path(args.python).is_file():/if "python" in args and args.python is not None and not Path(args.python).is_file():/' src/pipx/main.py 2>/dev/null || true
pip install -e . pytest pytest-cov
ALLPKG_SHA="1e558d81f6e488a0d0598b5808e1aa519330fa03"
CURRENT_SHA=$(git rev-parse HEAD)
if [ "$CURRENT_SHA" = "$ALLPKG_SHA" ]; then
    python -m pytest tests/ -v --tb=short --net-pypiserver --all-packages
else
    python -m pytest tests/ -v --tb=short --net-pypiserver
fi

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
sed -i 's/if "python" in args and not Path(args.python).is_file():/if "python" in args and args.python is not None and not Path(args.python).is_file():/' src/pipx/main.py 2>/dev/null || true
pip install -e . pytest pytest-cov
ALLPKG_SHA="1e558d81f6e488a0d0598b5808e1aa519330fa03"
CURRENT_SHA=$(git rev-parse HEAD)
if [ "$CURRENT_SHA" = "$ALLPKG_SHA" ]; then
    python -m pytest tests/ -v --tb=short --net-pypiserver --all-packages
else
    python -m pytest tests/ -v --tb=short --net-pypiserver
fi

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
sed -i 's/if "python" in args and not Path(args.python).is_file():/if "python" in args and args.python is not None and not Path(args.python).is_file():/' src/pipx/main.py 2>/dev/null || true
pip install -e . pytest pytest-cov
ALLPKG_SHA="1e558d81f6e488a0d0598b5808e1aa519330fa03"
CURRENT_SHA=$(git rev-parse HEAD)
if [ "$CURRENT_SHA" = "$ALLPKG_SHA" ]; then
    python -m pytest tests/ -v --tb=short --net-pypiserver --all-packages
else
    python -m pytest tests/ -v --tb=short --net-pypiserver
fi

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.9-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/pypa/pipx.git /home/pipx

WORKDIR /home/pipx
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("pypa", "pipx_1721_to_804")
class PIPX_1721_TO_804(Instance):
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

        pattern = r"^(.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAILED|XPASSED)\s+\[\s*\d+%\s*\]$"
        regex = re.compile(pattern)
        for line in log.split("\n"):
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
