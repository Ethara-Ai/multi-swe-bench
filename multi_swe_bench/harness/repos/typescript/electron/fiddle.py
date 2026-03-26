import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FiddleImageBase(Image):
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
        return "node:14"

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

RUN sed -i 's|http://deb.debian.org|http://archive.debian.org|g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class FiddleImageDefault(Image):
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
        return FiddleImageBase(self.pr, self.config)

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

# Workaround for npm 6 "cb() never called" bug on Node 14:
# clean cache and retry npm ci with backoff
npm cache clean --force 2>/dev/null || true

install_ok=false
for attempt in 1 2 3; do
  if npm ci 2>&1; then
    install_ok=true
    break
  fi
  echo "npm ci attempt $attempt failed, cleaning cache and retrying..."
  npm cache clean --force 2>/dev/null || true
  sleep $((attempt * 2))
done

if [ "$install_ok" = false ]; then
  echo "npm ci failed after 3 attempts, trying npm install..."
  npm install 2>&1 || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
./node_modules/.bin/jest --config=jest.json --verbose --runInBand 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./node_modules/.bin/jest --config=jest.json --verbose --runInBand 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./node_modules/.bin/jest --config=jest.json --verbose --runInBand 2>&1

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


@Instance.register("electron", "fiddle")
class Fiddle(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return FiddleImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes for reliable parsing
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_suite = None
        describe_stack: list[tuple[int, str]] = []  # (indent_level, name)
        in_error_section = False

        re_pass_suite = re.compile(r"^PASS\s+(\S+)(?:\s+\(.+\))?$")
        re_fail_suite = re.compile(r"^FAIL\s+(\S+)(?:\s+\(.+\))?$")
        re_pass_test = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_fail_test = re.compile(r"^\s*[✕×✗]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_skip_test = re.compile(r"^\s*[○◌]\s+(.+?)$")

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # Suite headers reset all tracking state
            pass_suite = re_pass_suite.match(stripped)
            if pass_suite:
                current_suite = pass_suite.group(1)
                describe_stack = []
                in_error_section = False
                continue

            fail_suite = re_fail_suite.match(stripped)
            if fail_suite:
                current_suite = fail_suite.group(1)
                describe_stack = []
                in_error_section = False
                continue

            # ● marks Jest error detail sections — skip everything after
            if stripped.startswith("●"):
                in_error_section = True
                continue

            if in_error_section or not current_suite:
                continue

            # Test result patterns
            pass_test = re_pass_test.match(line)
            if pass_test:
                test_name = self._build_test_name(
                    current_suite, describe_stack, pass_test.group(1).strip()
                )
                passed_tests.add(test_name)
                continue

            fail_test = re_fail_test.match(line)
            if fail_test:
                test_name = self._build_test_name(
                    current_suite, describe_stack, fail_test.group(1).strip()
                )
                failed_tests.add(test_name)
                continue

            skip_test = re_skip_test.match(line)
            if skip_test:
                test_name = self._build_test_name(
                    current_suite, describe_stack, skip_test.group(1).strip()
                )
                skipped_tests.add(test_name)
                continue

            # Describe block detection: indented non-test line within a suite
            if line != stripped:  # has leading whitespace
                indent = len(line) - len(line.lstrip())
                # Pop describe entries at same or deeper indent level
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                describe_stack.append((indent, stripped))

        # Deduplicate: worst result wins (failed > skipped > passed)
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

    @staticmethod
    def _build_test_name(
        suite: str, describe_stack: list[tuple[int, str]], leaf_name: str
    ) -> str:
        """Build full test name including describe block context.

        Produces names like:
          suite.tsx:describe1 > describe2 > test name
        This avoids collisions when different describe blocks contain
        identically-named it() tests (e.g. two 'handles an error').
        """
        if describe_stack:
            describe_path = " > ".join(name for _, name in describe_stack)
            return f"{suite}:{describe_path} > {leaf_name}"
        return f"{suite}:{leaf_name}"
