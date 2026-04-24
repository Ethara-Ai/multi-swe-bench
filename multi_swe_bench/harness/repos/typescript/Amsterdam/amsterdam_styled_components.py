import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO = "amsterdam-styled-components"

# Era boundary: PRs 271-573 use yarn + Node 12, PR 1923+ uses npm + Node 16
_ERA_BOUNDARY = 573


class ImageBase12(Image):
    """Base image for era 1 (PRs 271-573): Node 12 + yarn + lerna."""

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
        return "node:12-buster"

    def image_tag(self) -> str:
        return "base12"

    def workdir(self) -> str:
        return "base12"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{_REPO}.git /home/{_REPO}"
        else:
            code = f"COPY {_REPO} /home/{_REPO}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \\
    sed -i '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y git
RUN npm install -g --force yarn@1.22.18 lerna@3.22.1

{code}

{self.clear_env}

"""


class ImageBase16(Image):
    """Base image for era 2 (PR 1923+): Node 16 + npm workspaces + lerna."""

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
        return "node:16-bullseye"

    def image_tag(self) -> str:
        return "base16"

    def workdir(self) -> str:
        return "base16"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{_REPO}.git /home/{_REPO}"
        else:
            code = f"COPY {_REPO} /home/{_REPO}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y git
RUN npm install -g lerna@4.0.0

{code}

{self.clear_env}

"""


class ImageDefault12(Image):
    """Default image for era 1: yarn install + build at base commit."""

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
        return ImageBase12(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh
yarn install --frozen-lockfile || true

""".format(repo=_REPO, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
yarn jest --verbose

""".format(repo=_REPO),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch
yarn jest --verbose

""".format(repo=_REPO),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch /home/fix.patch
yarn jest --verbose

""".format(repo=_REPO),
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


class ImageDefault16(Image):
    """Default image for era 2: npm install + build at base commit."""

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
        return ImageBase16(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh
npm install || true
npm run build || true

""".format(repo=_REPO, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
npx jest --verbose

""".format(repo=_REPO),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm run build 2>/dev/null || true
npx jest --verbose

""".format(repo=_REPO),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm run build 2>/dev/null || true
npx jest --verbose

""".format(repo=_REPO),
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


@Instance.register("Amsterdam", "amsterdam-styled-components")
class AmsterdamStyledComponents(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= _ERA_BOUNDARY:
            return ImageDefault12(self.pr, self._config)
        return ImageDefault16(self.pr, self._config)

    def _test_cmd(self) -> str:
        if self.pr.number <= _ERA_BOUNDARY:
            return "yarn jest --verbose"
        return "npm run build 2>/dev/null || true; npx jest --verbose"

    def _lockfile_exclude(self) -> str:
        if self.pr.number <= _ERA_BOUNDARY:
            return "yarn.lock"
        return "package-lock.json"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return f"bash -c 'cd /home/{_REPO}; {self._test_cmd()}'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return f"bash -c 'cd /home/{_REPO}; git apply --exclude {self._lockfile_exclude()} --whitespace=nowarn /home/test.patch; {self._test_cmd()}'"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return f"bash -c 'cd /home/{_REPO}; git apply --exclude {self._lockfile_exclude()} --whitespace=nowarn /home/test.patch /home/fix.patch; {self._test_cmd()}'"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes for reliable matching
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        # Jest file-level output:
        #   PASS packages/asc-ui/src/components/Button/Button.test.tsx
        #   FAIL packages/asc-ui/src/components/Icon/Icon.test.tsx
        re_pass = re.compile(r"^PASS\s+(.+\.(?:test|spec)\.tsx?)$")
        re_fail = re.compile(r"^FAIL\s+(.+\.(?:test|spec)\.tsx?)$")

        for line in test_log.splitlines():
            clean = ansi_re.sub("", line).strip()
            if not clean:
                continue

            pass_match = re_pass.match(clean)
            if pass_match:
                passed_tests.add(pass_match.group(1))
                continue

            fail_match = re_fail.match(clean)
            if fail_match:
                failed_tests.add(fail_match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
