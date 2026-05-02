import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:10-buster"
_INTERVAL_NAME = "nuxt_2736_to_147"


class ImageBase(Image):
    """Base image for Nuxt v0.9–v1.x era (ava test runner, npm/yarn)."""

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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base-{name}".format(name=_INTERVAL_NAME)

    def workdir(self) -> str:
        return "base-{name}".format(name=_INTERVAL_NAME)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = "RUN git clone https://github.com/{org}/{repo}.git /home/{repo}".format(
                org=self.pr.org, repo=self.pr.repo
            )
        else:
            code = "COPY {repo} /home/{repo}".format(repo=self.pr.repo)

        return """FROM {image_name}

{global_env}

WORKDIR /home/

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


class ImageDefault(Image):
    """Per-PR image for Nuxt v0.9–v1.x era."""

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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def _test_files(self) -> list[str]:
        files = []
        for m in re.findall(r"diff --git a/(\S+)", self.pr.test_patch):
            if ".test." in m or ".spec." in m:
                files.append(m)
        return files

    def files(self) -> list[File]:
        test_files = self._test_files()
        test_files_str = " ".join(test_files)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(
                    repo=self.pr.repo, base_sha=self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                _RUN_TESTS_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH.format(repo=self.pr.repo, test_files=test_files_str),
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH.format(repo=self.pr.repo, test_files=test_files_str),
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH.format(repo=self.pr.repo, test_files=test_files_str),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        return """FROM {name}:{tag}

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

{clear_env}

""".format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            copy_commands=copy_commands,
            clear_env=self.clear_env,
        )


@Instance.register("nuxt", _INTERVAL_NAME)
class NuxtAva(Instance):
    """Nuxt v0.9–v1.x: ava test runner, npm/yarn."""

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Ava output patterns:
        #   ✔ test name
        #   ✖ test name
        #   - test name (skipped)
        re_pass = re.compile(r"^\s*[✔✓]\s+(.+?)$")
        re_fail = re.compile(r"^\s*[✖✗×]\s+(.+?)$")
        re_skip = re.compile(r"^\s*[-]\s+(.+?)$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

            m = re_skip.match(line)
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


# ---------------------------------------------------------------------------
# Shell script templates
# ---------------------------------------------------------------------------

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

_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install with yarn if yarn.lock present, else npm
if [ -f yarn.lock ]; then
    yarn install --ignore-engines || true
else
    npm install || true
fi

# Build (tests import from lib/)
npm run build || true

"""

_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
TEST_FILES="$@"

# Run ava with the test files
if [ -n "$TEST_FILES" ]; then
    npx ava --verbose --serial $TEST_FILES 2>&1 || true
else
    npx ava --verbose --serial test/ 2>&1 || true
fi
"""

_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
bash /home/run_tests.sh {test_files}
"""

_TEST_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true

# Reinstall if package.json changed, then build
if [ -f yarn.lock ]; then
    yarn install --ignore-engines || true
else
    npm install || true
fi
npm run build || true

bash /home/run_tests.sh {test_files}
"""

_FIX_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch || true; git apply --whitespace=nowarn --reject /home/fix.patch || true; }}

# Reinstall if package.json changed, then build
if [ -f yarn.lock ]; then
    yarn install --ignore-engines || true
else
    npm install || true
fi
npm run build || true

bash /home/run_tests.sh {test_files}
"""
