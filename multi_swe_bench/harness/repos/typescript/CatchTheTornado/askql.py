"""CatchTheTornado/askql harness config — Jest 26, npm, TypeScript with custom test runner."""

import re
import textwrap
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AskQLImageBase(Image):
    """Base Docker image: node:14 with the repo cloned.

    askql requires node:14 because node-sass@4.x native compilation
    fails on node >= 16 (C++ std::remove_cv_t incompatibility).
    Uses npm (bundled with node:14 as npm@6.14.18).
    """

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
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive

{code}

{self.clear_env}

"""


class AskQLImageDefault(Image):
    """PR-specific Docker layer: patches, prepare, and run scripts.

    askql uses a custom Jest test runner (dist/test.jest.testRunner.js)
    and transformer (dist/javascript.jest.transformer.js) that must be
    built via jest.build.config.js before tests can run.

    Build step: npx jest --config jest.build.config.js --no-cache
    Test step:  npx jest --config jest.test.config.js --verbose
    """

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
        return AskQLImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install dependencies (npm, includes native node-sass compilation)
npm install

# Compile TypeScript to dist/ (produces test runners and transformer
# needed by jest.build.config.js and jest.test.config.js)
npx tsc

# Build .ask files using the custom test runner (now available in dist/)
npx jest --config jest.build.config.js --no-cache
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}

npx tsc || true
npx jest --config jest.build.config.js --no-cache || true
npx jest --config jest.test.config.js --verbose 2>&1 || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

npm install || true
npx tsc || true
npx jest --config jest.build.config.js --no-cache || true
npx jest --config jest.test.config.js --verbose 2>&1 || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

npm install || true
npx tsc || true
npx jest --config jest.build.config.js --no-cache || true
npx jest --config jest.test.config.js --verbose 2>&1 || true
""".format(repo=self.pr.repo),
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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )

        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("CatchTheTornado", "askql")
class AskQLInstance(Instance):
    """Harness instance for CatchTheTornado/askql — Jest 26 with custom runner."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return AskQLImageDefault(self.pr, self._config)

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
        """Parse Jest 26 verbose output into pass/fail/skip sets.

        askql Jest output (with --verbose and displayName 'test') looks like:

            PASS test src/askscript/__tests__/01-basics/program01-ask.ask
              ✓ starts
              ✓ compiles
              ✓ computes (3 ms)

            FAIL test src/askscript/__tests__/07-arrays/program17f-list-map.ask
              ✕ computes (15 ms)

              ✎ todo some-skipped-test

        Suite-level lines have 'test' prefix from displayName config.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", test_log)

        # Suite-level patterns (with 'test' displayName prefix)
        re_suite_pass = re.compile(r"^\s*PASS\s+test\s+(.+?)\s*$")
        re_suite_fail = re.compile(r"^\s*FAIL\s+test\s+(.+?)\s*$")

        # Individual test patterns (verbose output)
        re_test_pass = re.compile(
            r"^\s+[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$"
        )
        re_test_fail = re.compile(
            r"^\s+[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$"
        )
        re_test_skip = re.compile(r"^\s+✎\s+todo\s+(.+?)\s*$")

        for line in clean_log.splitlines():
            # Suite-level pass
            m = re_suite_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            # Suite-level fail
            m = re_suite_fail.match(line)
            if m:
                test_name = m.group(1)
                failed_tests.add(test_name)
                if test_name in passed_tests:
                    passed_tests.remove(test_name)
                continue

            # Individual test pass
            m = re_test_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            # Individual test fail
            m = re_test_fail.match(line)
            if m:
                test_name = m.group(1)
                failed_tests.add(test_name)
                if test_name in passed_tests:
                    passed_tests.remove(test_name)
                continue

            # Individual test skip/todo
            m = re_test_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

        # Ensure mutual exclusivity
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
