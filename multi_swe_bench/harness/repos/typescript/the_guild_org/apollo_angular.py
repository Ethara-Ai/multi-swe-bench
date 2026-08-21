import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


class ApolloAngularImageBase(Image):
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
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN corepack enable || npm install -g yarn@1 || true

{code}

{self.clear_env}

"""


class ApolloAngularImageDefault(Image):
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
        return ApolloAngularImageBase(self.pr, self.config)

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
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git fetch origin {pr.base.sha} 2>/dev/null || git fetch --unshallow 2>/dev/null || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

yarn install --frozen-lockfile || yarn install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
cd packages/apollo-angular && NODE_OPTIONS=--experimental-modules npx jest --config jest.config.js --verbose --colors=false 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply /home/test.patch
cd packages/apollo-angular && NODE_OPTIONS=--experimental-modules npx jest --config jest.config.js --verbose --colors=false 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
cd packages/apollo-angular && NODE_OPTIONS=--experimental-modules npx jest --config jest.config.js --verbose --colors=false 2>&1 || true

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


@Instance.register("the-guild-org", "apollo-angular")
class ApolloAngular(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ApolloAngularImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        stack = []
        indentation_levels = []
        passed_pattern = re.compile(r"^\s*✓\s+(.*?)(?:\s*\(\d+\s*ms\))?$")
        skipped_pattern = re.compile(r"^\s*○\s+skipped\s+(.*)$")
        failed_pattern = re.compile(r"^\s*✕\s+(.*?)(?:\s*\(\d+\s*ms\))?$")

        for line in log.split("\n"):
            line = line.rstrip()
            if not line:
                continue
            indent = len(line) - len(line.lstrip(" "))
            content = line.lstrip(" ").rstrip()

            passed_match = passed_pattern.match(line)
            if passed_match:
                test_name = passed_match.group(1).strip()
                full_name = " ".join([desc for (_, desc) in stack] + [test_name])
                passed_tests.add(full_name)
                continue

            skipped_match = skipped_pattern.match(line)
            if skipped_match:
                test_name = skipped_match.group(1).strip()
                full_name = " ".join([desc for (_, desc) in stack] + [test_name])
                skipped_tests.add(full_name)
                continue

            failed_match = failed_pattern.match(line)
            if failed_match:
                test_name = failed_match.group(1).strip()
                full_name = " ".join([desc for (_, desc) in stack] + [test_name])
                failed_tests.add(full_name)
                continue

            if content and not content.startswith(
                ("✓", "○", "✕", "PASS", "FAIL", ">", "›")
            ):
                while indentation_levels and indent <= indentation_levels[-1]:
                    stack.pop()
                    indentation_levels.pop()
                stack.append((indent, content))
                indentation_levels.append(indent)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
