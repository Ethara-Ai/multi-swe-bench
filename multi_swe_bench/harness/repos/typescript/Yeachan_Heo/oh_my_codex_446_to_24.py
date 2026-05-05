import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# oh-my-codex Era 1 (PR #24 - #446)
# ---------------------------------------------------------------------------
# deps: @modelcontextprotocol/sdk
# devDeps: @types/node, typescript
# test: npm run build && node --test dist/**/*.test.js
# build: tsc
# ---------------------------------------------------------------------------


class ImageBase446to24(Image):
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
        return "node:22"

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

        global_env = self.global_env
        global_block = f"{global_env}\n" if global_env else ""

        clear = self.clear_env
        clear_block = f"\n{clear}" if clear else ""

        dockerfile_content = (
            f"FROM {image_name}\n"
            f"{global_block}"
            f"WORKDIR /home/\n"
            f"RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*\n"
            f"\n"
            f"{code}\n"
            f"{clear_block}\n"
        )
        return re.sub(r'\n{3,}', '\n\n', dockerfile_content)


class ImageDefault446to24(Image):
    """Per-PR image for Era 1 (PR #24 - #446).
    deps: @modelcontextprotocol/sdk
    devDeps: @types/node, typescript
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
        return ImageBase446to24(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_cmd(self) -> str:
        return "npm test"

    def files(self) -> list[File]:
        test_cmd = self._test_cmd()
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
npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{test_cmd}

""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}

""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}

""".format(repo=self.pr.repo, test_cmd=test_cmd),
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

        global_env = self.global_env
        global_block = f"{global_env}\n" if global_env else ""

        clear = self.clear_env
        clear_block = f"\n{clear}" if clear else ""

        dockerfile_content = (
            f"FROM {name}:{tag}\n"
            f"{global_block}"
            f"{copy_commands}"
            f"\n"
            f"{prepare_commands}\n"
            f"{clear_block}\n"
        )
        return re.sub(r'\n{3,}', '\n\n', dockerfile_content)


@Instance.register("Yeachan-Heo", "oh_my_codex_446_to_24")
class OhMyCodex446to24(Instance):
    """Era 1: PR #24 - #446.
    Minimal deps: only @modelcontextprotocol/sdk + typescript + @types/node.
    test: npm run build && node --test dist/**/*.test.js
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault446to24(self.pr, self._config)

    def _test_cmd(self) -> str:
        return "npm test"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return f"bash -c 'cd /home/{self.pr.repo}; {self._test_cmd()}'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return f"bash -c 'cd /home/{self.pr.repo}; git apply --whitespace=nowarn /home/test.patch; {self._test_cmd()}'"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return f"bash -c 'cd /home/{self.pr.repo}; git apply --whitespace=nowarn /home/test.patch /home/fix.patch; {self._test_cmd()}'"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        re_pass = re.compile(r"^✔\s+(.+?)(?:\s+\(.+\))?\s*$")
        re_fail = re.compile(r"^✖\s+(.+?)(?:\s+\(.+\))?\s*$")
        re_tap_ok = re.compile(r"^ok\s+\d+\s+-?\s*(.+)$")
        re_tap_not_ok = re.compile(r"^not ok\s+\d+\s+-?\s*(.+)$")

        for line in test_log.splitlines():
            clean = ansi_re.sub("", line).strip()
            if not clean:
                continue

            m = re_pass.match(clean)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_fail.match(clean)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_tap_ok.match(clean)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_tap_not_ok.match(clean)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
