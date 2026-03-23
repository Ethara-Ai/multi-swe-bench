import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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

    def image_prefix(self) -> str:
        return "envagent"

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

WORKDIR /home/
RUN apt-get update && apt-get install -y git

{code}

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "envagent"

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
                "prepare.sh",
                """cd /home/{repo_name}
git reset --hard
git checkout {base_sha}
###ACTION_DELIMITER###
npm install --legacy-peer-deps || true
###ACTION_DELIMITER###
npx lerna bootstrap --hoist || true
###ACTION_DELIMITER###
if grep -rq "ts-jest/utils" /home/{repo_name}/packages/*/jest.config.* 2>/dev/null; then npm install ts-jest@26 --legacy-peer-deps || true; else npm install ts-jest@27 --legacy-peer-deps || true; fi
###ACTION_DELIMITER###
npm run build || true""".format(
                    repo_name=repo_name, base_sha=self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
npx lerna run test --no-bail -- --verbose

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
python3 -c "
import re,sys
patch=open('/home/test.patch').read()
# Remove entire binary diff blocks (from 'GIT binary patch' to next 'diff --git' or end)
patch=re.sub(r'GIT binary patch.*?(?=diff --git|\\Z)', '', patch, flags=re.DOTALL)
# Remove 'Binary files ...' summary lines
patch=re.sub(r'^Binary files .*\\n', '', patch, flags=re.MULTILINE)
open('/home/test_clean.patch','w').write(patch)
" 2>/dev/null || grep -v "^Binary files" /home/test.patch > /home/test_clean.patch || true
if ! git -C /home/[[REPO_NAME]] apply --exclude='**/package-lock.json' --exclude='package-lock.json' --whitespace=nowarn --reject /home/test_clean.patch; then
    echo "Warning: git apply had issues, continuing with partial apply" >&2
fi
npx lerna run test --no-bail -- --verbose

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
python3 -c "
import re,sys
for src,dst in [('/home/test.patch','/home/test_clean.patch'),('/home/fix.patch','/home/fix_clean.patch')]:
    patch=open(src).read()
    patch=re.sub(r'GIT binary patch.*?(?=diff --git|\\Z)', '', patch, flags=re.DOTALL)
    patch=re.sub(r'^Binary files .*\\n', '', patch, flags=re.MULTILINE)
    open(dst,'w').write(patch)
" 2>/dev/null || { grep -v "^Binary files" /home/test.patch > /home/test_clean.patch || true; grep -v "^Binary files" /home/fix.patch > /home/fix_clean.patch || true; }
if ! git -C /home/[[REPO_NAME]] apply --exclude='**/package-lock.json' --exclude='package-lock.json' --whitespace=nowarn --reject /home/test_clean.patch /home/fix_clean.patch; then
    echo "Warning: git apply had issues, continuing with partial apply" >&2
fi
npx lerna bootstrap --hoist --legacy-peer-deps || true
npx lerna run test --no-bail -- --verbose

""".replace("[[REPO_NAME]]", repo_name),
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


@Instance.register("refinedev", "refine")
class REFINE(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        # Parse the log content and extract test execution results.
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped
        import re

        # Regex patterns to match test results
        success_package_pattern = re.compile(r"lerna success - (.+)")
        fail_package_pattern = re.compile(r"lerna ERR! (?!npm install).+ exited \d+ in '(.+)'")
        pass_pattern = re.compile(r"PASS\s+([^\s\(]+)")
        fail_pattern = re.compile(r"FAIL\s+([^\s\(]+)")
        # Iterate through each line to find test results
        for line in log.split("\n"):
            line = line.strip()
            # Check for passed tests
            pass_match = pass_pattern.search(line)
            if pass_match:
                test_name = pass_match.group(1)
                passed_tests.add(test_name)
            # Check for failed tests
            fail_match = fail_pattern.search(line)
            if fail_match:
                test_name = fail_match.group(1)
                failed_tests.add(test_name)
            # Check for package-level success
            success_package_match = success_package_pattern.search(line)
            if success_package_match:
                package_name = success_package_match.group(1)
                passed_tests.add(package_name)
            # Check for package-level failure
            fail_package_match = fail_package_pattern.search(line)
            if fail_package_match:
                package_name = fail_package_match.group(1)
                failed_tests.add(package_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
