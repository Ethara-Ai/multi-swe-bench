import re
import textwrap
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ReactStyleguidistImageBase(Image):
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

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class ReactStyleguidistImageDefault(Image):
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
        return ReactStyleguidistImageBase(self.pr, self.config)

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

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
npx jest --no-coverage --verbose 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npx jest --no-coverage --verbose 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npx jest --no-coverage --verbose 2>&1

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


@Instance.register("styleguidist", "react-styleguidist")
class ReactStyleguidist(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ReactStyleguidistImageDefault(self.pr, self._config)

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_re.sub("", test_log)

        current_file = ""
        describe_stack = []
        in_error_section = False

        for line in clean_log.splitlines():
            stripped = line.strip()

            if not stripped:
                continue

            file_match = re.match(r"^\s*(PASS|FAIL)\s+(\S+)", line)
            if file_match:
                current_file = file_match.group(2).strip()
                describe_stack = []
                in_error_section = False
                continue

            # jest error detail section: starts with bullet
            if stripped.startswith("\u25cf"):
                in_error_section = True
                continue

            if in_error_section:
                continue

            if re.match(
                r"^(Test Suites:|Tests:|Snapshots:|Time:|Ran all test suites)",
                stripped,
            ):
                continue

            if not current_file:
                continue

            indent = len(line) - len(line.lstrip())

            # jest verbose: checkmark for passed
            pass_match = re.match(
                r"^\s+[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line
            )
            if pass_match:
                test_name = pass_match.group(1).strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                parts = [current_file] + [d[1] for d in describe_stack] + [test_name]
                full_name = " > ".join(parts)
                passed_tests.add(full_name)
                continue

            # jest verbose: cross for failed
            fail_match = re.match(
                r"^\s+[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line
            )
            if fail_match:
                test_name = fail_match.group(1).strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                parts = [current_file] + [d[1] for d in describe_stack] + [test_name]
                full_name = " > ".join(parts)
                failed_tests.add(full_name)
                continue

            # jest verbose: circle for skipped/todo
            skip_match = re.match(
                r"^\s+○\s+(?:skipped|todo)\s+(.+?)\s*$", line
            )
            if skip_match:
                test_name = skip_match.group(1).strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                parts = [current_file] + [d[1] for d in describe_stack] + [test_name]
                full_name = " > ".join(parts)
                skipped_tests.add(full_name)
                continue

            # describe block: indented text without status markers
            if indent > 0:
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                describe_stack.append((indent, stripped))

        # dedup: worst result wins (failed > skipped > passed)
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
