import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Base image for PRs #658-#1101 (Node 18, Oct 2023 - Oct 2024)."""

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

RUN apt-get update && apt-get install -y \\
    git \\
    xvfb \\
    libgtk-3-0 \\
    libgbm-dev \\
    libnotify-dev \\
    libnss3 \\
    libxss1 \\
    libasound2 \\
    libxtst6 \\
    xauth \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class ImageBase22(Image):
    """Base image for PR #1264+ (Node 22, Oct 2025+)."""

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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base22"

    def workdir(self) -> str:
        return "base22"

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

RUN apt-get update && apt-get install -y \\
    git \\
    xvfb \\
    libgtk-3-0 \\
    libgbm-dev \\
    libnotify-dev \\
    libnss3 \\
    libxss1 \\
    libasound2 \\
    libxtst6 \\
    xauth \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class ImageDefault(Image):
    """Per-PR image: checks out base commit, installs deps, builds frontend, copies scripts."""

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
        if self.pr.number >= 1264:
            return ImageBase22(self.pr, self._config)
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

npm install || true
npm run build || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}

echo "=== LINT ==="
npm run lint 2>&1 || true

echo "=== UNIT TESTS ==="
npm run test:unit 2>&1 || true

echo "=== E2E TESTS ==="
npm start &
SERVER_PID=$!
sleep 20
VITE_API_URL=http://localhost:1337 xvfb-run --auto-servernum npx cypress run 2>&1 || true
kill $SERVER_PID 2>/dev/null || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch

echo "=== LINT ==="
npm run lint 2>&1 || true

echo "=== UNIT TESTS ==="
npm run test:unit 2>&1 || true

echo "=== E2E TESTS ==="
npm run build 2>&1 || true
npm start &
SERVER_PID=$!
sleep 20
VITE_API_URL=http://localhost:1337 xvfb-run --auto-servernum npx cypress run 2>&1 || true
kill $SERVER_PID 2>/dev/null || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch

echo "=== LINT ==="
npm run lint 2>&1 || true

echo "=== UNIT TESTS ==="
npm run test:unit 2>&1 || true

echo "=== E2E TESTS ==="
npm run build 2>&1 || true
npm start &
SERVER_PID=$!
sleep 20
VITE_API_URL=http://localhost:1337 xvfb-run --auto-servernum npx cypress run 2>&1 || true
kill $SERVER_PID 2>/dev/null || true

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


@Instance.register("cuttle-cards", "cuttle")
class Cuttle(Instance):
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

        # Remove ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        log = ansi_escape.sub("", test_log)

        # Split log into sections by markers
        sections = {}
        current_section = "pre"
        current_content: list[str] = []
        for line in log.splitlines():
            if "=== LINT ===" in line:
                sections[current_section] = "\n".join(current_content)
                current_section = "lint"
                current_content = []
            elif "=== UNIT TESTS ===" in line:
                sections[current_section] = "\n".join(current_content)
                current_section = "unit"
                current_content = []
            elif "=== E2E TESTS ===" in line:
                sections[current_section] = "\n".join(current_content)
                current_section = "e2e"
                current_content = []
            else:
                current_content.append(line)
        sections[current_section] = "\n".join(current_content)

        # --- Parse ESLint section ---
        if "lint" in sections:
            self._parse_lint(sections["lint"], passed_tests, failed_tests)

        # --- Parse Vitest section ---
        if "unit" in sections:
            self._parse_vitest(sections["unit"], passed_tests, failed_tests, skipped_tests)

        # --- Parse Cypress section ---
        if "e2e" in sections:
            self._parse_cypress(sections["e2e"], passed_tests, failed_tests, skipped_tests)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    def _parse_lint(
        self,
        lint_log: str,
        passed_tests: set,
        failed_tests: set,
    ) -> None:
        """Parse ESLint output. Each file with errors becomes a failed test."""
        current_file = None
        has_errors = False

        for line in lint_log.splitlines():
            # File path line (absolute path to .js or .vue file)
            file_match = re.match(r"^(/\S+\.(?:js|vue))\s*$", line)
            if file_match:
                current_file = file_match.group(1).replace("/home/cuttle/", "")
                continue

            # Error line: "  12:5  error  message  rule-name"
            error_match = re.match(
                r"^\s+\d+:\d+\s+error\s+.+?\s{2,}(\S+)\s*$", line
            )
            if error_match and current_file:
                rule = error_match.group(1)
                failed_tests.add(f"lint: {current_file} ({rule})")
                has_errors = True

        # If no errors found, lint passed
        if not has_errors and "✖" not in lint_log:
            passed_tests.add("lint: all files passed")

    def _parse_vitest(
        self,
        unit_log: str,
        passed_tests: set,
        failed_tests: set,
        skipped_tests: set,
    ) -> None:
        """Parse Vitest output."""
        for line in unit_log.splitlines():
            # Passed file: " ✓ tests/unit/file.spec.js  (N tests) 200ms"
            passed_match = re.match(r"^\s*[✓✔]\s+(.+?)(?:\s+\d+ms)?\s*$", line)
            if passed_match:
                name = passed_match.group(1).strip()
                if name:
                    # Normalize: strip " (N tests)" suffix for consistent matching
                    normalized = re.sub(r"\s+\(\d+ tests?\)$", "", name)
                    passed_tests.add(f"unit: {normalized}")
                continue

            # Failed file: " ❯ tests/unit/file.spec.js  (N tests | N failed) 200ms"
            failed_file_match = re.match(
                r"^\s*❯\s+(tests/.+?\.(?:spec|test)\.(?:js|ts|jsx|tsx))", line
            )
            if failed_file_match:
                name = failed_file_match.group(1).strip()
                if name:
                    failed_tests.add(f"unit: {name}")
                continue

            # Failed test: " ✗ test name" or " × test name"
            failed_match = re.match(r"^\s*[✗✕×]\s+(.+)\s*$", line)
            if failed_match:
                name = failed_match.group(1).strip()
                if name:
                    failed_tests.add(f"unit: {name}")
                continue

            # FAIL detail: " FAIL  tests/unit/file.spec.js > Suite > Test Name"
            fail_detail = re.match(r"^\s*FAIL\s+(tests/.+?>\s+.+)\s*$", line)
            if fail_detail:
                name = fail_detail.group(1).strip()
                if name:
                    failed_tests.add(f"unit: {name}")
                continue

            # Skipped test: " ↓ test name" or "- test name [skipped]"
            skipped_match = re.match(r"^\s*[↓-]\s+(.+?)(?:\s+\[skipped\])?\s*$", line)
            if skipped_match:
                name = skipped_match.group(1).strip()
                if name and ("skipped" in line.lower() or "↓" in line):
                    skipped_tests.add(f"unit: {name}")

    def _parse_cypress(
        self,
        e2e_log: str,
        passed_tests: set,
        failed_tests: set,
        skipped_tests: set,
    ) -> None:
        """Parse Cypress test output.

        Cypress outputs test results with indentation-based grouping:
          Suite Name
            ✓ test name (1234ms)
            1) failed test name
        """
        current_spec = ""

        for line in e2e_log.splitlines():
            # Skip Cypress "Run Finished" summary table lines
            # These contain │ box-drawing chars or "All specs passed/failed" summaries
            if "│" in line or "┌" in line or "┐" in line or "└" in line or "┘" in line or "├" in line or "┤" in line:
                continue
            if "All specs passed!" in line or "Run Finished" in line or "(Run Finished)" in line:
                continue

            # Spec file header: "  Running:  specs/file.spec.js (1 of 15)"
            spec_match = re.match(r"^\s*Running:\s*(.+?)\s+\(", line)
            if spec_match:
                current_spec = spec_match.group(1).strip()
                continue

            # Passed test: "    ✓ test name (1234ms)"
            cy_passed = re.match(
                r"^\s+[✓✔]\s+(.+?)(?:\s+\(\d+(?:ms|s)\))?\s*$", line
            )
            if cy_passed:
                name = cy_passed.group(1).strip()
                if name:
                    test_id = f"e2e: {name}" if not current_spec else f"e2e: [{current_spec}] {name}"
                    passed_tests.add(test_id)
                continue

            # Failed test: "    1) test name"
            cy_failed = re.match(r"^\s+\d+\)\s+(.+)\s*$", line)
            if cy_failed:
                name = cy_failed.group(1).strip()
                if name:
                    test_id = f"e2e: {name}" if not current_spec else f"e2e: [{current_spec}] {name}"
                    failed_tests.add(test_id)
                continue

            # Pending/skipped test: "  - test name"
            cy_pending = re.match(r"^\s+-\s+(.+)\s*$", line)
            if cy_pending:
                name = cy_pending.group(1).strip()
                if name and not re.match(r"^[\d.]", name):
                    test_id = f"e2e: {name}" if not current_spec else f"e2e: [{current_spec}] {name}"
                    skipped_tests.add(test_id)
