import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MpfImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "python:3.7"

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

RUN python -m pip install --no-cache-dir --upgrade "pip<24" "setuptools<60" wheel \\
    && python -m pip install --no-cache-dir \\
        "ruamel.yaml==0.15.100" \\
        "pyserial==3.4" \\
        "pyserial-asyncio==0.4" \\
        "asciimatics==1.11.0" \\
        "terminaltables==3.1.0" \\
        "sortedcontainers==2.1.0" \\
        "psutil==5.7.0" \\
        "typing==3.7.4.1"

{code}

{self.clear_env}

"""


class MpfImageDefault(Image):
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
        return MpfImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr"

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
                "run_tests.py",
                '''"""Emit one greppable result line per test in the MPF suite.

MPF is a unittest project: `python -m unittest discover -s mpf/tests` is what
setup.py (test_suite), the Makefile and .travis.yml all run, and it is the only
runner the suite is green under. pytest is not usable here -- it walks
mpf/tests/machine_files/, whose test_*.py files are machine fixtures rather
than tests (one of them fails collection outright and aborts the run), and it
collects the `test_config` / `test_config_directory` decorators exported by
MpfTestCase as though they were test functions.

Plain `unittest discover -v` is not parseable either. MpfTestCase.setUp calls
unittest_verbosity(), which walks the stack for the running TextTestRunner and
attaches a DEBUG StreamHandler to the root logger as soon as that verbosity is
above 1. The machine log lines it then emits share a stream with unittest's own
progress output and land between the "<id> ... " prefix and the "ok" that
terminates it, smearing one result across many lines.

Verbosity 1 keeps the suite silent, so this runner stays there and prints its
own result lines instead. Each is preceded by a newline because a few tests
(test_OPP's serial fake, test_Shows' timing warning) print without a trailing
one.
"""
import sys
import unittest

MARKER = "MPF_TEST_RESULT:"

class EmittingTestResult(unittest.TextTestResult):
    """TextTestResult that announces every outcome on stdout by test id."""

    def _emit(self, status, test):
        sys.stdout.write("\\n{} {} {}\\n".format(MARKER, status, test.id()))
        sys.stdout.flush()

    def addSuccess(self, test):
        super().addSuccess(test)
        self._emit("PASS", test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._emit("FAIL", test)

    def addError(self, test, err):
        super().addError(test, err)
        self._emit("ERROR", test)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._emit("SKIP", test)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._emit("XFAIL", test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._emit("XPASS", test)

def main():
    suite = unittest.TestLoader().discover(start_dir="mpf/tests", top_level_dir=".")
    runner = unittest.TextTestRunner(verbosity=1, resultclass=EmittingTestResult)
    return 0 if runner.run(suite).wasSuccessful() else 1

if __name__ == "__main__":
    sys.exit(main())
''',
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null; then
    git fetch --no-tags --depth 1 https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
fi
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

python -m pip install --no-cache-dir --no-deps -e . || true

python --version
python -c "import mpf, mpf._version; print(mpf._version.__version__, mpf.__file__)"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
python /home/run_tests.py

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
python /home/run_tests.py

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python /home/run_tests.py

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


@Instance.register("missionpinball", "mpf")
class Mpf(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MpfImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text: str) -> str:
            return re.compile(r"\x1B\[[0-?9;]*[mK]").sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        result_re = re.compile(
            r"MPF_TEST_RESULT:\s+(PASS|FAIL|ERROR|SKIP|XFAIL|XPASS)\s+(\S+)"
        )

        for line in test_log.splitlines():
            match = result_re.search(line)
            if not match:
                continue

            status, name = match.group(1), match.group(2)

            if status in ("PASS", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAIL", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
