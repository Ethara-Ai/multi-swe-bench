import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import (
    TestStatus,
    get_modified_files,
    mapping_to_testresult,
)


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
        return "python:3.11-slim-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        if self._config.need_clone:
            code = f'RUN git clone "https://github.com/{org}/{repo}.git" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM python:3.11-slim-bookworm

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget \\
    gcc gfortran pkg-config libffi-dev \\
    && rm -rf /var/lib/apt/lists/*

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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = [
            f
            for f in get_modified_files(self.pr.test_patch)
            if f.endswith(".py")
        ]
        test_files_str = " ".join(test_files)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
pytest --no-header -rA --tb=no -p no:cacheprovider {test_files}
""".format(repo=self.pr.repo, test_files=test_files_str),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
pytest --no-header -rA --tb=no -p no:cacheprovider {test_files}
""".format(repo=self.pr.repo, test_files=test_files_str),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/fix.patch
pytest --no-header -rA --tb=no -p no:cacheprovider {test_files}
""".format(repo=self.pr.repo, test_files=test_files_str),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())

        return f"""FROM {base.image_name()}:{base.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard && git checkout ${{BASE_COMMIT}}

RUN pip install --no-cache-dir "setuptools<69" "Cython>=0.29.33" "extension_helpers" "setuptools_scm" "numpy<2" jinja2
RUN pip install --no-cache-dir --no-build-isolation -e ".[test]" || (pip install --no-cache-dir --no-build-isolation -e . && pip install --no-cache-dir pytest)

{copy_commands}
RUN chmod +x /home/*.sh

{Image._HARDENING_BLOCK}
CMD ["/bin/bash"]
"""


@Instance.register("astropy", "15288-16386")
class Astropy(Instance):
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
        test_status_map = {}
        escapes = "".join([chr(char) for char in range(1, 32)])
        for line in test_log.split("\n"):
            line = re.sub(r"\[(\d+)m", "", line)
            translator = str.maketrans("", "", escapes)
            line = line.translate(translator)
            if any([line.startswith(x.value) for x in TestStatus]):
                if line.startswith(TestStatus.FAILED.value):
                    line = line.replace(" - ", " ")
                test_case = line.split()
                if len(test_case) >= 2:
                    test_status_map[test_case[1]] = test_case[0]
            elif any([line.endswith(x.value) for x in TestStatus]):
                test_case = line.split()
                if len(test_case) >= 2:
                    test_status_map[test_case[0]] = test_case[1]

        return mapping_to_testresult(test_status_map)
