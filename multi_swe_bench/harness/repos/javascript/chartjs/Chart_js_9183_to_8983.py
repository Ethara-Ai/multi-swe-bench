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

for kconf in karma.conf.js karma.conf.cjs karma.conf.ci.js; do
  if [ -f "$kconf" ]; then
    sed -i 's/suppressPassed: true/suppressPassed: false/' "$kconf" 2>/dev/null || true
    sed -i "s#args.browsers || 'chrome,firefox'#args.browsers || 'ChromeHeadless'#" "$kconf" 2>/dev/null || true
    sed -i "s/'progress'/'spec'/g" "$kconf" 2>/dev/null || true
    sed -i "s/browsers: \\['Chrome', 'Firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" 2>/dev/null || true
    sed -i "s/browsers: \\['Firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" 2>/dev/null || true
    sed -i "s/browsers: \\['chrome', 'firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" 2>/dev/null || true
  fi
done

export NODE_PATH=$(pwd)/node_modules
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox
export CHROMIUM_BIN=/usr/local/bin/chromium-no-sandbox

if grep -q '"packageManager".*pnpm' package.json 2>/dev/null; then
  pnpm run test-ci 2>&1 || pnpm test 2>&1 || true
elif grep -q '"test-ci"' package.json 2>/dev/null; then
  npm run test-ci 2>&1 || npm test 2>&1 || true
elif grep -q '"test"' package.json 2>/dev/null; then
  npm test -- --browsers ChromeHeadless 2>&1 || npm test 2>&1 || true
elif [ -f gulpfile.js ]; then
  ./node_modules/.bin/gulp unittest 2>&1 || ./node_modules/.bin/karma start karma.conf.js --single-run --browsers ChromeHeadless 2>&1 || ./node_modules/.bin/karma start --single-run --browsers ChromeHeadless 2>&1 || true
else
  ./node_modules/.bin/karma start --single-run --browsers ChromeHeadless 2>&1 || true
fi

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]

git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --binary /home/test.patch || true

for kconf in karma.conf.js karma.conf.cjs karma.conf.ci.js; do
  if [ -f "$kconf" ]; then
    sed -i 's/suppressPassed: true/suppressPassed: false/' "$kconf" 2>/dev/null || true
    sed -i "s#args.browsers || 'chrome,firefox'#args.browsers || 'ChromeHeadless'#" "$kconf" 2>/dev/null || true
    sed -i "s/'progress'/'spec'/g" "$kconf" 2>/dev/null || true
    sed -i "s/browsers: \\['Chrome', 'Firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" 2>/dev/null || true
    sed -i "s/browsers: \\['Firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" 2>/dev/null || true
    sed -i "s/browsers: \\['chrome', 'firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" 2>/dev/null || true
  fi
done

export NODE_PATH=$(pwd)/node_modules
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox
export CHROMIUM_BIN=/usr/local/bin/chromium-no-sandbox

if grep -q '"packageManager".*pnpm' package.json 2>/dev/null; then
  pnpm run test-ci 2>&1 || pnpm test 2>&1 || true
elif grep -q '"test-ci"' package.json 2>/dev/null; then
  npm run test-ci 2>&1 || npm test 2>&1 || true
elif grep -q '"test"' package.json 2>/dev/null; then
  npm test -- --browsers ChromeHeadless 2>&1 || npm test 2>&1 || true
elif [ -f gulpfile.js ]; then
  ./node_modules/.bin/gulp unittest 2>&1 || ./node_modules/.bin/karma start karma.conf.js --single-run --browsers ChromeHeadless 2>&1 || ./node_modules/.bin/karma start --single-run --browsers ChromeHeadless 2>&1 || true
else
  ./node_modules/.bin/karma start --single-run --browsers ChromeHeadless 2>&1 || true
fi

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]

git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --binary /home/test.patch || true
git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --binary /home/fix.patch || true

for kconf in karma.conf.js karma.conf.cjs karma.conf.ci.js; do
  if [ -f "$kconf" ]; then
    sed -i 's/suppressPassed: true/suppressPassed: false/' "$kconf" 2>/dev/null || true
    sed -i "s#args.browsers || 'chrome,firefox'#args.browsers || 'ChromeHeadless'#" "$kconf" 2>/dev/null || true
    sed -i "s/'progress'/'spec'/g" "$kconf" 2>/dev/null || true
    sed -i "s/browsers: \\['Chrome', 'Firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" 2>/dev/null || true
    sed -i "s/browsers: \\['Firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" 2>/dev/null || true
    sed -i "s/browsers: \\['chrome', 'firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" 2>/dev/null || true
  fi
done

export NODE_PATH=$(pwd)/node_modules
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox
export CHROMIUM_BIN=/usr/local/bin/chromium-no-sandbox

if grep -q '"packageManager".*pnpm' package.json 2>/dev/null; then
  pnpm run test-ci 2>&1 || pnpm test 2>&1 || true
elif grep -q '"test-ci"' package.json 2>/dev/null; then
  npm run test-ci 2>&1 || npm test 2>&1 || true
elif grep -q '"test"' package.json 2>/dev/null; then
  npm test -- --browsers ChromeHeadless 2>&1 || npm test 2>&1 || true
elif [ -f gulpfile.js ]; then
  ./node_modules/.bin/gulp unittest 2>&1 || ./node_modules/.bin/karma start karma.conf.js --single-run --browsers ChromeHeadless 2>&1 || ./node_modules/.bin/karma start --single-run --browsers ChromeHeadless 2>&1 || true
else
  ./node_modules/.bin/karma start --single-run --browsers ChromeHeadless 2>&1 || true
fi

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        base_image = "node:16-bullseye-slim" if self.pr.number < 7400 else "node:18-slim"

        dockerfile_content = """
FROM """  + base_image + """

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

# Install karma-spec-reporter and karma-chrome-launcher for per-test output
RUN npm install karma-spec-reporter karma-chrome-launcher --save-dev 2>/dev/null || true

# Fix karma config: disable suppressPassed, force spec reporter, ChromeHeadless
RUN for kconf in karma.conf.js karma.conf.cjs karma.conf.ci.js; do \\
      if [ -f "$kconf" ]; then \\
        sed -i 's/suppressPassed: true/suppressPassed: false/' "$kconf" || true; \\
        sed -i "s#args.browsers || 'chrome,firefox'#args.browsers || 'ChromeHeadless'#" "$kconf" || true; \\
        sed -i "s/'progress'/'spec'/g" "$kconf" || true; \\
        sed -i "s/browsers: \\['Chrome', 'Firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" || true; \\
        sed -i "s/browsers: \\['Firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" || true; \\
        sed -i "s/browsers: \\['chrome', 'firefox'\\]/browsers: ['ChromeHeadless']/" "$kconf" || true; \\
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        import re

        # Pattern 1: spec reporter with ANSI ✓ (green) for passed tests
        passed_pattern = re.compile(r".*\x1b\[32m✓ \x1b\[39m(.*)", re.MULTILINE)
        # Pattern 1: spec reporter with ANSI ✗ (red) for failed tests
        failed_pattern = re.compile(
            r".*\x1b\[31m✗ \x1b\[39m\x1b\[31m(.*?)\x1b\[39m", re.MULTILINE
        )

        for match in passed_pattern.findall(log):
            test_name = match.strip()
            if test_name:
                passed_tests.add(test_name)

        for match in failed_pattern.findall(log):
            test_name = match.strip()
            if test_name:
                failed_tests.add(test_name)

        # Pattern 2: spec reporter without ANSI (plain text ✓ / ✗)
        if not passed_tests and not failed_tests:
            plain_pass = re.compile(r"^\s*✓\s+(.+)", re.MULTILINE)
            plain_fail = re.compile(r"^\s*✗\s+(.+)", re.MULTILINE)
            for match in plain_pass.findall(log):
                test_name = match.strip()
                if test_name:
                    passed_tests.add(test_name)
            for match in plain_fail.findall(log):
                test_name = match.strip()
                if test_name:
                    failed_tests.add(test_name)

        # Pattern 3: Karma progress reporter - individual FAILED lines
        # Format: "Chrome Headless ... (Linux ...) <test name> FAILED"
        if not passed_tests and not failed_tests:
            karma_failed = re.compile(
                r"(?:Chrome|Firefox|Chromium)\s+Headless\s+[\d.]+\s+\([^)]+\)\s+(.+?)\s+FAILED\s*$",
                re.MULTILINE,
            )
            for match in karma_failed.findall(log):
                test_name = match.strip()
                if test_name:
                    failed_tests.add(test_name)

            # Parse the final summary line for total passed count
            # Format: "Executed X of Y (Z FAILED) (time)" or "Executed X of Y SUCCESS (time)"
            summary_pattern = re.compile(
                r"(?:Chrome|Firefox|Chromium)\s+Headless\s+[\d.]+\s+\([^)]+\):\s+Executed\s+(\d+)\s+of\s+(\d+)\s*(?:\((\d+)\s+FAILED\))?\s*(?:SUCCESS)?\s*\(",
                re.MULTILINE,
            )
            summaries = summary_pattern.findall(log)
            if summaries:
                # Take the last (final) summary line
                executed, total, failed_count_str = summaries[-1]
                executed = int(executed)
                total_tests = int(total)
                n_failed = int(failed_count_str) if failed_count_str else 0
                n_passed = executed - n_failed

                # Generate synthetic passed test names if none found individually
                if n_passed > 0 and not passed_tests:
                    for i in range(1, n_passed + 1):
                        passed_tests.add(f"test_{i}")
                # Generate synthetic failed test names if individual ones weren't captured
                if n_failed > 0 and not failed_tests:
                    for i in range(1, n_failed + 1):
                        failed_tests.add(f"failed_test_{i}")

        # Pattern 4: Jasmine dot reporter or simple "X specs, Y failures"
        if not passed_tests and not failed_tests:
            jasmine_summary = re.compile(
                r"(\d+)\s+specs?,\s+(\d+)\s+failures?", re.MULTILINE
            )
            matches = jasmine_summary.findall(log)
            if matches:
                total_specs, n_failures = matches[-1]
                total_specs = int(total_specs)
                n_failures = int(n_failures)
                n_pass = total_specs - n_failures
                for i in range(1, n_pass + 1):
                    passed_tests.add(f"test_{i}")
                for i in range(1, n_failures + 1):
                    failed_tests.add(f"failed_test_{i}")

        # Pattern 5: TOTAL line from karma "TOTAL: X FAILED, Y SUCCESS"
        if not passed_tests and not failed_tests:
            total_line = re.compile(
                r"TOTAL:\s+(\d+)\s+FAILED,\s+(\d+)\s+SUCCESS", re.MULTILINE
            )
            matches = total_line.findall(log)
            if matches:
                n_failed_total, n_passed_total = matches[-1]
                n_failed_total = int(n_failed_total)
                n_passed_total = int(n_passed_total)
                for i in range(1, n_passed_total + 1):
                    passed_tests.add(f"test_{i}")
                for i in range(1, n_failed_total + 1):
                    failed_tests.add(f"failed_test_{i}")

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
