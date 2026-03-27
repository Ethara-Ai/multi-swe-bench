import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GosecVersionBase(Image):
    """Version-bucketed base image: FROM golang:{version} + clone repo.

    Shared across all PRs in the same Go version bucket.
    image_tag = "base-{interval_name}" (e.g., "base-gosec_460_to_435").
    """

    def __init__(self, pr: PullRequest, config: Config, go_version: str, interval_name: str = ""):
        self._pr = pr
        self._config = config
        self._go_version = go_version
        self._interval_name = interval_name or f"go{go_version}"

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    @property
    def go_version(self) -> str:
        return self._go_version

    def dependency(self) -> Union[str, "Image"]:
        return f"golang:{self._go_version}"

    def image_tag(self) -> str:
        return f"base-{self._interval_name}"

    def workdir(self) -> str:
        return f"base-{self._interval_name}"

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

        # Debian archive fix for old Go images (buster/stretch are EOL)
        debian_fix = ""
        if self._go_version in ("1.13", "1.14", "1.16", "1.17", "1.18", "1.19", "1.20"):
            debian_fix = """
RUN if grep -q 'buster\\|stretch' /etc/apt/sources.list 2>/dev/null; then \\
        sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\
        sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\
        sed -i '/stretch-updates/d' /etc/apt/sources.list && \\
        sed -i '/buster-updates/d' /etc/apt/sources.list && \\
        echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid; \\
    fi
"""

        return f"""FROM {image_name}

ENV DEBIAN_FRONTEND=noninteractive
{debian_fix}
RUN apt-get update && apt-get install -y git

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class GosecImageDefault(Image):
    """Per-PR image: FROM base → checkout + patches + prepare."""

    def __init__(self, pr: PullRequest, config: Config, go_version: str = "latest"):
        self._pr = pr
        self._config = config
        self._go_version = go_version

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return GosecVersionBase(self.pr, self.config, self._go_version)

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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

go mod tidy 2>&1 || echo "go mod tidy failed (non-fatal)"
go mod download 2>&1 || echo "go mod download failed (non-fatal)"

go test -v -count=1 -timeout 15m ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go test -v -count=1 -timeout 15m ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go test -v -count=1 -timeout 15m ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go test -v -count=1 -timeout 15m ./...

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


def gosec_parse_log(test_log: str) -> TestResult:
    """Shared parse_log for all gosec instances."""
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    re_pass_tests = [
        re.compile(r"--- PASS: (\S+)"),
        re.compile(r"ok\s+(\S+)"),
    ]
    re_fail_tests = [
        re.compile(r"--- FAIL: (\S+)"),
        re.compile(r"FAIL\s+(\S+)"),
    ]
    re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

    for line in test_log.splitlines():
        line = line.strip()

        for re_pass in re_pass_tests:
            match = re_pass.match(line)
            if match:
                test_name = match.group(1)
                if test_name in failed_tests:
                    continue
                if test_name in skipped_tests:
                    skipped_tests.remove(test_name)
                passed_tests.add(test_name)

        for re_fail in re_fail_tests:
            match = re_fail.match(line)
            if match:
                test_name = match.group(1)
                if test_name in passed_tests:
                    passed_tests.remove(test_name)
                if test_name in skipped_tests:
                    skipped_tests.remove(test_name)
                failed_tests.add(test_name)

        for re_skip in re_skip_tests:
            match = re_skip.match(line)
            if match:
                test_name = match.group(1)
                if test_name in passed_tests:
                    continue
                if test_name not in failed_tests:
                    skipped_tests.add(test_name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("securego", "gosec")
class Gosec(Instance):
    """Default gosec instance — for future PRs without number_interval."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GosecImageDefault(self.pr, self._config, go_version="latest")

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
        return gosec_parse_log(test_log)
