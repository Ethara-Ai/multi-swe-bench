import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestStatus, mapping_to_testresult


class ImageBase(Image):
    """Base image for sympy: python:3.9 + system deps + repo clone."""

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
        return "python:3.9-slim"

    def image_tag(self) -> str:
        # Include base commit in tag so each PR gets its own base image
        base_sha = self.pr.base.sha[:8] if hasattr(self.pr.base, "sha") else "base"
        return f"base-{base_sha}"

    def workdir(self) -> str:
        # Include base commit in workdir so each PR gets its own build dir
        base_sha = self.pr.base.sha[:8] if hasattr(self.pr.base, "sha") else "base"
        return f"base-{base_sha}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make sudo wget && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN pip install --no-cache-dir mpmath==1.3.0 flake8 flake8-comprehensions
RUN pip install --no-cache-dir -e .

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """PR-specific image: base + patches + test/fix scripts."""

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
        from multi_swe_bench.harness.test_result import get_modified_files

        test_files = get_modified_files(self.pr.test_patch)
        test_files_str = " ".join(test_files)

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
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh
pip install --no-cache-dir -e .
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git config --global --add safe.directory /home/{repo}
PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' python -m pytest {test_files} -v 2>&1 || \\
PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose {test_files} 2>&1
""".format(
                    repo=self.pr.repo,
                    test_files=test_files_str,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git config --global --add safe.directory /home/{repo}
git apply --whitespace=nowarn /home/test.patch
PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' python -m pytest {test_files} -v 2>&1 || \\
PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose {test_files} 2>&1
""".format(
                    repo=self.pr.repo,
                    test_files=test_files_str,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git config --global --add safe.directory /home/{repo}
git apply --whitespace=nowarn /home/fix.patch
: '>>>>> Start Fix Verification'
git status
git diff HEAD
: '>>>>> End Fix Verification'
git apply --whitespace=nowarn /home/test.patch
: '>>>>> Start Test Output'
PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' python -m pytest {test_files} -v 2>&1 || \\
PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose {test_files} 2>&1
: '>>>>> End Test Output'
git checkout {base_sha} -- {test_files}
""".format(
                    repo=self.pr.repo,
                    test_files=test_files_str,
                    base_sha=self.pr.base.sha,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base_img = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base_img}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make sudo wget && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN pip install --no-cache-dir mpmath==1.3.0 flake8 flake8-comprehensions
RUN pip install --no-cache-dir -e .

WORKDIR /home

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("sympy", "sympy")
class Sympy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

    _PYTEST_STUB = "pip install pytest -q 2>/dev/null"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return f"bash -c '{self._PYTEST_STUB}; bash /home/run.sh'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return f"bash -c '{self._PYTEST_STUB}; bash /home/test-run.sh'"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return f"bash -c '{self._PYTEST_STUB}; bash /home/fix-run.sh'"

    def parse_log(self, test_log: str) -> TestResult:
        test_status_map = {}
        pattern = r"(_*) (.*)\.py:(.*) (_*)"
        matches = re.findall(pattern, test_log)
        for match in matches:
            test_case = f"{match[1]}.py:{match[2]}"
            test_status_map[test_case] = TestStatus.FAILED.value
        for line in test_log.split("\n"):
            line = line.strip()
            if line.startswith("test_"):
                if line.endswith(" E"):
                    test = line.split()[0]
                    test_status_map[test] = TestStatus.ERROR.value
                if line.endswith(" F"):
                    test = line.split()[0]
                    test_status_map[test] = TestStatus.FAILED.value
                if line.endswith(" ok"):
                    test = line.split()[0]
                    test_status_map[test] = TestStatus.PASSED.value

        return mapping_to_testresult(test_status_map)
