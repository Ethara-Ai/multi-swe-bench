import re
import textwrap
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class BrowserosaurusImageBase(Image):
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
        return "node:14-bullseye"

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
# electron is a devDependency whose postinstall downloads a ~90MB platform
# binary. The suite runs under jsdom against __mocks__/electron.js, so the
# real binary is never needed and the download only makes the build flaky.
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
RUN apt-get update && apt-get install -y git curl python3 make g++
{code}

{self.clear_env}

"""


class BrowserosaurusImageDefault(Image):
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
        return BrowserosaurusImageBase(self.pr, self._config)

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

""",
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

export ELECTRON_SKIP_BINARY_DOWNLOAD=1
yarn install --frozen-lockfile --ignore-scripts --network-timeout 600000 || yarn install --ignore-scripts --network-timeout 600000 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
cd /home/{pr.repo}

./node_modules/.bin/jest --ci --verbose --colors=false 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch

./node_modules/.bin/jest --ci --verbose --colors=false 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --3way /home/fix.patch

./node_modules/.bin/jest --ci --verbose --colors=false 2>&1

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


@Instance.register("will-stone", "browserosaurus")
class Browserosaurus(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BrowserosaurusImageDefault(self.pr, self._config)

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

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        _file_pat = r"\S+\.(?:spec|test)\.(?:tsx?|jsx?)"
        re_pass_suite = re.compile(rf"^PASS\s+({_file_pat})")
        re_fail_suite = re.compile(rf"^FAIL\s+({_file_pat})")

        _dur = r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?"
        re_passed = re.compile(rf"^✓\s+(.+?){_dur}$")
        re_failed = re.compile(rf"^✕\s+(.+?){_dur}$")
        re_skipped = re.compile(rf"^[○✎]\s+(?:skipped\s+|todo\s+)?(.+?){_dur}$")

        current_suite: Optional[str] = None
        suite_had_tests: dict[str, bool] = {}
        failed_suites: set[str] = set()

        def qualify(name: str) -> str:
            name = name.strip()
            return f"{current_suite}::{name}" if current_suite else name

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            m = re_pass_suite.match(stripped)
            if m:
                current_suite = m.group(1)
                suite_had_tests.setdefault(current_suite, False)
                continue

            m = re_fail_suite.match(stripped)
            if m:
                current_suite = m.group(1)
                suite_had_tests.setdefault(current_suite, False)
                failed_suites.add(current_suite)
                continue

            m = re_passed.match(stripped)
            if m:
                passed_tests.add(qualify(m.group(1)))
                if current_suite:
                    suite_had_tests[current_suite] = True
                continue

            m = re_failed.match(stripped)
            if m:
                failed_tests.add(qualify(m.group(1)))
                if current_suite:
                    suite_had_tests[current_suite] = True
                continue

            m = re_skipped.match(stripped)
            if m:
                skipped_tests.add(qualify(m.group(1)))
                if current_suite:
                    suite_had_tests[current_suite] = True
                continue

        for suite in failed_suites:
            if not suite_had_tests.get(suite, False):
                failed_tests.add(suite)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
