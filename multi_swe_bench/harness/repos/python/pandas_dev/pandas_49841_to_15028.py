import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PandasLegacyImageDefault(Image):
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
        return "python:3.8-slim"

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
                """ls -la
###ACTION_DELIMITER###
apt-get update && apt-get install -y build-essential gfortran libopenblas-dev liblapack-dev libbz2-dev liblzma-dev libzstd-dev libssl-dev pkg-config
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
cd /home/{pr.repo} && git reset --hard && git checkout {pr.base.sha}
###ACTION_DELIMITER###
pip install cython numpy 2>/dev/null || true
###ACTION_DELIMITER###
cd /home/{pr.repo} && python setup.py develop 2>/dev/null || pip install -e . 2>/dev/null || pip install -e . --no-build-isolation 2>/dev/null || true
###ACTION_DELIMITER###
pip install pytest pytest-xdist hypothesis 2>/dev/null || true""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --no-header -rA --tb=no -p no:cacheprovider -v pandas/tests/ 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || true
python setup.py develop 2>/dev/null || pip install -e . 2>/dev/null || true
pytest --no-header -rA --tb=no -p no:cacheprovider -v pandas/tests/ 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --3way /home/fix.patch || true
python setup.py develop 2>/dev/null || pip install -e . 2>/dev/null || true
pytest --no-header -rA --tb=no -p no:cacheprovider -v pandas/tests/ 2>&1 || true
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """
FROM python:3.8-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \\
    git \\
    build-essential \\
    gfortran \\
    libopenblas-dev \\
    liblapack-dev \\
    libbz2-dev \\
    liblzma-dev \\
    libzstd-dev \\
    libssl-dev \\
    pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel

RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install cython numpy 2>/dev/null || true
RUN cd /home/{pr.repo} && python setup.py develop 2>/dev/null || pip install -e . 2>/dev/null || pip install -e . --no-build-isolation 2>/dev/null || true
RUN pip install pytest pytest-xdist hypothesis 2>/dev/null || true

{copy_commands}
""".format(pr=self.pr, copy_commands=copy_commands)


@Instance.register("pandas-dev", "pandas_49841_to_15028")
class PandasLegacy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PandasLegacyImageDefault(self.pr, self._config)

    _PIP_FIX = 'pip install "setuptools<60" "cython<3" "numpy<1.24" pytz python-dateutil pytest-asyncio pyarrow moto 2>/dev/null || true ; '

    _BUILD_CMD = (
        'python -c "import pandas" 2>/dev/null || '
        '( find pandas/_libs -name "*.c" -exec grep -l "Do not use this file" {} \\; | xargs rm -f 2>/dev/null ; '
        'CFLAGS="-DNUMPY_IMPORT_ARRAY_RETVAL=" '
        "python setup.py build_ext --inplace --force 2>&1 && "
        "python setup.py develop --no-deps 2>&1 ) || true"
    )

    _PYTEST_CMD = (
        'TEST_FILES=$(sed -n "s/^diff --git a\\/\\([^ ]*\\).*/\\1/p" /home/test.patch | grep "^pandas/tests/.*\\.py$" | sort -u | tr "\\n" " ") ; '
        "pytest --no-header -rA --tb=short -p no:cacheprovider -v $TEST_FILES"
    )

    _APPLY_OPTS = "--whitespace=nowarn"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash -c 'cd /home/{repo} ; {pip}{build} ; {pytest}'".format(
            repo=self.pr.repo,
            pip=self._PIP_FIX,
            build=self._BUILD_CMD,
            pytest=self._PYTEST_CMD,
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "{pip}"
            "git apply {opts} --reject /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "{build} ; "
            "{pytest}"
            "'"
        ).format(
            repo=self.pr.repo,
            pip=self._PIP_FIX,
            opts=self._APPLY_OPTS,
            build=self._BUILD_CMD,
            pytest=self._PYTEST_CMD,
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "{pip}"
            "git apply {opts} --reject /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "git apply {opts} --reject /home/fix.patch || "
            "git apply {opts} --3way /home/fix.patch || true ; "
            "{build} ; "
            "{pytest}"
            "'"
        ).format(
            repo=self.pr.repo,
            pip=self._PIP_FIX,
            opts=self._APPLY_OPTS,
            build=self._BUILD_CMD,
            pytest=self._PYTEST_CMD,
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
            re.compile(r"^(.*?)\s+PASSED$"),
        ]
        failed_patterns = [
            re.compile(r"^(.*?)\s+FAILED\s+\[\s*\d+%\]$"),
            re.compile(r"^FAILED\s+(.*?)(?: - .*)?$"),
            re.compile(r"^(.*?)\s+FAILED$"),
        ]
        skipped_pattern = re.compile(
            r"^SKIPPED\s+(?:\[\d+\]\s+)?(pandas/.*?(?:::\S+|\.py:\d+))\s*",
            re.MULTILINE,
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
