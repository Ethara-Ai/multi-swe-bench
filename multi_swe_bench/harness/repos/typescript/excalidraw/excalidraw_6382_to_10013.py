import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ExcalidrawNewImageBase(Image):
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
        return "base-new"

    def workdir(self) -> str:
        return "base-new"

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
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

RUN corepack enable && corepack prepare yarn@4 --activate || npm install -g yarn

{code}

{self.clear_env}

"""


class ExcalidrawNewImageDefault(Image):
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
        return ExcalidrawNewImageBase(self.pr, self._config)

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

yarn install || npm install

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
yarn test --run --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
yarn install || npm install
yarn test --run --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
yarn install || npm install
yarn test --run --reporter=verbose

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


@Instance.register("excalidraw", "excalidraw_6382_to_10013")
class EXCALIDRAW_6382_TO_10013(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ExcalidrawNewImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        lines = log.split("\n")
        for line in lines:
            line = line.strip()

            # Vitest verbose: " ✓ file.test.tsx > suite > test name (123ms)"
            vitest_verbose_pass = re.match(r"[✓√✔]\s+(\S+\.test\.\w+)\s+>\s+(.+?)(?:\s+\d+m?s)?$", line)
            if vitest_verbose_pass:
                test_file = vitest_verbose_pass.group(1)
                test_desc = vitest_verbose_pass.group(2).strip()
                test_id = f"{test_file} > {test_desc}"
                failed_tests.discard(test_id)
                passed_tests.add(test_id)
                continue

            # Vitest verbose fail: " × file.test.tsx > suite > test name"
            vitest_verbose_fail = re.match(r"[×✗❌]\s+(\S+\.test\.\w+)\s+>\s+(.+?)(?:\s+\d+m?s)?$", line)
            if vitest_verbose_fail:
                test_file = vitest_verbose_fail.group(1)
                test_desc = vitest_verbose_fail.group(2).strip()
                test_id = f"{test_file} > {test_desc}"
                passed_tests.discard(test_id)
                failed_tests.add(test_id)
                continue

            # Jest/Vitest FAIL pattern: "FAIL  file.test.tsx > suite > test" or "FAIL file.test.tsx"
            fail_match = re.search(r"FAIL\s+(\S+\.test\.\w+)(?:\s+>\s+(.+?))?$", line)
            if fail_match:
                test_file = fail_match.group(1)
                test_desc = fail_match.group(2)
                if test_desc:
                    test_id = f"{test_file} > {test_desc.strip()}"
                    passed_tests.discard(test_id)
                    failed_tests.add(test_id)
                continue

            # Jest individual test case fail via "●"
            if "●" in line:
                match = re.search(r"●\s*(.+?)(?:\s*›|$)", line)
                if match:
                    test_name = match.group(1).strip()
                    if test_name:
                        failed_tests.add(test_name)
                continue

            # Skipped tests
            skip_match = re.search(r"[○⊘]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", line)
            if skip_match:
                skipped_tests.add(skip_match.group(1).strip())
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


@Instance.register("excalidraw", "excalidraw")
class EXCALIDRAW_CATCHALL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        if pr.number <= 6336:
            from multi_swe_bench.harness.repos.typescript.excalidraw.excalidraw_527_to_6336 import EXCALIDRAW_527_TO_6336
            self._delegate = EXCALIDRAW_527_TO_6336(pr, config, *args, **kwargs)
        else:
            self._delegate = EXCALIDRAW_6382_TO_10013(pr, config, *args, **kwargs)
        self._pr = pr

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return self._delegate.dependency()

    def run(self, run_cmd: str = "") -> str:
        return self._delegate.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return self._delegate.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return self._delegate.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, log: str) -> TestResult:
        return self._delegate.parse_log(log)
