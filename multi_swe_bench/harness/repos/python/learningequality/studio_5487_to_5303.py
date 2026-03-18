import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _get_js_test_files(test_patch: str) -> list[str]:
    """Extract JS/TS test file paths from a test patch."""
    diff_pat = r"diff --git a/.* b/(.*)"
    files = re.findall(diff_pat, test_patch)
    return [f for f in files if f.endswith((".js", ".jsx", ".ts", ".tsx", ".vue"))]


class StudioJSImageBase(Image):
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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-jest"

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

RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential python3 \
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable && corepack prepare pnpm@10.12.4 --activate

{code}

WORKDIR /home/{self.pr.repo}

RUN pnpm install --frozen-lockfile || pnpm install || true

{self.clear_env}

"""


class StudioJSImageDefault(Image):
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
        return StudioJSImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = _get_js_test_files(self.pr.test_patch)
        test_cmd = "pnpm exec jest --config jest_config/jest.conf.js --verbose --no-coverage --passWithNoTests --forceExit {}".format(
            " ".join(test_files)
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}
pnpm install --frozen-lockfile || pnpm install || true
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    test_cmd=test_cmd,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        return f"""FROM {name}:{tag}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("learningequality", "studio_5487_to_5303")
class STUDIO_5487_TO_5303(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return StudioJSImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_suite = ""

        for line in clean_log.split("\n"):
            # Track current test suite (file path from PASS/FAIL line)
            suite_match = re.match(r"^\s*(PASS|FAIL)\s+(.+)$", line)
            if suite_match:
                current_suite = suite_match.group(2).strip()
                continue

            # Passed tests: checkmark markers
            pass_match = re.match(
                r"\s+[\u2713\u221a]\s+(.*?)(?:\s+\(\d+\s*m?s\))?\s*$", line
            )
            if pass_match:
                name = pass_match.group(1).strip()
                prefix = f"{current_suite} > " if current_suite else ""
                passed_tests.add(f"{prefix}{name}")
                continue

            # Failed tests: cross markers
            fail_match = re.match(
                r"\s+[\u2715\u00d7]\s+(.*?)(?:\s+\(\d+\s*m?s\))?\s*$", line
            )
            if fail_match:
                name = fail_match.group(1).strip()
                prefix = f"{current_suite} > " if current_suite else ""
                failed_tests.add(f"{prefix}{name}")
                continue

            # Skipped tests: circle marker
            skip_match = re.match(
                r"\s+[\u25cb]\s+(?:skipped\s+)?(.*?)(?:\s+\(\d+\s*m?s\))?\s*$", line
            )
            if skip_match:
                name = skip_match.group(1).strip()
                prefix = f"{current_suite} > " if current_suite else ""
                skipped_tests.add(f"{prefix}{name}")
                continue

        # Deduplicate (worst result wins)
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
