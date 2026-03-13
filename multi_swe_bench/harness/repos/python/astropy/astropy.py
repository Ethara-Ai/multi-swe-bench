import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import (
    TestStatus,
    get_modified_files,
    mapping_to_testresult,
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

    def dependency(self) -> str:
        return "python:3.11-slim-bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        return ["gcc", "gfortran", "pkg-config", "libffi-dev"]

    def extra_setup(self) -> str:
        return (
            'RUN pip install --no-cache-dir "setuptools<69" "Cython>=0.29.33" "extension_helpers" "setuptools_scm" "numpy<2" jinja2\n'
            'RUN pip install --no-cache-dir --no-build-isolation -e ".[test]" '
            "|| (pip install --no-cache-dir --no-build-isolation -e . && pip install --no-cache-dir pytest)"
        )

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
""".format(
                    repo=self.pr.repo, test_files=test_files_str
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
pytest --no-header -rA --tb=no -p no:cacheprovider {test_files}
""".format(
                    repo=self.pr.repo, test_files=test_files_str
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pytest --no-header -rA --tb=no -p no:cacheprovider {test_files}
""".format(
                    repo=self.pr.repo, test_files=test_files_str
                ),
            ),
        ]


@Instance.register("astropy", "astropy")
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

    def _test_files_str(self) -> str:
        """Get space-separated .py test files from test_patch (dynamic, not baked-in)."""
        return " ".join(
            f for f in get_modified_files(self.pr.test_patch) if f.endswith(".py")
        )

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        test_files = self._test_files_str()
        return f'bash -c "cd /home/{self.pr.repo} && pytest --no-header -rA --tb=no -p no:cacheprovider {test_files}"'

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        test_files = self._test_files_str()
        return f'bash -c "cd /home/{self.pr.repo} && git apply --whitespace=nowarn /home/test.patch && pytest --no-header -rA --tb=no -p no:cacheprovider {test_files}"'

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        test_files = self._test_files_str()
        return f'bash -c "cd /home/{self.pr.repo} && git apply --whitespace=nowarn /home/test.patch /home/fix.patch && pytest --no-header -rA --tb=no -p no:cacheprovider {test_files}"'

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
            # Support older pytest versions by checking if the line ends with the test status
            elif any([line.endswith(x.value) for x in TestStatus]):
                test_case = line.split()
                if len(test_case) >= 2:
                    test_status_map[test_case[0]] = test_case[1]

        return mapping_to_testresult(test_status_map)
