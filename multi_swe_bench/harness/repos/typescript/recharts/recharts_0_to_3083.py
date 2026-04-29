
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RechartsKarmaImageBase(Image):
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
        return "node:14"

    def image_tag(self) -> str:
        return "base-karma"

    def workdir(self) -> str:
        return "base-karma"

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

# Fix Debian Buster EOL repos (node:14 is based on Buster)
RUN sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g' /etc/apt/sources.list && \
    sed -i 's|http://deb.debian.org/debian-security|http://archive.debian.org/debian-security|g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install -y --no-install-recommends git chromium

ENV CHROME_BIN=/usr/bin/chromium
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV CHROMIUM_FLAGS="--no-sandbox"

{code}

{self.clear_env}

"""


class RechartsKarmaImageDefault(Image):
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
        return RechartsKarmaImageBase(self.pr, self.config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

if [ -f yarn.lock ]; then
    npm install -g yarn || true
    yarn install || true
else
    npm install || true
fi

npm install karma-chrome-launcher@latest || true
npm install karma-mocha-reporter || true

sed -i "s/'karma-coveralls',/'karma-coveralls','karma-mocha-reporter',/" test/karma.conf.js || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CHROME_BIN=/usr/bin/chromium
export CHROMIUM_FLAGS="--no-sandbox"
npx cross-env NODE_ENV=test karma start test/karma.conf.js --single-run --browsers ChromeHeadless --reporters mocha

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude yarn.lock --whitespace=nowarn /home/test.patch || git apply --exclude package-lock.json --exclude package.json --exclude yarn.lock --whitespace=nowarn /home/test.patch
if [ -f yarn.lock ]; then
    yarn install || true
else
    npm install || true
fi
npm install karma-chrome-launcher@latest || true
npm install karma-mocha-reporter || true
sed -i "s/'karma-coveralls',/'karma-coveralls','karma-mocha-reporter',/" test/karma.conf.js || true
export CHROME_BIN=/usr/bin/chromium
export CHROMIUM_FLAGS="--no-sandbox"
npx cross-env NODE_ENV=test karma start test/karma.conf.js --single-run --browsers ChromeHeadless --reporters mocha

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude yarn.lock --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --exclude package-lock.json --exclude package.json --exclude yarn.lock --whitespace=nowarn /home/test.patch /home/fix.patch
if [ -f yarn.lock ]; then
    yarn install || true
else
    npm install || true
fi
npm install karma-chrome-launcher@latest || true
npm install karma-mocha-reporter || true
sed -i "s/'karma-coveralls',/'karma-coveralls','karma-mocha-reporter',/" test/karma.conf.js || true
export CHROME_BIN=/usr/bin/chromium
export CHROMIUM_FLAGS="--no-sandbox"
npx cross-env NODE_ENV=test karma start test/karma.conf.js --single-run --browsers ChromeHeadless --reporters mocha

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


@Instance.register("recharts", "recharts_0_to_3083")
class RECHARTS_0_TO_3083(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RechartsKarmaImageDefault(self.pr, self._config)

    _KARMA_REPORTER_ENSURE = (
        "npm install karma-mocha-reporter 2>/dev/null || true; "
        "sed -i \"s/\\x27karma-coveralls\\x27,/\\x27karma-coveralls\\x27,\\x27karma-mocha-reporter\\x27,/\" test/karma.conf.js 2>/dev/null || true"
    )

    def _test_cmd(self) -> str:
        return 'CHROME_BIN=/usr/bin/chromium CHROMIUM_FLAGS="--no-sandbox" npx cross-env NODE_ENV=test karma start test/karma.conf.js --single-run --browsers ChromeHeadless --reporters mocha'

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        re_test = re.compile(r"^(\s*)[✔✓✖✗]\s+(.+?)(?:\s+\(\d+.*\))?$")
        re_summary = re.compile(r"^\d+\s+tests?\s+completed")

        describe_stack: list[tuple[int, str]] = []

        for line in test_log.splitlines():
            clean = ansi_re.sub("", line).rstrip()
            if not clean.strip():
                continue

            stripped = clean.lstrip()
            indent = len(clean) - len(stripped)

            test_match = re_test.match(clean)
            if test_match:
                test_indent = len(test_match.group(1))
                test_name = test_match.group(2).strip()
                is_pass = stripped[0] in "✔✓"

                if re_summary.match(test_name):
                    continue

                while describe_stack and describe_stack[-1][0] >= test_indent:
                    describe_stack.pop()

                context = " > ".join(d[1] for d in describe_stack)
                full_name = f"{context} > {test_name}" if context else test_name

                if is_pass:
                    if full_name not in failed_tests:
                        passed_tests.add(full_name)
                else:
                    failed_tests.add(full_name)
                    passed_tests.discard(full_name)
            else:
                if indent < 2:
                    continue
                if stripped.startswith(("ERROR", "WARN", "in ", "at ", "</", "npm ")):
                    continue
                if stripped.startswith("<") and ("=" in stripped or "{" in stripped):
                    continue
                # Skip wrapped console output (quoted strings, pure numbers, long messages)
                if stripped.startswith(("'", '"')) or re.match(r"^\d+(\.\d+)?$", stripped):
                    continue

                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                describe_stack.append((indent, stripped))

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
