import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBaseNpmModern(Image):
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
        return "node:18-bullseye"

    def image_tag(self) -> str:
        return "base-npm-modern"

    def workdir(self) -> str:
        return "base-npm-modern"

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
    git python3 build-essential && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


INSTALL_DEPS = """
npm install --legacy-peer-deps || npm install || true
npm install --save-dev @testing-library/jest-dom @testing-library/react @testing-library/user-event || true
for pkg in react-dev-utils react-scripts react-error-overlay; do
    if [ -d "packages/$pkg" ]; then
        cd packages/$pkg && npm install --legacy-peer-deps 2>/dev/null || true && cd /home/{pr.repo}
    fi
done
"""


class ImageDefaultNpmModern(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return ImageBaseNpmModern(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        install = INSTALL_DEPS.format(pr=self.pr)
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
""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{install}

""".format(pr=self.pr, install=install),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
export CI=true

cd /home/{pr.repo}
npm test || true


TEST_FILES=$(cat /home/test.patch 2>/dev/null | grep "^diff --git" | sed "s|diff --git a/||" | sed "s| b/.*||" | grep -E "\.(test|spec)\.(js|ts|jsx|tsx)$" || true)
if [ -n "$TEST_FILES" ]; then
    JEST=""
    for j in ./node_modules/.bin/jest ./packages/react-scripts/node_modules/.bin/jest; do
        if [ -x "$j" ]; then JEST="$j"; break; fi
    done
    if [ -n "$JEST" ]; then
        $JEST --no-coverage --forceExit --env=jsdom $TEST_FILES 2>&1 || true
    fi
fi


""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export CI=true

cd /home/{pr.repo}
git clean -fd 2>/dev/null || true
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch || true
{install}
npm test || true


TEST_FILES=$(cat /home/test.patch 2>/dev/null | grep "^diff --git" | sed "s|diff --git a/||" | sed "s| b/.*||" | grep -E "\.(test|spec)\.(js|ts|jsx|tsx)$" || true)
if [ -n "$TEST_FILES" ]; then
    JEST=""
    for j in ./node_modules/.bin/jest ./packages/react-scripts/node_modules/.bin/jest; do
        if [ -x "$j" ]; then JEST="$j"; break; fi
    done
    if [ -n "$JEST" ]; then
        $JEST --no-coverage --forceExit --env=jsdom $TEST_FILES 2>&1 || true
    fi
fi


""".format(pr=self.pr, install=install),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export CI=true

cd /home/{pr.repo}
git clean -fd 2>/dev/null || true
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch || true
{install}
npm test || true


TEST_FILES=$(cat /home/test.patch 2>/dev/null | grep "^diff --git" | sed "s|diff --git a/||" | sed "s| b/.*||" | grep -E "\.(test|spec)\.(js|ts|jsx|tsx)$" || true)
if [ -n "$TEST_FILES" ]; then
    JEST=""
    for j in ./node_modules/.bin/jest ./packages/react-scripts/node_modules/.bin/jest; do
        if [ -x "$j" ]; then JEST="$j"; break; fi
    done
    if [ -n "$JEST" ]; then
        $JEST --no-coverage --forceExit --env=jsdom $TEST_FILES 2>&1 || true
    fi
fi


""".format(pr=self.pr, install=install),
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

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("facebook", "create_react_app_13738_to_99999")
class CREATE_REACT_APP_13738_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefaultNpmModern(self.pr, self._config)

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
        re_pass_suite = re.compile(r"^PASS\s+(.+)$")
        re_fail_suite = re.compile(r"^FAIL\s+(.+)$")
        re_pass_test = re.compile(r"^\s*[✔✓]\s+(.*?)(?:\s+\(\d+\s*m?s\))?$")
        re_fail_test = re.compile(r"^\s*[✕×✗]\s+(.+)$")
        re_skip_test = re.compile(r"^\s*[○◌]\s+(?:skipped\s+)?(.+)$")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue
            m = re_pass_suite.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue
            m = re_fail_suite.match(line)
            if m:
                test_name = m.group(1).strip()
                failed_tests.add(test_name)
                passed_tests.discard(test_name)
                continue
            m = re_pass_test.match(line)
            if m and m.group(1).strip() not in failed_tests:
                passed_tests.add(m.group(1).strip())
                continue
            m = re_fail_test.match(line)
            if m:
                test_name = m.group(1).strip()
                failed_tests.add(test_name)
                passed_tests.discard(test_name)
                continue
            m = re_skip_test.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
