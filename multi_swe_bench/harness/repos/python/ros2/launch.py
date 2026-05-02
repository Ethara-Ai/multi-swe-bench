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
        return "ubuntu:22.04"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
        ]

    def dockerfile(self) -> str:
        dockerfile_content = """
# ros2/launch - ROS2 Launch Framework
# Base image: Ubuntu 22.04 with Python3

FROM ubuntu:22.04

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
RUN apt-get update && apt-get install -y git python3 python3-pip python3-venv

WORKDIR /home/

COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

# Install Python dependencies (pin pytest<9 to avoid PytestRemovedIn9Warning becoming an error)
RUN pip3 install lark osrf-pycommon pyyaml "pytest<9" pytest-cov pytest-timeout typing_extensions \\
    "importlib-metadata<5.0" mock flake8 pydocstyle mypy \\
    "ament-copyright @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_copyright" \\
    "ament-index-python @ git+https://github.com/ament/ament_index.git#subdirectory=ament_index_python" \\
    "ament-mypy @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_mypy" \\
    "ament-flake8 @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_flake8" \\
    "ament-pep257 @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_pep257" \\
    "ament-lint @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_lint" \\
    "ament-xmllint @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_xmllint"

# Install launch subpackages in editable mode
RUN pip3 install -e launch/ && \\
    pip3 install -e launch_testing/ && \\
    pip3 install -e launch_xml/ && \\
    pip3 install -e launch_yaml/

# Conditionally install launch_pytest (only exists post-PR#528)
RUN if [ -d launch_pytest ] && [ -f launch_pytest/setup.py ]; then pip3 install -e launch_pytest/; fi

# Set up ament index for ALL subpackages
ENV AMENT_PREFIX_PATH=/usr/local
RUN mkdir -p /usr/local/share/ament_index/resource_index/packages && \\
    touch /usr/local/share/ament_index/resource_index/packages/launch && \\
    touch /usr/local/share/ament_index/resource_index/packages/launch_testing && \\
    touch /usr/local/share/ament_index/resource_index/packages/launch_xml && \\
    touch /usr/local/share/ament_index/resource_index/packages/launch_yaml

# Copy grammar.lark to ament share directory
RUN mkdir -p /usr/local/share/launch/frontend && \\
    if [ -f launch/share/launch/frontend/grammar.lark ]; then \\
        cp launch/share/launch/frontend/grammar.lark /usr/local/share/launch/frontend/grammar.lark; \\
    fi
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("ros2", "launch")
class ROS2_LAUNCH(Instance):

    _PYTEST_FLAGS = (
        "--no-header -rA --tb=no -p no:cacheprovider -v "
        "-p no:launch_testing -p no:launch -p no:launch_pytest "
        "--timeout=60 "
        '-o "addopts=" '
        "-W ignore::pytest.PytestRemovedIn9Warning"
    )

    _PYTEST_LOOP = (
        'RESULT=0; '
        'for pkg in launch launch_testing launch_xml launch_yaml; do '
        'd="$pkg/test/$pkg"; '
        'if [ -d "$d" ]; then '
        'echo "=== Running tests in $d ==="; '
        'python3 -m pytest "$d" {flags} || RESULT=$?; '
        'fi; '
        'done; '
        'exit $RESULT'
    ).format(flags=_PYTEST_FLAGS)

    _PYTHONPATH_PREFIX = 'export PYTHONPATH=/home/{repo}/launch/test:$PYTHONPATH; '

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
        pypath = self._PYTHONPATH_PREFIX.format(repo=self.pr.repo)
        return "bash -c 'cd /home/{repo} ; {pypath}{pytest}'".format(
            repo=self.pr.repo, pypath=pypath, pytest=self._PYTEST_LOOP
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        pypath = self._PYTHONPATH_PREFIX.format(repo=self.pr.repo)
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git apply --whitespace=nowarn /home/test.patch || "
            "git apply --whitespace=nowarn --3way /home/test.patch || true ; "
            "git add -A ; "
            "pip3 install -e launch/ -e launch_testing/ -e launch_xml/ -e launch_yaml/ 2>/dev/null || true ; "
            "if [ -d launch_pytest ] && [ -f launch_pytest/setup.py ]; then pip3 install -e launch_pytest/ 2>/dev/null || true; fi ; "
            "{pypath}{pytest}"
            "'".format(repo=self.pr.repo, pypath=pypath, pytest=self._PYTEST_LOOP)
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        pypath = self._PYTHONPATH_PREFIX.format(repo=self.pr.repo)
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git apply --whitespace=nowarn /home/test.patch || "
            "git apply --whitespace=nowarn --3way /home/test.patch || true ; "
            "git add -A ; "
            "git apply --whitespace=nowarn /home/fix.patch || "
            "git apply --whitespace=nowarn --3way /home/fix.patch || true ; "
            "pip3 install -e launch/ -e launch_testing/ -e launch_xml/ -e launch_yaml/ 2>/dev/null || true ; "
            "if [ -d launch_pytest ] && [ -f launch_pytest/setup.py ]; then pip3 install -e launch_pytest/ 2>/dev/null || true; fi ; "
            "{pypath}{pytest}"
            "'".format(repo=self.pr.repo, pypath=pypath, pytest=self._PYTEST_LOOP)
        )

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", log)

        # Verbose pytest output: "test_name PASSED [xx%]" or "test_name FAILED [xx%]"
        verbose_passed = re.compile(r"^(.*?)\s+PASSED\s+\[\s*\d+%\]$")
        verbose_failed = re.compile(r"^(.*?)\s+FAILED\s+\[\s*\d+%\]$")

        # Summary section (-rA output): "PASSED test_name" or "FAILED test_name"
        summary_passed = re.compile(r"^\s*PASSED\s+(\S+)")
        summary_failed = re.compile(r"^\s*FAILED\s+(\S+)")
        skipped_pattern = re.compile(
            r"^\s*SKIPPED\s+\[\d+\]\s+(\S+)", re.IGNORECASE
        )

        for line in clean_log.splitlines():
            stripped = line.strip()

            match = verbose_passed.match(stripped)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = verbose_failed.match(stripped)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = summary_passed.match(stripped)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = summary_failed.match(stripped)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = skipped_pattern.match(stripped)
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
