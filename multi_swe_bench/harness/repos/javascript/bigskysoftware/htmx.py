import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _parse_base_version(label: str) -> tuple[int, ...]:
    """Parse the first version from a base label like 'v2.0.6..v2.0.7' → (2, 0, 6)."""
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", label)
    if match:
        return tuple(int(x) for x in match.groups())
    return (0, 0, 0)


def _is_wtr_era(label: str) -> bool:
    """Determine if the base label indicates @web/test-runner era (>= v2.0.5)."""
    return _parse_base_version(label) >= (2, 0, 5)


# ---------------------------------------------------------------------------
# Era 1: mocha-chrome (base version < v2.0.5)
#   Base image: node:22 + Chromium for headless browser testing
#   Test runner: npx mocha-chrome test/index.html
# ---------------------------------------------------------------------------


class ImageBaseMochaChrome(Image):
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
        return "node:22"

    def image_tag(self) -> str:
        return "base-mocha-chrome"

    def workdir(self) -> str:
        return "base-mocha-chrome"

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

RUN apt-get update && apt-get install -y chromium && rm -rf /var/lib/apt/lists/*

ENV CHROME_PATH=/usr/bin/chromium

{code}

{self.clear_env}

"""


# ---------------------------------------------------------------------------
# Era 2: @web/test-runner with Playwright (base version >= v2.0.5)
#   Base image: mcr.microsoft.com/playwright:v1.52.0
#   Test runner: npx web-test-runner --browsers chromium --playwright
# ---------------------------------------------------------------------------


class ImageBaseWTR(Image):
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
        return "mcr.microsoft.com/playwright:v1.52.0"

    def image_tag(self) -> str:
        return "base-wtr"

    def workdir(self) -> str:
        return "base-wtr"

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

{code}

{self.clear_env}

"""


# ---------------------------------------------------------------------------
# ImageDefault for Era 1: mocha-chrome
# ---------------------------------------------------------------------------


class ImageDefaultMochaChrome(Image):
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
        return ImageBaseMochaChrome(self.pr, self._config)

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

npm install --ignore-scripts || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
npx mocha-chrome test/index.html --chrome-flags '["--no-sandbox","--disable-gpu","--headless"]' --ignore-resource-errors --timeout 120000
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude 'dist/*' --exclude '*.png' --exclude '*.jpg' --exclude '*.jpeg' --exclude '*.gif' --exclude '*.gz' --exclude '*.mp4' --whitespace=nowarn /home/test.patch
npx mocha-chrome test/index.html --chrome-flags '["--no-sandbox","--disable-gpu","--headless"]' --ignore-resource-errors --timeout 120000

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude 'dist/*' --exclude '*.png' --exclude '*.jpg' --exclude '*.jpeg' --exclude '*.gif' --exclude '*.gz' --exclude '*.mp4' --whitespace=nowarn /home/test.patch /home/fix.patch
npx mocha-chrome test/index.html --chrome-flags '["--no-sandbox","--disable-gpu","--headless"]' --ignore-resource-errors --timeout 120000

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


# ---------------------------------------------------------------------------
# ImageDefault for Era 2: @web/test-runner
# ---------------------------------------------------------------------------


class ImageDefaultWTR(Image):
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
        return ImageBaseWTR(self.pr, self._config)

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

npm install || true
npx playwright install chromium || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
npx web-test-runner --browsers chromium --playwright
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude 'dist/*' --exclude '*.png' --exclude '*.jpg' --exclude '*.jpeg' --exclude '*.gif' --exclude '*.gz' --exclude '*.mp4' --whitespace=nowarn /home/test.patch
npx web-test-runner --browsers chromium --playwright

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude 'dist/*' --exclude '*.png' --exclude '*.jpg' --exclude '*.jpeg' --exclude '*.gif' --exclude '*.gz' --exclude '*.mp4' --whitespace=nowarn /home/test.patch /home/fix.patch
npx web-test-runner --browsers chromium --playwright

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


# ---------------------------------------------------------------------------
# Instance: routes PRs to the correct era
# ---------------------------------------------------------------------------


@Instance.register("bigskysoftware", "htmx")
class Htmx(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if _is_wtr_era(self.pr.base.label):
            return ImageDefaultWTR(self.pr, self._config)

        return ImageDefaultMochaChrome(self.pr, self._config)

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
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_re.sub("", test_log)

        if _is_wtr_era(self.pr.base.label):
            return self._parse_web_test_runner(clean_log)

        return self._parse_mocha_chrome(clean_log)

    @staticmethod
    def _parse_mocha_chrome(log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        lines = log.splitlines()
        current_suites: list[str] = []

        # ✓ (U+2713) or ✅ (U+2705) = pass, N) = fail, - = pending/skip
        pass_re = re.compile(
            r"^(\s+)[\u2713\u2705]\s+(.+?)(?:\s+\(\d+(?:ms|s)\))?\s*$"
        )
        fail_re = re.compile(r"^(\s+)(\d+)\)\s+(.+?)\s*$")
        skip_re = re.compile(r"^(\s+)-\s+(.+?)\s*$")
        suite_re = re.compile(r"^(\s{2,})([A-Za-z][\w\s\-:./]+?)\s*$")
        summary_re = re.compile(r"^\s+\d+\s+passing")

        # htmx console.log noise (htmx:event + JSON payloads) dominates the output
        noise_re = re.compile(
            r"^(?:"
            r"htmx:|"
            r"DeprecationWarning:|"
            r"\s*\{|"
            r"\s*\[|"
            r"\s*at\s|"
            r"---RUNNING TESTS---|"
            r"$"
            r")"
        )

        for line in lines:
            if summary_re.match(line):
                break

            if noise_re.match(line):
                continue

            m = pass_re.match(line)
            if m:
                indent = len(m.group(1))
                test_name = m.group(2).strip()
                full_name = _mocha_full_name(current_suites, indent, test_name)
                passed_tests.add(full_name)
                continue

            m = fail_re.match(line)
            if m:
                indent = len(m.group(1))
                test_name = m.group(3).strip()
                full_name = _mocha_full_name(current_suites, indent, test_name)
                failed_tests.add(full_name)
                continue

            m = skip_re.match(line)
            if m:
                indent = len(m.group(1))
                test_name = m.group(2).strip()
                full_name = _mocha_full_name(current_suites, indent, test_name)
                skipped_tests.add(full_name)
                continue

            m = suite_re.match(line)
            if m:
                indent = len(m.group(1))
                suite_name = m.group(2).strip()
                if len(suite_name) > 200 or "{" in suite_name or "}" in suite_name:
                    continue
                level = indent // 2
                current_suites = current_suites[:level]
                if len(current_suites) < level + 1:
                    current_suites.append(suite_name)
                else:
                    current_suites[level] = suite_name

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    # ----- Era 2: @web/test-runner output -----
    # Output format:
    #   test/path/file.js:
    #
    #    suite name [Chromium]
    #      ✓ test name
    #      ✗ test name           <-- failing
    #
    #   Chromium: |███| 48/48 test files | 834 passed, 0 failed, 5 skipped

    @staticmethod
    def _parse_web_test_runner(log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        lines = log.splitlines()
        current_file = ""
        current_suite = ""

        for line in lines:
            # File header: test/path/file.js:
            file_match = re.match(r"^(test/\S+\.js):$", line)
            if file_match:
                current_file = file_match.group(1)
                current_suite = ""
                continue

            # Suite header:  suite name [Chromium]
            suite_match = re.match(r"^\s+(.+?)\s+\[Chromium\]\s*$", line)
            if suite_match:
                current_suite = suite_match.group(1)
                continue

            # Passing test:   ✓ test name
            pass_match = re.match(r"^\s+\u2713\s+(.+?)$", line)
            if pass_match:
                test_name = pass_match.group(1).strip()
                full_name = _wtr_test_path(current_file, current_suite, test_name)
                passed_tests.add(full_name)
                continue

            # Failing test:   ✗ test name
            fail_match = re.match(r"^\s+\u2717\s+(.+?)$", line)
            if fail_match:
                test_name = fail_match.group(1).strip()
                full_name = _wtr_test_path(current_file, current_suite, test_name)
                failed_tests.add(full_name)
                continue

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


def _mocha_full_name(
    current_suites: list[str], indent: int, test_name: str
) -> str:
    level = indent // 2
    parts = current_suites[:level]
    parts.append(test_name)
    return " > ".join(parts)


def _wtr_test_path(file: str, suite: str, test: str) -> str:
    parts = [p for p in [file, suite, test] if p]
    return " > ".join(parts)
