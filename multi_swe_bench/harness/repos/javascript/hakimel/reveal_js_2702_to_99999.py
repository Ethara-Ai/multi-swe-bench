import re
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
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return "base-gulp"

    def workdir(self) -> str:
        return "base-gulp"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git"
                f" /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y git chromium && rm -rf /var/lib/apt/lists/*

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
        return ImageBase(self.pr, self._config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)

npm install || true

# Ensure --no-sandbox is set for running as root in Docker
if grep -q "puppeteerArgs" gulpfile.js 2>/dev/null; then
    if ! grep -q "no-sandbox" gulpfile.js 2>/dev/null; then
        sed -i "s/puppeteerArgs: \\[/puppeteerArgs: ['--no-sandbox', '--disable-setuid-sandbox', /" gulpfile.js
    fi
fi

# Upgrade node-qunit-puppeteer if bundled version < 2.1 (crashes with modern Chromium)
NQP_VER=$(node -e "try{{console.log(require('./node_modules/node-qunit-puppeteer/package.json').version)}}catch(e){{console.log('0.0.0')}}" 2>/dev/null)
NQP_MINOR=$(echo "$NQP_VER" | cut -d. -f2)
if [ "$(echo "$NQP_VER" | cut -d. -f1)" = "2" ] && [ "$NQP_MINOR" -lt 1 ] 2>/dev/null; then
    npm install node-qunit-puppeteer@2.2.1 --save-dev 2>/dev/null || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
npx gulp test 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
git apply --reject --whitespace=nowarn /home/test.patch || true
npm install || true
if grep -q "puppeteerArgs" gulpfile.js 2>/dev/null; then
    if ! grep -q "no-sandbox" gulpfile.js 2>/dev/null; then
        sed -i "s/puppeteerArgs:\\s*\\[/puppeteerArgs: ['--no-sandbox', /" gulpfile.js || true
    fi
fi
npx gulp test 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
git apply --reject --whitespace=nowarn /home/test.patch /home/fix.patch || true
npm install || true
if grep -q "puppeteerArgs" gulpfile.js 2>/dev/null; then
    if ! grep -q "no-sandbox" gulpfile.js 2>/dev/null; then
        sed -i "s/puppeteerArgs:\\s*\\[/puppeteerArgs: ['--no-sandbox', /" gulpfile.js || true
    fi
fi
npx gulp test 2>&1 || true

""".format(pr=self.pr),
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


@Instance.register("hakimel", "reveal_js_2702_to_99999")
class REVEAL_JS_2702_TO_99999(Instance):

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
        """Parse node-qunit-puppeteer test output from gulp test.

        Captured output format (verified in Docker):
          ✔ test/test-state.html [6/6] in 32ms
          ✔ test/test.html [158/158] in 450ms
          ! test/test-foo.html [5/8] in 120ms
          ✔ Passed 293 tests
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

        # ✔ test/test-state.html [6/6] in 32ms
        re_file_pass = re.compile(
            r"^[✔✓]\s+(.+?)\s+\[(\d+)/(\d+)\]\s+in\s+\d+ms$"
        )
        # ! test/test-foo.html [5/8] in 120ms
        re_file_fail = re.compile(
            r"^[!✗✕]\s+(.+?)\s+\[(\d+)/(\d+)\]\s+in\s+\d+ms$"
        )

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            m = re_file_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_file_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

        for test in failed_tests:
            passed_tests.discard(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
