import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "hono"


class ImageBase1401To13(Image):
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

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base_node18_jest"

    def workdir(self) -> str:
        return "base_node18_jest"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = "RUN git clone https://github.com/{org}/{repo}.git /home/{repo_dir}".format(
                org=self.pr.org, repo=self.pr.repo, repo_dir=REPO_DIR
            )
        else:
            code = "COPY {repo} /home/{repo_dir}".format(
                repo=self.pr.repo, repo_dir=REPO_DIR
            )

        return """FROM {image_name}

{global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g yarn --force

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


class ImageDefault1401To13(Image):
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
        return ImageBase1401To13(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

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

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

yarn install || true
yarn build 2>/dev/null || true

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}
npx jest --verbose 2>&1
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch || git apply --exclude yarn.lock --whitespace=nowarn --3way /home/test.patch || true
yarn build 2>/dev/null || true
npx jest --verbose 2>&1
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --exclude yarn.lock --whitespace=nowarn --3way /home/test.patch /home/fix.patch || true
yarn build 2>/dev/null || true
npx jest --verbose 2>&1
""".format(repo_dir=REPO_DIR),
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


@Instance.register("honojs", "hono_1401_to_13")
class Hono1401To13(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault1401To13(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        re_pass = re.compile(r"[^a-zA-Z]*PASS[^a-zA-Z]*\s+(.+?)\s*(?=\n|$)")
        re_fail = re.compile(r"[^a-zA-Z]*FAIL[^a-zA-Z]*\s+(.+?)\s*(?=\n|$)")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line)
            if not line.strip():
                continue

            pass_match = re_pass.match(line)
            if pass_match:
                test_name = pass_match.group(1).strip()
                if test_name:
                    passed_tests.add(test_name)
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                test_name = fail_match.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)

        passed_tests = passed_tests - failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
