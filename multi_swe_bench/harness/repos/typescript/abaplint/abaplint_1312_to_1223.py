import re
import json
from typing import Optional, Union

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
        return "node:18-bullseye"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
cd /home/abaplint
npm install && cd packages/core/ && npm install && cd ../../ && cd ./packages/cli/ && npm install && cd ../../ && cd ./packages/monaco/ && npm install && cd ../../
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/[[REPO_NAME]]

run_package_tests() {
    local pkg="$1"
    local mocha_args="$2"
    local pkg_dir="/home/[[REPO_NAME]]/packages/${pkg}"
    if [ ! -d "${pkg_dir}" ]; then
        echo "###MOCHA_JSON_START:${pkg}###"
        echo '{"stats":{"tests":0,"passes":0,"failures":0},"tests":[],"passes":[],"failures":[],"missing_dir":true}'
        echo "###MOCHA_JSON_END:${pkg}###"
        return 0
    fi
    echo "###MOCHA_JSON_START:${pkg}###"
    cd "${pkg_dir}"
    if npm run compile 2>&1; then
        npx mocha --reporter json ${mocha_args} 2>&1 || true
    else
        echo '{"stats":{"tests":0,"passes":0,"failures":0},"tests":[],"passes":[],"failures":[],"tsc_error":true}'
    fi
    echo "###MOCHA_JSON_END:${pkg}###"
    cd /home/[[REPO_NAME]]
}

run_package_tests "core" ""
run_package_tests "cli" ""
run_package_tests "monaco" ""

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='package-lock.json' --exclude='**/package-lock.json' /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

run_package_tests() {
    local pkg="$1"
    local mocha_args="$2"
    local pkg_dir="/home/[[REPO_NAME]]/packages/${pkg}"
    if [ ! -d "${pkg_dir}" ]; then
        echo "###MOCHA_JSON_START:${pkg}###"
        echo '{"stats":{"tests":0,"passes":0,"failures":0},"tests":[],"passes":[],"failures":[],"missing_dir":true}'
        echo "###MOCHA_JSON_END:${pkg}###"
        return 0
    fi
    echo "###MOCHA_JSON_START:${pkg}###"
    cd "${pkg_dir}"
    if npm run compile 2>&1; then
        npx mocha --reporter json ${mocha_args} 2>&1 || true
    else
        echo '{"stats":{"tests":0,"passes":0,"failures":0},"tests":[],"passes":[],"failures":[],"tsc_error":true}'
    fi
    echo "###MOCHA_JSON_END:${pkg}###"
    cd /home/[[REPO_NAME]]
}

run_package_tests "core" ""
run_package_tests "cli" ""
run_package_tests "monaco" ""

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='package-lock.json' --exclude='**/package-lock.json' /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

run_package_tests() {
    local pkg="$1"
    local mocha_args="$2"
    local pkg_dir="/home/[[REPO_NAME]]/packages/${pkg}"
    if [ ! -d "${pkg_dir}" ]; then
        echo "###MOCHA_JSON_START:${pkg}###"
        echo '{"stats":{"tests":0,"passes":0,"failures":0},"tests":[],"passes":[],"failures":[],"missing_dir":true}'
        echo "###MOCHA_JSON_END:${pkg}###"
        return 0
    fi
    echo "###MOCHA_JSON_START:${pkg}###"
    cd "${pkg_dir}"
    if npm run compile 2>&1; then
        npx mocha --reporter json ${mocha_args} 2>&1 || true
    else
        echo '{"stats":{"tests":0,"passes":0,"failures":0},"tests":[],"passes":[],"failures":[],"tsc_error":true}'
    fi
    echo "###MOCHA_JSON_END:${pkg}###"
    cd /home/[[REPO_NAME]]
}

run_package_tests "core" ""
run_package_tests "cli" ""
run_package_tests "monaco" ""

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

# Choose an appropriate base image based on the project's requirements - replace [base image] with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM node:18-bullseye

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/abaplint/abaplint.git /home/abaplint

WORKDIR /home/abaplint
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("abaplint", "abaplint_1312_to_1223")
class ABAPLINT_1312_TO_1223(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1B\[[0-9;]*m", "", log)

        packages = ["core", "cli", "monaco"]
        for pkg in packages:
            start_marker = f"###MOCHA_JSON_START:{pkg}###"
            end_marker = f"###MOCHA_JSON_END:{pkg}###"
            start_idx = log.find(start_marker)
            end_idx = log.find(end_marker)

            if start_idx == -1 or end_idx == -1:
                skipped_tests.add(f"{pkg}::tsc_compile")
                continue

            section = log[start_idx + len(start_marker):end_idx].strip()

            json_start = section.rfind("\n{")
            if json_start == -1:
                json_start = 0 if section.startswith("{") else -1

            if json_start == -1:
                skipped_tests.add(f"{pkg}::tsc_compile")
                continue

            json_str = section[json_start:].strip()
            brace_count = 0
            json_end = 0
            for i, ch in enumerate(json_str):
                if ch == "{":
                    brace_count += 1
                elif ch == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break
            if json_end > 0:
                json_str = json_str[:json_end]

            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                skipped_tests.add(f"{pkg}::tsc_compile")
                continue

            if data.get("missing_dir"):
                skipped_tests.add(f"{pkg}::tsc_compile")
                continue
            if data.get("tsc_error"):
                failed_tests.add(f"{pkg}::tsc_compile")
                continue
            if data.get("tsc_only"):
                passed_tests.add(f"{pkg}::tsc_compile")
                continue

            for test in data.get("passes", []):
                title = test.get("fullTitle", "").strip()
                if title:
                    passed_tests.add(f"{pkg}::{title}")

            for test in data.get("failures", []):
                title = test.get("fullTitle", "").strip()
                if title:
                    failed_tests.add(f"{pkg}::{title}")

            for test in data.get("pending", []):
                title = test.get("fullTitle", "").strip()
                if title:
                    skipped_tests.add(f"{pkg}::{title}")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
