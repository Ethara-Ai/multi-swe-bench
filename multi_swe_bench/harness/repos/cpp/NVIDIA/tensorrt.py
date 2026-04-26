import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TensorRTImageBase(Image):

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
        return "ubuntu:22.04"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    build-essential cmake git \\
    python3 python3-pip python3-dev \\
    wget curl && \\
    rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python && \\
    pip3 install pytest numpy onnx onnxruntime protobuf

{code}

RUN cd /home/{self.pr.repo} && git submodule update --init --recursive

{self.clear_env}

"""


class TensorRTImageDefault(Image):
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
        return TensorRTImageBase(self.pr, self._config)

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

if [[ -n $(git status --porcelain --ignore-submodules) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
git submodule update --init --recursive
git submodule foreach --recursive git clean -fdx
git clean -fdx
git checkout -- .

if [ -d "/home/{pr.repo}/tools/onnx-graphsurgeon" ]; then
    cd /home/{pr.repo}/tools/onnx-graphsurgeon
    pip3 install -e . 2>/dev/null || true
fi
if [ -d "/home/{pr.repo}/tools/Polygraphy" ]; then
    cd /home/{pr.repo}/tools/Polygraphy
    pip3 install -e . 2>/dev/null || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Run onnx-graphsurgeon tests
if [ -d "tools/onnx-graphsurgeon/tests" ]; then
    cd tools/onnx-graphsurgeon
    python3 -m pytest tests/ -v --continue-on-collection-errors || true
    cd /home/{pr.repo}
fi

# Run Polygraphy tests (subset that doesn't require tensorrt)
if [ -d "tools/Polygraphy/tests" ]; then
    cd tools/Polygraphy
    python3 -m pytest tests/ -v --continue-on-collection-errors --ignore=tests/backend --ignore=tests/comparator || true
    cd /home/{pr.repo}
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
find . -name '*.rej' -delete 2>/dev/null || true
git submodule update --init --recursive 2>/dev/null || true

if [ -d "/home/{pr.repo}/tools/onnx-graphsurgeon" ]; then
    cd /home/{pr.repo}/tools/onnx-graphsurgeon
    pip3 install -e . 2>/dev/null || true
fi
if [ -d "/home/{pr.repo}/tools/Polygraphy" ]; then
    cd /home/{pr.repo}/tools/Polygraphy
    pip3 install -e . 2>/dev/null || true
fi
cd /home/{pr.repo}

# Run onnx-graphsurgeon tests
if [ -d "tools/onnx-graphsurgeon/tests" ]; then
    cd tools/onnx-graphsurgeon
    python3 -m pytest tests/ -v --continue-on-collection-errors || true
    cd /home/{pr.repo}
fi

# Run Polygraphy tests (subset that doesn't require tensorrt)
if [ -d "tools/Polygraphy/tests" ]; then
    cd tools/Polygraphy
    python3 -m pytest tests/ -v --continue-on-collection-errors --ignore=tests/backend --ignore=tests/comparator || true
    cd /home/{pr.repo}
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
find . -name '*.rej' -delete 2>/dev/null || true
git submodule update --init --recursive 2>/dev/null || true

if [ -d "/home/{pr.repo}/tools/onnx-graphsurgeon" ]; then
    cd /home/{pr.repo}/tools/onnx-graphsurgeon
    pip3 install -e . 2>/dev/null || true
fi
if [ -d "/home/{pr.repo}/tools/Polygraphy" ]; then
    cd /home/{pr.repo}/tools/Polygraphy
    pip3 install -e . 2>/dev/null || true
fi
cd /home/{pr.repo}

# Run onnx-graphsurgeon tests
if [ -d "tools/onnx-graphsurgeon/tests" ]; then
    cd tools/onnx-graphsurgeon
    python3 -m pytest tests/ -v --continue-on-collection-errors || true
    cd /home/{pr.repo}
fi

# Run Polygraphy tests (subset that doesn't require tensorrt)
if [ -d "tools/Polygraphy/tests" ]; then
    cd tools/Polygraphy
    python3 -m pytest tests/ -v --continue-on-collection-errors --ignore=tests/backend --ignore=tests/comparator || true
    cd /home/{pr.repo}
fi

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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("NVIDIA", "TensorRT")
class TensorRT(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return TensorRTImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_escape.sub("", test_log)

        pattern = re.compile(
            r"^\s*(?:([^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)|(PASSED|FAILED|SKIPPED|ERROR)\s+([^\s]+))(?:\s+\[.*?\])?\s*$",
            re.MULTILINE,
        )
        for match in pattern.finditer(test_log):
            test_name = (match.group(1) or match.group(4)).strip()
            status = match.group(2) or match.group(3)
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Capture file-level collection ERRORs
        file_error_pattern = re.compile(
            r"^ERROR\s+(tests/\S+\.py)\s*$", re.MULTILINE
        )
        for match in file_error_pattern.finditer(test_log):
            failed_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
