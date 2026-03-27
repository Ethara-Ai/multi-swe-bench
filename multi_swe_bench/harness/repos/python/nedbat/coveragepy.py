import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class CoveragepyImageDefault(Image):
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
        return "python:3.10-slim"

    def image_prefix(self) -> str:
        return "mswebench"

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
                (
                    "ls -la\n"
                    "###ACTION_DELIMITER###\n"
                    "apt-get update && apt-get install -y gcc python3-dev\n"
                    "###ACTION_DELIMITER###\n"
                    "pip install -e . 2>/dev/null || "
                    "pip install -e '.[toml]' 2>/dev/null || true\n"
                    "###ACTION_DELIMITER###\n"
                    "pip install pytest pytest-xdist hypothesis flaky 2>/dev/null || true"
                ),
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "cd /home/{repo}\n"
                "python -m pytest --no-header -rA --tb=short -p no:cacheprovider -v tests/\n"
                "\n".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "cd /home/{repo}\n"
                "if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then\n"
                '    echo "Error: git apply failed" >&2\n'
                "    exit 1\n"
                "fi\n"
                "python -m pytest --no-header -rA --tb=short -p no:cacheprovider -v tests/\n"
                "\n".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "cd /home/{repo}\n"
                "if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then\n"
                '    echo "Error: git apply failed" >&2\n'
                "    exit 1\n"
                "fi\n"
                "python -m pytest --no-header -rA --tb=short -p no:cacheprovider -v tests/\n"
                "\n".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git gcc python3-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install -e . 2>/dev/null || pip install -e '.[toml]' 2>/dev/null || true
RUN pip install pytest pytest-xdist hypothesis flaky || true
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("nedbat", "coveragepy")
class Coveragepy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CoveragepyImageDefault(self.pr, self._config)

    _APPLY_OPTS = "--whitespace=nowarn"

    _PIP_FIX = (
        "pip install -e . 2>/dev/null || "
        "pip install -e .[toml] 2>/dev/null || true ; "
        "pip install unittest-mixins mock flaky pytest-xdist hypothesis 2>/dev/null || true ; "
        'pip install "pytest<8" 2>/dev/null || true ; '
    )

    _PYTEST_CMD = (
        "python -m pytest --no-header -rA --tb=short -p no:cacheprovider -v "
        '-o "addopts=" tests/'
    )

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash -c 'cd /home/{repo} ; {pip_fix}{pytest} 2>&1 || true'".format(
            repo=self.pr.repo, pip_fix=self._PIP_FIX, pytest=self._PYTEST_CMD
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . ; "
            "git apply {opts} /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "{pip_fix}"
            "{pytest} 2>&1 || true"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                pip_fix=self._PIP_FIX,
                pytest=self._PYTEST_CMD,
            )
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . ; "
            "git apply {opts} /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "git apply {opts} /home/fix.patch || "
            "git apply {opts} --3way /home/fix.patch || true ; "
            "{pip_fix}"
            "{pytest} 2>&1 || true"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                pip_fix=self._PIP_FIX,
                pytest=self._PYTEST_CMD,
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
            r"^SKIPPED\s+(?:\[\d+\]\s+)?(tests/.*?(?:::\S+|\.py:\d+))\s*",
            re.MULTILINE,
        )

        for line in lines:
            line = line.strip()
            for pattern in passed_patterns:
                m = pattern.match(line)
                if m:
                    passed_tests.add(m.group(1).strip())
                    break
            for pattern in failed_patterns:
                m = pattern.match(line)
                if m:
                    failed_tests.add(m.group(1).strip())
                    break
            m = skipped_pattern.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
