import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# tap test command varies by PR number (verified against each base commit's package.json):
#   316-566 (tap ^11-^14): tap tests/*.js --100
#   486, 608-815 (tap ^15-^16): tap tests/*.js --100 -Rspec
#   849-912 (tap ^19): tap run --allow-empty-coverage --disable-coverage --timeout=60000
#   928 (tap ^19): tap run tests/**/*.js --allow-empty-coverage --disable-coverage --timeout=60000
_SPEC_PRS = {486, 608, 621, 667, 735, 751, 781, 797, 803, 805, 809, 815}
_TAP19_PRS = {849, 864, 894, 912}
_TAP19_GLOB_PRS = {928}


def _get_test_cmd(pr_number: int) -> str:
    if pr_number in _TAP19_GLOB_PRS:
        return "npx tap run tests/**/*.js --allow-empty-coverage --disable-coverage --timeout=60000"
    if pr_number in _TAP19_PRS:
        return "npx tap run --allow-empty-coverage --disable-coverage --timeout=60000"
    if pr_number in _SPEC_PRS:
        return "npx tap tests/*.js --100 -Rspec"
    return "npx tap tests/*.js --100"


def _is_spec_output(pr_number: int) -> bool:
    return pr_number in _SPEC_PRS


class DotenvTapImageBase(Image):
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
        return "tap-base"

    def workdir(self) -> str:
        return "tap-base"

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

        return f"""\
FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

RUN npm ci || npm install || true

{self.clear_env}
"""


class DotenvTapImageDefault(Image):
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
        return DotenvTapImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = _get_test_cmd(self.pr.number)

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
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
git checkout {sha}

npm ci || npm install || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --3way --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn /home/test.patch || true
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        name = dep.image_name()
        tag = dep.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""\
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("motdotla", "dotenv_931_to_316")
class DotenvTap(Instance):
    """ERA 2 (tap framework, PRs 316-931). Parses both TAP protocol and spec reporter output."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DotenvTapImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        if _is_spec_output(self.pr.number):
            return self._parse_spec(clean_log)
        return self._parse_tap(clean_log)

    def _parse_tap(self, clean_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in clean_log.splitlines():
            # Real TAP output structure:
            #   ok 1 - tests/test-parse.js # time=285ms   ← 0-space: file aggregate (SKIP)
            #       ok 1 - test name # time=2ms            ← 4-space: actual test (CAPTURE)
            #           ok 1 - should be equal             ← 8-space: sub-assertion (SKIP)
            match = re.match(
                r"^    (ok|not ok) \d+ - (.+?)(?:\s+#\s*(.*))?$", line
            )
            if not match:
                continue

            status = match.group(1)
            test_name = match.group(2).strip()
            directive = (match.group(3) or "").strip().upper()

            if "SKIP" in directive:
                skipped_tests.add(test_name)
            elif status == "not ok":
                failed_tests.add(test_name)
            else:
                passed_tests.add(test_name)

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

    _SPEC_SUMMARY_RE = re.compile(
        r"^\s+\d+\s+(passing|pending|failing)\b"
    )

    def _parse_spec(self, clean_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_file = None
        current_suite = None
        in_summary = False

        for line in clean_log.splitlines():
            stripped = line.rstrip()

            if self._SPEC_SUMMARY_RE.match(stripped):
                in_summary = True
                continue
            if in_summary:
                continue

            if re.match(r"^\S.*\.js$", stripped):
                current_file = stripped.strip()
                current_suite = None
                continue

            suite_match = re.match(r"^  (\S.+)$", stripped)
            if suite_match and not stripped.lstrip().startswith(("\u2713", "-")):
                potential_suite = suite_match.group(1).strip()
                if not re.match(r"^\d+\)", potential_suite):
                    current_suite = potential_suite
                    continue

            if current_file and current_suite:
                prefix = f"{current_file} > {current_suite} > "
            elif current_file:
                prefix = f"{current_file} > "
            else:
                prefix = ""

            pass_match = re.match(r"^\s+\u2713\s+(.+)$", stripped)
            if pass_match:
                passed_tests.add(f"{prefix}{pass_match.group(1).strip()}")
                continue

            fail_match = re.match(r"^\s+(\d+)\)\s+(.+)$", stripped)
            if fail_match:
                failed_tests.add(f"{prefix}{fail_match.group(2).strip()}")
                continue

            skip_match = re.match(r"^\s+-\s+(.+)$", stripped)
            if skip_match:
                skipped_tests.add(f"{prefix}{skip_match.group(1).strip()}")
                continue

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
