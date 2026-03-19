"""Lightning-AI/pytorch-lightning harness — pytest with parametrized test normalization."""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestStatus, mapping_to_testresult
from multi_swe_bench.utils.python_test import python_test_command


class PytorchLightningImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        super().__init__(pr, config)

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.8-slim"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    curl \\
    ca-certificates \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class PytorchLightningImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        super().__init__(pr, config)

    def dependency(self) -> Image:
        return PytorchLightningImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = python_test_command(
            self.pr.test_patch,
            base_test_cmd="python -m pytest -rA -v --tb=short --no-header",
        )
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
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

apt-get update && apt-get install -y --no-install-recommends cmake && rm -rf /var/lib/apt/lists/*
pip install torch==1.7.1 -f https://download.pytorch.org/whl/cpu/torch_stable.html || true
pip install six || true
pip install -r requirements.txt || true
pip install -r requirements/test.txt || true
pip install -e . || true
HOROVOD_WITH_PYTORCH=1 HOROVOD_WITHOUT_MXNET=1 HOROVOD_WITHOUT_TENSORFLOW=1 HOROVOD_WITH_GLOO=1 HOROVOD_WITHOUT_MPI=1 pip install horovod==0.28.1 || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
pip install -r requirements.txt || true
pip install -r requirements/test.txt || true
pip install -e . || true
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pip install -r requirements.txt || true
pip install -r requirements/test.txt || true
pip install -e . || true
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
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

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("Lightning-AI", "pytorch-lightning")
class PytorchLightning(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__(pr, config)

    def dependency(self) -> Optional[Image]:
        return PytorchLightningImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        parametrized_pattern = re.compile(r"(.*?)\[(.*)\]")
        test_status_map = {}

        for parts in clean_log.split("\n"):
            parts = parts.strip()
            if not parts:
                continue
            if not any(parts.startswith(x.value) for x in TestStatus):
                continue
            # Collapse " - reason" suffix on FAILED lines before splitting
            if parts.startswith(TestStatus.FAILED.value):
                parts = parts.replace(" - ", " ")
            parts = parts.split()
            if len(parts) < 2:
                continue

            status = parts[0]
            raw_test_id = parts[1]

            match = parametrized_pattern.search(raw_test_id)
            if match:
                test_base, option = match.groups()
                # Normalize single-slash paths: /long/deep/path/file → /file
                is_single_path = (
                    option.startswith("/")
                    and not option.startswith("//")
                    and "*" not in option
                )
                if is_single_path:
                    option = "/" + option.split("/")[-1]
                test_name = f"{test_base}[{option}]"
            else:
                test_name = raw_test_id

            test_status_map[test_name] = status

        return mapping_to_testresult(test_status_map)
