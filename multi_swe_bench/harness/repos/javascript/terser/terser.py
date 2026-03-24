import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
RUN apt update && apt install -y git build-essential

{code}

{self.clear_env}

"""


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self.config)

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

export PATH="$PWD/node_modules/.bin:$PATH"
npm install || true
npm run build || npm run prepare || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PATH="$PWD/node_modules/.bin:$PATH"
npm test 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PATH="$PWD/node_modules/.bin:$PATH"
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm run build || npm run prepare || true
npm test 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PATH="$PWD/node_modules/.bin:$PATH"
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm run build || npm run prepare || true
npm test 2>&1 || true

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


@Instance.register("terser", "terser")
class Terser(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    _NPM_ENSURE = "[ -d node_modules ] || npm install || true"
    _BUILD = "npm run build || npm run prepare || true"
    _PATH_SETUP = 'export PATH="$PWD/node_modules/.bin:$PATH"'

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return (
            f"bash -c 'set -e; cd /home/{self.pr.repo}; "
            f"{self._PATH_SETUP}; "
            f"{self._NPM_ENSURE}; "
            f"npm test 2>&1 || true'"
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return (
            f"bash -c 'set -e; cd /home/{self.pr.repo}; "
            f"{self._PATH_SETUP}; "
            f"{self._NPM_ENSURE}; "
            f"git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch; "
            f"{self._BUILD}; "
            f"npm test 2>&1 || true'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return (
            f"bash -c 'set -e; cd /home/{self.pr.repo}; "
            f"{self._PATH_SETUP}; "
            f"{self._NPM_ENSURE}; "
            f"git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch; "
            f"{self._BUILD}; "
            f"npm test 2>&1 || true'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_escape.sub("", test_log)

        current_compress_test = None
        current_compress_file = None
        compress_test_failed = False
        # v3/v4 log individual test names; v5 only logs failures (names invisible for passing tests)
        has_named_compress_tests = False

        re_compress_test_v5 = re.compile(r"^---\s+Running test \[(.+?)\]")
        re_compress_test_v3v4 = re.compile(r"^\s+Running test \[(.+?)\]")
        re_compress_file = re.compile(r"^---\s+(\S+\.js)\s*$")
        re_compress_fail = re.compile(r"^!!!\s*(failed|Invalid input|Cannot parse)")
        re_compress_pass_summary = re.compile(r"^Passed\s+(\d+)\s+test cases?\.", re.IGNORECASE)
        re_compress_fail_summary = re.compile(r"^!!!\s*Failed\s+(\d+)\s+test cases?\.")

        # Mocha spec: "✓ name (Nms)" / "N) name" / "- name"
        re_mocha_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+.*\))?\s*$")
        re_mocha_fail = re.compile(r"^\s*\d+\)\s+(.+?)\s*$")
        re_mocha_pending = re.compile(r"^\s*-\s+(.+?)\s*$")

        in_mocha_section = False
        re_mocha_passing = re.compile(r"^\s*\d+\s+passing", re.IGNORECASE)
        re_mocha_failing = re.compile(r"^\s*\d+\s+failing", re.IGNORECASE)
        re_mocha_pending_summary = re.compile(r"^\s*\d+\s+pending", re.IGNORECASE)

        def _make_test_name(file_ctx, test_name):
            if file_ctx:
                return f"{file_ctx}:{test_name}"
            return test_name

        def _finalize_compress_test():
            nonlocal current_compress_test, compress_test_failed
            if current_compress_test is not None:
                full_name = _make_test_name(current_compress_file, current_compress_test)
                if compress_test_failed:
                    failed_tests.add(full_name)
                else:
                    passed_tests.add(full_name)
                current_compress_test = None
                compress_test_failed = False

        def _handle_compress_summary(total_count, is_fail_summary):
            """v5 synthetic name fallback: when no individual test names are logged,
            generate synthetic names from summary counts (videojs pattern)."""
            _finalize_compress_test()

            if has_named_compress_tests:
                return

            named_fail_count = len(failed_tests)

            if is_fail_summary:
                synthetic_fail_count = max(0, total_count - named_fail_count)
                for i in range(synthetic_fail_count):
                    failed_tests.add(f"compress_failed_test_{i + 1}")
            else:
                for i in range(total_count):
                    passed_tests.add(f"compress_test_{i + 1}")

        lines = test_log.splitlines()
        for line in lines:
            stripped = line.rstrip()
            if not stripped:
                continue

            compress_match = (
                re_compress_test_v5.match(stripped)
                or re_compress_test_v3v4.match(stripped)
            )
            if compress_match and not in_mocha_section:
                _finalize_compress_test()
                current_compress_test = compress_match.group(1)
                compress_test_failed = False
                has_named_compress_tests = True
                continue

            file_match = re_compress_file.match(stripped)
            if file_match and not in_mocha_section:
                _finalize_compress_test()
                current_compress_file = file_match.group(1)
                continue

            pass_summary = re_compress_pass_summary.match(stripped)
            if pass_summary:
                _handle_compress_summary(int(pass_summary.group(1)), False)
                in_mocha_section = True
                continue

            fail_summary = re_compress_fail_summary.match(stripped)
            if fail_summary:
                _handle_compress_summary(int(fail_summary.group(1)), True)
                in_mocha_section = True
                continue

            if re_compress_fail.match(stripped) and not in_mocha_section:
                compress_test_failed = True
                continue

            if re_mocha_passing.match(stripped) or re_mocha_failing.match(stripped):
                _finalize_compress_test()
                in_mocha_section = True
                continue

            if re_mocha_pending_summary.match(stripped):
                in_mocha_section = True
                continue

            mocha_pass = re_mocha_pass.match(stripped)
            if mocha_pass:
                test_name = mocha_pass.group(1).strip()
                if test_name:
                    passed_tests.add(test_name)
                in_mocha_section = True
                continue

            mocha_fail = re_mocha_fail.match(stripped)
            if mocha_fail and in_mocha_section:
                test_name = mocha_fail.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                continue

            mocha_pending = re_mocha_pending.match(stripped)
            if mocha_pending and in_mocha_section:
                test_name = mocha_pending.group(1).strip()
                if test_name:
                    skipped_tests.add(test_name)
                continue

        _finalize_compress_test()

        for test in failed_tests:
            passed_tests.discard(test)
            skipped_tests.discard(test)
        for test in skipped_tests:
            passed_tests.discard(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
