import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ComponentsImageBase(Image):
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
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends build-essential python3 && rm -rf /var/lib/apt/lists/*
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash && export NVM_DIR="$HOME/.nvm" && [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" && nvm install 16

{code}

{self.clear_env}

"""


class ComponentsImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return ComponentsImageBase(self.pr, self.config)

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use 16

cd /home/[[REPO_NAME]]
git reset --hard
git clean -fd
git checkout {pr.base.sha}
git clean -fd
bash /home/check_git_changes.sh

npm install --legacy-peer-deps --ignore-scripts
npm install --legacy-peer-deps --ignore-scripts cheerio@1.0.0-rc.12
rm -rf node_modules/@types/glob node_modules/@types/minimatch node_modules/@types/react-datepicker/node_modules/@types/react
git checkout -- package.json
""".replace("[[REPO_NAME]]", repo_name).format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use 16

cd /home/[[REPO_NAME]]
node -e "var p=require('./package.json');p.jest=p.jest||{};p.jest.globals={...p.jest.globals,'ts-jest':{diagnostics:false}};require('fs').writeFileSync('package.json',JSON.stringify(p,null,2)+'\\n')"
CI=true npx tsdx test --env=jsdom --verbose 2>&1 || true
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use 16

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch
npm install --legacy-peer-deps --ignore-scripts 2>/dev/null || true
npm install --legacy-peer-deps --ignore-scripts cheerio@1.0.0-rc.12 2>/dev/null || true
rm -rf node_modules/@types/glob node_modules/@types/minimatch node_modules/@types/react-datepicker/node_modules/@types/react
node -e "var p=require('./package.json');p.jest=p.jest||{};p.jest.globals={...p.jest.globals,'ts-jest':{diagnostics:false}};require('fs').writeFileSync('package.json',JSON.stringify(p,null,2)+'\\n')"
CI=true npx tsdx test --env=jsdom --verbose 2>&1 || true
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use 16

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm install --legacy-peer-deps --ignore-scripts 2>/dev/null || true
npm install --legacy-peer-deps --ignore-scripts cheerio@1.0.0-rc.12 2>/dev/null || true
rm -rf node_modules/@types/glob node_modules/@types/minimatch node_modules/@types/react-datepicker/node_modules/@types/react
node -e "var p=require('./package.json');p.jest=p.jest||{};p.jest.globals={...p.jest.globals,'ts-jest':{diagnostics:false}};require('fs').writeFileSync('package.json',JSON.stringify(p,null,2)+'\\n')"
CI=true npx tsdx test --env=jsdom --verbose 2>&1 || true
""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        name = self.dependency().image_name()
        tag = self.dependency().image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("HospitalRun", "components")
class HospitalRunComponents(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ComponentsImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r'\x1b\[[0-9;]*m')

        lines = log.splitlines()
        hierarchy = []
        for line in lines:
            line = ansi_escape.sub('', line)
            stripped = line.strip()
            if not stripped:
                continue

            leading_spaces = len(line) - len(line.lstrip(" "))
            current_level = leading_spaces // 2

            pass_match = re.match(
                r'^[✓✔]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$', stripped
            )
            fail_match = re.match(
                r'^[✕✗✘×]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$', stripped
            )
            skip_match = re.match(
                r'^○\s+(?:skipped\s+)?(.*?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$',
                stripped,
            )

            if pass_match or fail_match or skip_match:
                if pass_match:
                    test_name = pass_match.group(1)
                elif fail_match:
                    test_name = fail_match.group(1)
                else:
                    test_name = skip_match.group(1)

                full_name = ' '.join(hierarchy + [test_name])

                if pass_match:
                    passed_tests.add(full_name)
                elif fail_match:
                    failed_tests.add(full_name)
                else:
                    skipped_tests.add(full_name)
            else:
                group_name = stripped.rstrip()
                if current_level < len(hierarchy):
                    hierarchy = hierarchy[:current_level]
                    hierarchy.append(group_name)
                elif current_level == len(hierarchy):
                    if hierarchy:
                        hierarchy[-1] = group_name
                    else:
                        hierarchy.append(group_name)
                else:
                    while len(hierarchy) < current_level:
                        hierarchy.append("")
                    hierarchy.append(group_name)

        # Post-processing: remove overlaps (TestResult validates no overlap)
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
