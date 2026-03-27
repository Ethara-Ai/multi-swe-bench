import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class BlitzImageBase(Image):

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

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git build-essential python3

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

"""


class ImageDefault(Image):
    """Era 1: blitz-js/blitz PRs 1402-3142 (releases 0.28-0.45, base_ref=canary).
    Toolchain: Yarn workspaces, Jest, Node 14, ultra-runner.
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
        return BlitzImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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

cd /home/{repo_name}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh
yarn install --frozen-lockfile || yarn install || true

""".format(repo_name=repo_name, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo_name}
npx ultra -r test 2>&1

""".format(repo_name=repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo_name}
git apply --exclude yarn.lock --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn /home/test.patch || \
git apply --exclude yarn.lock --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn --reject /home/test.patch || true
find . -name '*.rej' -delete
npx ultra -r test 2>&1

""".format(repo_name=repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo_name}
git apply --exclude yarn.lock --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn /home/test.patch /home/fix.patch || \
git apply --exclude yarn.lock --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
find . -name '*.rej' -delete
npx ultra -r test 2>&1

""".format(repo_name=repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN npm install -g ultra-runner

RUN yarn install --frozen-lockfile || yarn install || true

{copy_commands}

{self.clear_env}

"""


@Instance.register("blitz-js", "blitz_3142_to_1402")
class BLITZ_3142_TO_1402(Instance):
    """Era 1 Instance: blitz-js/blitz PRs 1402-3142."""

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
        """Parse Jest output from ultra -r test.

        Lines may be prefixed by ultra-runner scope, e.g.:
            [@blitzjs/core::jest] PASS src/server/resolver.test.ts
            FAIL test/react-query-utils.test.ts
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Match PASS/FAIL anywhere in line (ultra-runner may prepend package prefix)
        passed_tests = set(
            re.findall(r"PASS\s+(.*?)\s*$", test_log, re.MULTILINE)
        )
        failed_tests = set(
            re.findall(r"FAIL\s+(.*?)\s*$", test_log, re.MULTILINE)
        )
        skipped_tests = set(
            re.findall(
                r"(?:SKIP|SKIPPED|XSKIP):?\s+(.*?)\s*$",
                test_log,
                re.MULTILINE,
            )
        )

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
