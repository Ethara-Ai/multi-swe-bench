import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Repo-level base image: OS deps + cloned/checked-out source + baked env.
    Built once as `<repo>:base`; PR images layer only patches + scripts on top."""

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

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Clone via "${{REPO_URL}}" (the pipeline enhancer leaves this form untouched and
        # injects its git-hardening block just before our trailing CMD); env installs sit
        # after checkout so aesara's C extensions build against the checked-out source.
        return f"""FROM {image_name}
ENV DEBIAN_FRONTEND=noninteractive
# numpy.distutils imports distutils.msvccompiler, removed by newer setuptools' vendored
# distutils; force the stdlib distutils so aesara's BLAS detection / C compile works.
ENV SETUPTOOLS_USE_DISTUTILS=stdlib
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
# Base image `git clone` can land an incomplete packfile; fetch the base commit by URL if missing.
RUN git cat-file -e ${{BASE_COMMIT}} 2>/dev/null || git fetch --no-tags "${{REPO_URL}}" ${{BASE_COMMIT}}
RUN git checkout ${{BASE_COMMIT}}

# --- Environment baked in so human_mode=True works.
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir pytest-html
RUN pip install --no-cache-dir numpy==1.23.5
RUN pip install --no-cache-dir numba==0.56.0 llvmlite==0.39.0
RUN pip install --no-cache-dir pytest-xdist pytest-timeout

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """PR-specific image: FROM the repo base, add only patches + run scripts."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

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
# Re-assert the base commit at PR-build time. Non-destructive on purpose: no `git reset`
# (some bases carry intentional working-tree edits from their env setup) and no test run
# (tests execute at instance time via run/test/fix-run.sh).
cd /home/{pr.repo}
git checkout {pr.base.sha}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest -v -rA --continue-on-collection-errors -n auto tests/link/test_jax.py tests/link/test_numba.py tests/scan/test_printing.py tests/tensor/nnet/test_batchnorm.py tests/tensor/test_basic.py tests/tensor/test_basic_opt.py tests/tensor/test_math.py tests/tensor/test_opt_uncanonicalize.py tests/tensor/test_shape.py tests/tensor/test_subtensor.py tests/tensor/test_subtensor_opt.py tests/tensor/test_type.py tests/test_rop.py

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
pytest -v -rA --continue-on-collection-errors -n auto tests/link/test_jax.py tests/link/test_numba.py tests/scan/test_printing.py tests/tensor/nnet/test_batchnorm.py tests/tensor/test_basic.py tests/tensor/test_basic_opt.py tests/tensor/test_math.py tests/tensor/test_opt_uncanonicalize.py tests/tensor/test_shape.py tests/tensor/test_subtensor.py tests/tensor/test_subtensor_opt.py tests/tensor/test_type.py tests/test_rop.py

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest -v -rA --continue-on-collection-errors -n auto tests/link/test_jax.py tests/link/test_numba.py tests/scan/test_printing.py tests/tensor/nnet/test_batchnorm.py tests/tensor/test_basic.py tests/tensor/test_basic_opt.py tests/tensor/test_math.py tests/tensor/test_opt_uncanonicalize.py tests/tensor/test_shape.py tests/tensor/test_subtensor.py tests/tensor/test_subtensor_opt.py tests/tensor/test_type.py tests/test_rop.py

""".format(pr=self.pr),
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

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("aesara-devs", "aesara_1073_to_741")
class AESARA_1073_TO_741(Instance):
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
        import re

        # Parse log content by lines to capture test statuses and names
        pattern = (
            r"(?:\[\w+\]\s+\[\s*\d+%\]\s+)?(PASSED|FAILED|SKIPPED)\s+(.+?)(?:\s+-|$)"
        )
        for line in log.split("\n"):
            match = re.search(pattern, line)
            if match:
                status = match.group(1)
                test_name = match.group(2).strip()
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status == "FAILED":
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
