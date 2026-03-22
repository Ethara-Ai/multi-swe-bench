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
        return "node:20"

    def image_tag(self) -> str:
        return "base-grunt"

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
RUN apt update && apt install -y git python3 jq chromium

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
                "karma-wrapper.conf.js",
                """module.exports = function(config) {
  require('./karma.conf.js')(config);
  config.set({
    browsers: ['ChromeHeadlessNoSandbox'],
    customLaunchers: {
      ChromeHeadlessNoSandbox: {
        base: 'ChromeHeadless',
        flags: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']
      }
    },
    singleRun: true
  });
};
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true
export TRAVIS=true
export CHROME_BIN=/usr/bin/chromium
export PATH=./node_modules/.bin:$PATH

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install --ignore-scripts || true
npm rebuild || true

npm install -g grunt-cli || true
npm install sass node-sass proxyquireify || true

cp /home/karma-wrapper.conf.js /home/{pr.repo}/test/karma-wrapper.conf.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export CI=true
export TRAVIS=true
export CHROME_BIN=/usr/bin/chromium
export PATH=./node_modules/.bin:$PATH

cd /home/{pr.repo}
grunt build --force || true
grunt shell:noderequire shell:browserify shell:webpack --force || true
npx karma start test/karma-wrapper.conf.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export CI=true
export TRAVIS=true
export CHROME_BIN=/usr/bin/chromium
export PATH=./node_modules/.bin:$PATH

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm install --ignore-scripts || true
npm rebuild || true
npm install sass node-sass proxyquireify || true
cp /home/karma-wrapper.conf.js /home/{pr.repo}/test/karma-wrapper.conf.js
grunt build --force || true
grunt shell:noderequire shell:browserify shell:webpack --force || true
npx karma start test/karma-wrapper.conf.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export CI=true
export TRAVIS=true
export CHROME_BIN=/usr/bin/chromium
export PATH=./node_modules/.bin:$PATH

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm install --ignore-scripts || true
npm rebuild || true
npm install sass node-sass proxyquireify || true
cp /home/karma-wrapper.conf.js /home/{pr.repo}/test/karma-wrapper.conf.js
grunt build --force || true
grunt shell:noderequire shell:browserify shell:webpack --force || true
npx karma start test/karma-wrapper.conf.js

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


@Instance.register("videojs", "video_js_0_to_5599")
class VideoJsGrunt(Instance):
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

        passed_count = 0
        failed_count = 0
        skipped_count = 0

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        # Karma dots summary: "Executed N of M (skipped K) SUCCESS" or "Executed N of M (K FAILED) ... FAILED"
        re_success = re.compile(
            r"Executed\s+(\d+)\s+of\s+(\d+)\s*"
            r"(?:\(skipped\s+(\d+)\)\s*)?"
            r"SUCCESS"
        )
        re_failed = re.compile(
            r"Executed\s+(\d+)\s+of\s+(\d+)\s*"
            r"\((\d+)\s+FAILED\)"
            r"(?:\s*\(skipped\s+(\d+)\))?"
        )

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            success_match = re_success.search(line)
            if success_match:
                executed = int(success_match.group(1))
                skipped = int(success_match.group(3) or 0)
                passed_count = executed
                failed_count = 0
                skipped_count = skipped
                continue

            failed_match = re_failed.search(line)
            if failed_match:
                executed = int(failed_match.group(1))
                num_failed = int(failed_match.group(3))
                skipped = int(failed_match.group(4) or 0)
                passed_count = executed - num_failed
                failed_count = num_failed
                skipped_count = skipped
                continue

        for i in range(passed_count):
            passed_tests.add(f"test_{i + 1}")
        for i in range(failed_count):
            failed_tests.add(f"failed_test_{i + 1}")
        for i in range(skipped_count):
            skipped_tests.add(f"skipped_test_{i + 1}")

        return TestResult(
            passed_count=passed_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
