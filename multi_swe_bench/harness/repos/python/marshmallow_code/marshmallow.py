import re
from typing import Union

from unidiff import PatchSet

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_DIR = "tests/"


def _test_files_from_patch(patch: str) -> list[str]:
    """Extract pytest-collectable test files from a unified diff.

    Excludes:
    - conftest.py / __init__.py / helper.py / base.py  (test infrastructure)
    - tests/mypy_test_cases/  (mypy type-checking stubs, not pytest tests)
    """
    _SKIP_BASENAMES = frozenset({"conftest.py", "__init__.py", "helper.py", "base.py"})
    _SKIP_DIRS = ("tests/mypy_test_cases/",)

    seen: set[str] = set()
    for patched_file in PatchSet(patch):
        path = patched_file.target_file
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if path == "/dev/null":
            continue
        if path.endswith(".py") and path.startswith(_TEST_DIR):
            if any(path.startswith(d) for d in _SKIP_DIRS):
                continue
            basename = path.rsplit("/", 1)[-1]
            if basename not in _SKIP_BASENAMES:
                seen.add(path)
    return sorted(seen)


def _python_image_for_pr(pr_number: int) -> str:
    """Select Python Docker image based on PR number.

    PRs on the 2.x-line branch (numbers <= 1398) may touch code written
    for older Python.  Using 3.8 keeps compatibility.  Everything newer
    runs comfortably on 3.11.
    """
    if pr_number <= 1398:
        return "python:3.8-slim"
    return "python:3.11-slim"


def _base_tag_for_python(python_image: str) -> str:
    version = python_image.split(":")[-1].split("-")[0]
    return f"base-py{version}"


class ImageBase(Image):

    def __init__(self, pr: PullRequest, config: Config, python_image: str):
        self._pr = pr
        self._config = config
        self._python_image = python_image

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return self._python_image

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return _base_tag_for_python(self._python_image)

    def workdir(self) -> str:
        return _base_tag_for_python(self._python_image)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
RUN apt-get update && apt-get install -y git

WORKDIR /home/

{code}

{self.clear_env}

"""


class ImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config, python_image: str = "python:3.11-slim"):
        self._pr = pr
        self._config = config
        self._python_image = python_image

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return ImageBase(self.pr, self.config, self._python_image)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = _test_files_from_patch(self.pr.test_patch)
        test_files_str = " ".join(test_files)

        if test_files_str:
            pytest_cmd = f"python -m pytest --no-header -rN --tb=short -v {test_files_str}"
        else:
            pytest_cmd = "python -m pytest --no-header -rN --tb=short -v"

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
                '  echo "check_git_changes: Not inside a git repository"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                'if [[ -n $(git status --porcelain) ]]; then\n'
                '  echo "check_git_changes: Uncommitted changes"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                'echo "check_git_changes: No uncommitted changes"\n'
                "exit 0\n",
            ),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "cd /home/{repo}\n"
                "git reset --hard\n"
                "bash /home/check_git_changes.sh\n"
                "git checkout {sha}\n"
                "bash /home/check_git_changes.sh\n"
                "\n"
                "( "
                'pip install --no-cache-dir -e ".[tests,reco]" || '
                'pip install --no-cache-dir -e ".[tests]" || '
                "pip install --no-cache-dir -e . || "
                "true"
                " ) && pip install --no-cache-dir pytest simplejson pytz python-dateutil\n".format(
                    repo=self.pr.repo, sha=self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "export CI=true\n"
                "cd /home/{repo}\n"
                "{pytest_cmd}\n".format(
                    repo=self.pr.repo, pytest_cmd=pytest_cmd
                ),
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "export CI=true\n"
                "cd /home/{repo}\n"
                "git apply --whitespace=nowarn /home/test.patch\n"
                "{pytest_cmd}\n".format(
                    repo=self.pr.repo, pytest_cmd=pytest_cmd
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "export CI=true\n"
                "cd /home/{repo}\n"
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                "{pytest_cmd}\n".format(
                    repo=self.pr.repo, pytest_cmd=pytest_cmd
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        assert isinstance(base, Image)
        base_name = base.image_name()
        base_tag = base.image_tag()

        copy_commands = "".join(
            f"COPY {f.name} /home/\n" for f in self.files()
        )

        return f"""FROM {base_name}:{base_tag}



{copy_commands}

RUN bash /home/prepare.sh

"""


@Instance.register("marshmallow-code", "marshmallow")
class Marshmallow(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        python_image = _python_image_for_pr(self.pr.number)
        return ImageDefault(self.pr, self._config, python_image=python_image)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape.sub("", test_log)

        pytest_pattern = r"([^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)\s+\["
        for test_name, status in re.findall(pytest_pattern, test_log):
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
