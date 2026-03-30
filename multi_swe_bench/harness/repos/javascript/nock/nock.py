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
        return "ubuntu:latest"

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

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
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt update && apt install -y git curl && \
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && \
    apt install -y nodejs

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
        return ImageBase(self.pr, self._config)

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

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

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}

# Detect test framework from package.json devDependencies
HAS_TAP=false
HAS_MOCHA=false
if grep -q '"tap"' package.json; then HAS_TAP=true; fi
if grep -q '"mocha"' package.json; then HAS_MOCHA=true; fi

set +e

if [ "$HAS_TAP" = true ] && [ "$HAS_MOCHA" = true ]; then
  npx mocha $(grep -rl '^\\s*it(' tests) --exit 2>&1
  npx tap ./tests/test_*.js 2>&1
elif [ "$HAS_TAP" = true ]; then
  npx tap ./tests/test_*.js 2>&1
else
  npx mocha --recursive tests --exit 2>&1
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

# Detect test framework from package.json devDependencies
HAS_TAP=false
HAS_MOCHA=false
if grep -q '"tap"' package.json; then HAS_TAP=true; fi
if grep -q '"mocha"' package.json; then HAS_MOCHA=true; fi

set +e

if [ "$HAS_TAP" = true ] && [ "$HAS_MOCHA" = true ]; then
  npx mocha $(grep -rl '^\\s*it(' tests) --exit 2>&1
  npx tap ./tests/test_*.js 2>&1
elif [ "$HAS_TAP" = true ]; then
  npx tap ./tests/test_*.js 2>&1
else
  npx mocha --recursive tests --exit 2>&1
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Detect test framework from package.json devDependencies
HAS_TAP=false
HAS_MOCHA=false
if grep -q '"tap"' package.json; then HAS_TAP=true; fi
if grep -q '"mocha"' package.json; then HAS_MOCHA=true; fi

set +e

if [ "$HAS_TAP" = true ] && [ "$HAS_MOCHA" = true ]; then
  npx mocha $(grep -rl '^\\s*it(' tests) --exit 2>&1
  npx tap ./tests/test_*.js 2>&1
elif [ "$HAS_TAP" = true ]; then
  npx tap ./tests/test_*.js 2>&1
else
  npx mocha --recursive tests --exit 2>&1
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


@Instance.register("nock", "nock")
class Nock(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_escape.sub("", test_log)

        # Detect format: TAP output starts with "TAP version" or has "ok N -" / "not ok N -" lines
        # Mocha output has "N passing" / "N failing" summary lines or "✓" / "✔" markers
        is_tap = False
        is_mocha = False

        re_tap_version = re.compile(r"^TAP version \d+")
        re_tap_test = re.compile(r"^\s*(ok|not ok)\s+\d+\s+-\s+(.+?)(?:\s+#.*)?$")
        re_tap_plan = re.compile(r"^1\.\.\d+$")
        re_tap_subtest = re.compile(r"^\s*# Subtest:\s*(.+)")
        re_tap_skip = re.compile(r"#\s*SKIP\b", re.IGNORECASE)
        re_tap_todo = re.compile(r"#\s*TODO\b", re.IGNORECASE)

        re_mocha_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+.*\))?\s*$")
        re_mocha_fail = re.compile(r"^\s*\d+\)\s+(.+?)\s*$")
        re_mocha_pending = re.compile(r"^\s*-\s+(.+?)\s*$")
        re_mocha_passing = re.compile(r"^\s*\d+\s+passing", re.IGNORECASE)
        re_mocha_failing = re.compile(r"^\s*\d+\s+failing", re.IGNORECASE)

        lines = test_log.splitlines()

        # Pre-scan to detect format
        for line in lines:
            stripped = line.strip()
            if re_tap_version.match(stripped):
                is_tap = True
                break
            if re_tap_plan.match(stripped):
                is_tap = True
                break
            if re_mocha_passing.match(stripped) or re_mocha_failing.match(stripped):
                is_mocha = True
                break
            if re_mocha_pass.match(stripped):
                is_mocha = True
                break
            if re_tap_test.match(stripped) and not re_mocha_fail.match(stripped):
                is_tap = True
                break

        if is_tap:
            # TAP format parsing
            # nock uses node-tap which supports subtests
            subtest_stack: list[str] = []

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue

                # Track subtests for hierarchical test names
                subtest_match = re_tap_subtest.match(line)
                if subtest_match:
                    indent = len(line) - len(line.lstrip())
                    level = indent // 4
                    subtest_stack = subtest_stack[:level]
                    subtest_stack.append(subtest_match.group(1).strip())
                    continue

                tap_match = re_tap_test.match(stripped)
                if tap_match:
                    status = tap_match.group(1)
                    test_name = tap_match.group(2).strip()

                    # Build full test name with subtest context
                    if subtest_stack:
                        full_name = ":".join(subtest_stack + [test_name])
                    else:
                        full_name = test_name

                    # Check for SKIP or TODO directives
                    if re_tap_skip.search(line):
                        skipped_tests.add(full_name)
                    elif re_tap_todo.search(line):
                        skipped_tests.add(full_name)
                    elif status == "ok":
                        passed_tests.add(full_name)
                    elif status == "not ok":
                        failed_tests.add(full_name)
                    continue

        else:
            # Mocha format parsing (default if not detected as TAP)
            in_mocha_section = False

            for line in lines:
                stripped = line.rstrip()
                if not stripped:
                    continue

                if re_mocha_passing.match(stripped) or re_mocha_failing.match(stripped):
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

        # Clean up: failed tests should not appear in passed or skipped
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
