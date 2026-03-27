import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class BlitzImageBaseV2(Image):

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
        return "node:18"

    def image_tag(self) -> str:
        return "base-v2"

    def workdir(self) -> str:
        return "base-v2"

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
RUN echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.conf || true

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

"""


class ImageDefault(Image):
    """Era 2: blitz-js/blitz PRs 4269-4427 (releases 2.0-3.0, base_ref=main).
    Toolchain: pnpm@8.6.6 workspaces, Vitest, Turbo, Node 18.
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
        return BlitzImageBaseV2(self.pr, self._config)

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

echo 524288 > /proc/sys/fs/inotify/max_user_watches 2>/dev/null || true
ulimit -n 65536 2>/dev/null || true
cd /home/{repo_name}
git apply --exclude pnpm-lock.yaml --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn /home/test.patch /home/fix.patch || \
git apply --exclude pnpm-lock.yaml --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
find . -name '*.rej' -delete
pnpm turbo run build 2>&1 || true
pnpm turbo run test 2>&1

""".format(repo_name=repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

echo 524288 > /proc/sys/fs/inotify/max_user_watches 2>/dev/null || true
ulimit -n 65536 2>/dev/null || true
cd /home/{repo_name}
git apply --exclude pnpm-lock.yaml --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn /home/test.patch || \
git apply --exclude pnpm-lock.yaml --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn --reject /home/test.patch || true
find . -name '*.rej' -delete
pnpm turbo run build 2>&1 || true
pnpm turbo run test 2>&1

""".format(repo_name=repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

echo 524288 > /proc/sys/fs/inotify/max_user_watches 2>/dev/null || true
ulimit -n 65536 2>/dev/null || true
cd /home/{repo_name}
git apply --exclude pnpm-lock.yaml --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn /home/test.patch /home/fix.patch || \
git apply --exclude pnpm-lock.yaml --exclude '*.node' --exclude '*.jpg' --exclude '*.png' --exclude '*.ico' --exclude '*.gif' --exclude '*.bmp' --exclude '*.webp' --exclude '*.sqlite' --exclude '*.db' --exclude '*.zip' --exclude '*.tar.gz' --exclude '*.woff' --exclude '*.woff2' --exclude '*.ttf' --exclude '*.eot' --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
find . -name '*.rej' -delete
pnpm turbo run build 2>&1 || true
pnpm turbo run test 2>&1

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

RUN npm install -g pnpm@8.6.6
RUN npm install -g turbo

RUN pnpm install || true

{copy_commands}

{self.clear_env}

"""


@Instance.register("blitz-js", "blitz_4427_to_4269")
class BLITZ_4427_TO_4269(Instance):
    """Era 2 Instance: blitz-js/blitz PRs 4269-4427."""

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
        """Parse Vitest output from turbo run test.

        Lines are prefixed by turbo package scope, e.g.:
            @blitzjs/generator:test:  ✓ generators/app-generator.test.ts  (1 test) 2ms
            @blitzjs/codemod:test:  ❯ src/index.test.ts  (1 test | 1 failed) 165ms
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Match vitest file-level pass after turbo prefix: ...  ✓ <file>  (<N> tests) <time>
        passed_tests = set(
            re.findall(
                r"\u2713\s+(\S+)\s+\(\d+\s+tests?\)",
                test_log,
            )
        )
        # Match vitest file-level fail after turbo prefix: ...  ❯ <file>  (<N> test | <N> failed) <time>
        failed_tests = set(
            re.findall(
                r"\u276f\s+(\S+)\s+\(\d+\s+tests?\s*\|",
                test_log,
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
