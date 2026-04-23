from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class webpackCliImageBase(Image):
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
        return "node:16"

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
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends build-essential python3 && \
    rm -rf /var/lib/apt/lists/*

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash && \\
    export NVM_DIR="$HOME/.nvm" && \\
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

{code}

{self.clear_env}

"""


class webpackCliImageDefault(Image):
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
        return webpackCliImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _is_era1(self) -> bool:
        """ERA 1: PR #1196 only — uses npm + package-lock.json + lerna bootstrap."""
        return self.pr.number <= 1196

    def files(self) -> list[File]:
        if self._is_era1():
            prepare_script = """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install 14 || true
nvm use 14 || true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install
npx lerna@3 bootstrap
npx tsc
""".format(pr=self.pr)

            run_script = """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install 14 || true
nvm use 14 || true

cd /home/{pr.repo}
npx jest --reporters=default || true
""".format(pr=self.pr)

            test_run_script = """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install 14 || true
nvm use 14 || true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npm install
npx lerna@3 bootstrap
npx tsc
npx jest --reporters=default || true
""".format(pr=self.pr)

            fix_run_script = """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install 14 || true
nvm use 14 || true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm install
npx lerna@3 bootstrap
npx tsc
npx jest --reporters=default || true
""".format(pr=self.pr)

        else:
            # ERA 2: PRs #1276–#2381 — yarn + yarn.lock + lerna bootstrap
            prepare_script = """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install 14 || true
nvm use 14 || true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

yarn install --ignore-engines --network-timeout 600000 || yarn install --ignore-engines --network-timeout 600000
npx lerna@3 bootstrap
yarn build
""".format(pr=self.pr)

            run_script = """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install 14 || true
nvm use 14 || true

cd /home/{pr.repo}
npx jest --reporters=default || true
""".format(pr=self.pr)

            test_run_script = """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install 14 || true
nvm use 14 || true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
yarn install --ignore-engines --network-timeout 600000 || yarn install --ignore-engines --network-timeout 600000
npx lerna@3 bootstrap
yarn build
npx jest --reporters=default || true
""".format(pr=self.pr)

            fix_run_script = """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install 14 || true
nvm use 14 || true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
yarn install --ignore-engines --network-timeout 600000 || yarn install --ignore-engines --network-timeout 600000
npx lerna@3 bootstrap
yarn build
npx jest --reporters=default || true
""".format(pr=self.pr)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
            File(".", "prepare.sh", prepare_script),
            File(".", "run.sh", run_script),
            File(".", "test-run.sh", test_run_script),
            File(".", "fix-run.sh", fix_run_script),
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


@Instance.register("webpack", "webpack-cli")
class webpackCliInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return webpackCliImageDefault(self.pr, self._config)

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

        passed_res = [
            re.compile(r"PASS:?\s+([^\(]+)"),
            re.compile(
                r"\s*[\u2714\u2713\u2705]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
            ),
        ]
        failed_res = [
            re.compile(r"FAIL:?\s+([^\(]+)"),
            re.compile(
                r"\s*[\u00d7\u2717\u2718]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
            ),
            re.compile(
                r"^(?!\s*\(node:)\s*\d+\)\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
            ),
        ]
        skipped_res = [
            re.compile(r"SKIP:?\s+([^\(]+)"),
        ]

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            for passed_re in passed_res:
                m = passed_re.search(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))
            for failed_re in failed_res:
                m = failed_re.search(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))
            for skipped_re in skipped_res:
                m = skipped_re.search(line)
                if m:
                    skipped_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
