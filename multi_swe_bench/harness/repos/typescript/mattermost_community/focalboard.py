from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FocalboardImageBase(Image):
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
        return "node:20"

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

RUN apt-get update && apt-get install -y wget && \\
    GOARCH=$(dpkg --print-architecture) && \\
    wget -q https://go.dev/dl/go1.21.5.linux-${{GOARCH}}.tar.gz && \\
    tar -C /usr/local -xzf go1.21.5.linux-${{GOARCH}}.tar.gz && \\
    rm go1.21.5.linux-${{GOARCH}}.tar.gz

ENV PATH="/usr/local/go/bin:$PATH"
ENV GOPATH="/root/go"

WORKDIR /home/

{code}

{self.clear_env}

"""


class FocalboardImageDefault(Image):
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
        return FocalboardImageBase(self.pr, self.config)

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
bash /home/check_git_changes.sh

# Detect test type from test.patch
HAS_GO=false
HAS_TS=false

if grep -q '^diff --git a/server/' /home/test.patch 2>/dev/null; then
    HAS_GO=true
fi
if grep -q '^diff --git a/webapp/' /home/test.patch 2>/dev/null; then
    HAS_TS=true
fi

# Install Go dependencies if needed
if [ "$HAS_GO" = true ]; then
    cd /home/{pr.repo}/server
    export PATH="/usr/local/go/bin:$PATH"
    go mod download || true
    cd /home/{pr.repo}
fi

# Install TS dependencies if needed
if [ "$HAS_TS" = true ]; then
    cd /home/{pr.repo}/webapp
    npm install 2>/dev/null || npm install --ignore-scripts 2>/dev/null || true
    cd /home/{pr.repo}
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

export PATH="/usr/local/go/bin:$PATH"

# Detect test type from test.patch
HAS_GO=false
HAS_TS=false

if grep -q '^diff --git a/server/' /home/test.patch 2>/dev/null; then
    HAS_GO=true
fi
if grep -q '^diff --git a/webapp/' /home/test.patch 2>/dev/null; then
    HAS_TS=true
fi

if [ "$HAS_GO" = true ]; then
    cd /home/{pr.repo}/server
    go test -v -count=1 ./...
    cd /home/{pr.repo}
fi

if [ "$HAS_TS" = true ]; then
    cd /home/{pr.repo}/webapp
    ./node_modules/.bin/jest --forceExit
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
git apply /home/test.patch

export PATH="/usr/local/go/bin:$PATH"

# Detect test type from test.patch
HAS_GO=false
HAS_TS=false

if grep -q '^diff --git a/server/' /home/test.patch 2>/dev/null; then
    HAS_GO=true
fi
if grep -q '^diff --git a/webapp/' /home/test.patch 2>/dev/null; then
    HAS_TS=true
fi

if [ "$HAS_GO" = true ]; then
    cd /home/{pr.repo}/server
    go test -v -count=1 ./...
    cd /home/{pr.repo}
fi

if [ "$HAS_TS" = true ]; then
    cd /home/{pr.repo}/webapp
    ./node_modules/.bin/jest --forceExit
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
git apply /home/test.patch /home/fix.patch

export PATH="/usr/local/go/bin:$PATH"

# Detect test type from test.patch
HAS_GO=false
HAS_TS=false

if grep -q '^diff --git a/server/' /home/test.patch 2>/dev/null; then
    HAS_GO=true
fi
if grep -q '^diff --git a/webapp/' /home/test.patch 2>/dev/null; then
    HAS_TS=true
fi

if [ "$HAS_GO" = true ]; then
    cd /home/{pr.repo}/server
    go test -v -count=1 ./...
    cd /home/{pr.repo}
fi

if [ "$HAS_TS" = true ]; then
    cd /home/{pr.repo}/webapp
    ./node_modules/.bin/jest --forceExit
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


@Instance.register("mattermost-community", "focalboard")
class Focalboard(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FocalboardImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_go_pass = re.compile(r"--- PASS: (\S+)")
        re_go_fail = re.compile(r"--- FAIL: (\S+)")
        re_go_skip = re.compile(r"--- SKIP: (\S+)")

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        current_suite = ""
        current_suite_status = ""
        has_checkmark_tests = False
        passed_suites = set()
        failed_suites = set()

        for line in test_log.splitlines():
            stripped = line.strip()

            # Go: --- PASS: TestName
            go_pass_match = re_go_pass.match(stripped)
            if go_pass_match:
                test_name = go_pass_match.group(1)
                if test_name not in failed_tests:
                    base = get_base_name(test_name)
                    if base in skipped_tests:
                        skipped_tests.remove(base)
                    passed_tests.add(base)
                continue

            # Go: --- FAIL: TestName
            go_fail_match = re_go_fail.match(stripped)
            if go_fail_match:
                test_name = go_fail_match.group(1)
                base = get_base_name(test_name)
                if base in passed_tests:
                    passed_tests.remove(base)
                if base in skipped_tests:
                    skipped_tests.remove(base)
                failed_tests.add(base)
                continue

            # Go: --- SKIP: TestName
            go_skip_match = re_go_skip.match(stripped)
            if go_skip_match:
                test_name = go_skip_match.group(1)
                base = get_base_name(test_name)
                if base not in passed_tests and base not in failed_tests:
                    skipped_tests.add(base)
                continue

            # Jest: PASS/FAIL suite line
            suite_match = re.match(r"^(PASS|FAIL)\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$", stripped)
            if suite_match:
                current_suite_status = suite_match.group(1)
                current_suite = suite_match.group(2)
                if current_suite_status == "PASS":
                    passed_suites.add(current_suite)
                else:
                    failed_suites.add(current_suite)
                continue

            # Jest: checkmark passed
            pass_match = re.match(
                r"^[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped
            )
            if pass_match:
                has_checkmark_tests = True
                test_name = (
                    f"{current_suite} > {pass_match.group(1)}"
                    if current_suite
                    else pass_match.group(1)
                )
                passed_tests.add(test_name)
                continue

            # Jest: checkmark failed
            fail_match = re.match(
                r"^[✕✗✘×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped
            )
            if fail_match:
                has_checkmark_tests = True
                test_name = (
                    f"{current_suite} > {fail_match.group(1)}"
                    if current_suite
                    else fail_match.group(1)
                )
                failed_tests.add(test_name)
                continue

            # Jest: skipped
            skip_match = re.match(r"^[○⊘]\s+(.+)", stripped)
            if skip_match:
                has_checkmark_tests = True
                test_name = (
                    f"{current_suite} > {skip_match.group(1)}"
                    if current_suite
                    else skip_match.group(1)
                )
                skipped_tests.add(test_name)
                continue

            # Jest: bullet failure detail
            bullet_match = re.match(r"^●\s+(.+?)\s+›\s+(.+)", stripped)
            if bullet_match and current_suite_status == "FAIL":
                test_name = f"{current_suite} > {bullet_match.group(1)} › {bullet_match.group(2)}"
                failed_tests.add(test_name)
                continue

        if not has_checkmark_tests:
            for suite in passed_suites:
                passed_tests.add(suite)
            if not failed_tests:
                for suite in failed_suites:
                    failed_tests.add(suite)

        # Ensure no overlap: failed takes precedence over passed
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
