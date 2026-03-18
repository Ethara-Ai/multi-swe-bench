import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "node:18-slim"

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
                """apt-get update
###ACTION_DELIMITER###
apt-get install -y chromium firefox-esr
###ACTION_DELIMITER###
npm install
###ACTION_DELIMITER###
sed -i 's/suppressPassed: true/suppressPassed: false/' karma.conf.js
###ACTION_DELIMITER###
echo 'npm test -- --browsers ChromeHeadless,FirefoxHeadless --verbose' > test_commands.sh
###ACTION_DELIMITER###
echo 'npm test -- --browsers ChromeHeadless,FirefoxHeadless --verbose --chromeFlags="--no-sandbox --disable-gpu" --firefoxFlags="-headless"' > test_commands.sh
###ACTION_DELIMITER###
echo 'npm test -- --browsers ChromeHeadless,FirefoxHeadless --verbose --chromeFlags="--no-sandbox --disable-gpu" --firefoxFlags="-headless"' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]

# Fix karma config: disable suppressPassed, force ChromeHeadless default
for kconf in karma.conf.js karma.conf.cjs; do
  if [ -f "$kconf" ]; then
    sed -i 's/suppressPassed: true/suppressPassed: false/' "$kconf" 2>/dev/null || true
    sed -i "s#args.browsers || 'chrome,firefox'#args.browsers || 'ChromeHeadless'#" "$kconf" 2>/dev/null || true
  fi
done

export NODE_PATH=$(pwd)/node_modules
if grep -q '"packageManager".*pnpm' package.json 2>/dev/null; then
  pnpm run test-ci 2>&1 || pnpm test 2>&1 || true
else
  npm run test-ci 2>&1 || npm test 2>&1 || true
fi

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]

# Apply test patch with --binary for PNG diffs, || true for partial applies
git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --binary /home/test.patch || true

# Fix karma config: disable suppressPassed, force ChromeHeadless default (patches may revert it)
for kconf in karma.conf.js karma.conf.cjs; do
  if [ -f "$kconf" ]; then
    sed -i 's/suppressPassed: true/suppressPassed: false/' "$kconf" 2>/dev/null || true
    sed -i "s#args.browsers || 'chrome,firefox'#args.browsers || 'ChromeHeadless'#" "$kconf" 2>/dev/null || true
  fi
done

export NODE_PATH=$(pwd)/node_modules
if grep -q '"packageManager".*pnpm' package.json 2>/dev/null; then
  pnpm run test-ci 2>&1 || pnpm test 2>&1 || true
else
  npm run test-ci 2>&1 || npm test 2>&1 || true
fi

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]

# Apply patches separately with --binary for PNG diffs, || true for partial applies
git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --binary /home/test.patch || true
git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --binary /home/fix.patch || true

# Fix karma config: disable suppressPassed, force ChromeHeadless default (patches may revert it)
for kconf in karma.conf.js karma.conf.cjs; do
  if [ -f "$kconf" ]; then
    sed -i 's/suppressPassed: true/suppressPassed: false/' "$kconf" 2>/dev/null || true
    sed -i "s#args.browsers || 'chrome,firefox'#args.browsers || 'ChromeHeadless'#" "$kconf" 2>/dev/null || true
  fi
done

export NODE_PATH=$(pwd)/node_modules
if grep -q '"packageManager".*pnpm' package.json 2>/dev/null; then
  pnpm run test-ci 2>&1 || pnpm test 2>&1 || true
else
  npm run test-ci 2>&1 || npm test 2>&1 || true
fi

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM node:18-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PUPPETEER_SKIP_DOWNLOAD=true

# Install git, browsers, and build dependencies
RUN apt-get update && apt-get install -y \\
    git \\
    ca-certificates \\
    chromium \\
    firefox-esr \\
    && rm -rf /var/lib/apt/lists/*

# Create chromium wrapper with --no-sandbox for Docker root compatibility
RUN printf '#!/bin/bash\\nexec /usr/bin/chromium --no-sandbox --disable-gpu "$@"\\n' > /usr/local/bin/chromium-no-sandbox && chmod +x /usr/local/bin/chromium-no-sandbox
ENV CHROME_BIN=/usr/local/bin/chromium-no-sandbox
ENV CHROMIUM_BIN=/usr/local/bin/chromium-no-sandbox

# Enable corepack for pnpm support (Chart.js v4+)
RUN corepack enable

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/chartjs/Chart.js.git /home/Chart.js

WORKDIR /home/Chart.js
RUN git reset --hard
RUN git checkout {pr.base.sha}

# Install dependencies (pnpm if packageManager set, otherwise npm)
RUN if grep -q '"packageManager".*pnpm' package.json 2>/dev/null; then \\
        pnpm install --no-frozen-lockfile --shamefully-hoist || true; \\
    else \\
        npm install || true; \\
    fi

# Disable suppressPassed and force ChromeHeadless in karma config
RUN for kconf in karma.conf.js karma.conf.cjs; do \\
      if [ -f "$kconf" ]; then \\
        sed -i 's/suppressPassed: true/suppressPassed: false/' "$kconf" || true; \\
        sed -i "s#args.browsers || 'chrome,firefox'#args.browsers || 'ChromeHeadless'#" "$kconf" || true; \\
      fi; \\
    done
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("chartjs", "Chart.js")
class CHART_JS_9183_TO_8983(Instance):
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

        # import json  # Not needed for this parsing logic
        # Pattern for passed tests: matches lines with ✓ (green ANSI code) followed by test name
        passed_pattern = re.compile(r".*\x1b\[32m✓ \x1b\[39m(.*)", re.MULTILINE)
        # Pattern for failed tests: matches lines with ✗ (red ANSI code) followed by test name
        failed_pattern = re.compile(
            r".*\x1b\[31m✗ \x1b\[39m\x1b\[31m(.*?)\x1b\[39m", re.MULTILINE
        )
        # Extract passed tests
        for match in passed_pattern.findall(log):
            test_name = match.strip()
            if test_name:
                passed_tests.add(test_name)
        # Extract failed tests
        for match in failed_pattern.findall(log):
            test_name = match.strip()
            if test_name:
                failed_tests.add(test_name)
        # Skipped tests: No pattern identified in provided logs, so remains empty
        # Add parsing logic here if skipped tests are found in logs
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
