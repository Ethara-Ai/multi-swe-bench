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
        return "golang:1.11"

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
                "prepare.sh",
                """ls -F
###ACTION_DELIMITER###
curl https://glide.sh/get | sh
###ACTION_DELIMITER###
glide install
###ACTION_DELIMITER###
go test -v ./...
###ACTION_DELIMITER###
echo "go test -v ./..." > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export GOPATH=/go
cd $GOPATH/src/github.com/{pr.org}/{pr.repo}
go test -v ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export GOPATH=/go
cd $GOPATH/src/github.com/{pr.org}/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
go test -v ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export GOPATH=/go
cd $GOPATH/src/github.com/{pr.org}/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
go test -v ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = f"""\
# Jaeger Glide era (PRs 236-1068)
# GOPATH mode with Glide dependency management
FROM golang:1.11

# Install basic requirements
RUN apt-get update && apt-get install -y --no-install-recommends git curl \\
    && rm -rf /var/lib/apt/lists/*

# Install Glide
RUN curl https://glide.sh/get | sh

WORKDIR /home/

# Clone into GOPATH
RUN mkdir -p /go/src/github.com/jaegertracing
RUN git clone https://github.com/jaegertracing/jaeger.git /go/src/github.com/jaegertracing/jaeger

WORKDIR /go/src/github.com/jaegertracing/jaeger
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
CMD ["bash"]
"""
        return dockerfile_content


@Instance.register("jaegertracing", "jaeger_1068_to_236")
class JAEGER_1068_TO_236(Instance):
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
        # Strip ANSI escape codes
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        run_tests = set(re.findall(r"=== RUN\s+([\S]+)", log))
        passed_tests = set(re.findall(r"--- PASS: ([\S]+)", log))
        skipped_individual = set(re.findall(r"--- SKIP: ([\S]+)", log))
        skipped_packages = set(re.findall(r"\?   \t([^\t]+?)\[no test files\]", log))

        failed_tests = run_tests - passed_tests - skipped_individual
        skipped_tests = skipped_packages | skipped_individual
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
