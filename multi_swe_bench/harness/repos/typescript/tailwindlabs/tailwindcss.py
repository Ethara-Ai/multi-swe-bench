"""Tailwind CSS harness — shared base classes, parse_log, and shell scripts.

Covers tailwindlabs/tailwindcss across five major versions (v0–v4).
Era-specific files import from here and register their own Instance.
"""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Shared base image — parameterized by Node version + interval name
# ---------------------------------------------------------------------------


class TailwindImageBase(Image):
    """Base image: clone repo on a given Node version.

    Args:
        node_image: Docker image name (e.g. "node:18", "node:20")
        interval_name: Dedup key for image_tag/workdir
    """

    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        node_image: str,
        interval_name: str,
    ):
        self._pr = pr
        self._config = config
        self._node_image = node_image
        self._interval_name = interval_name

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return self._node_image

    def image_tag(self) -> str:
        return "base-{name}".format(name=self._interval_name)

    def workdir(self) -> str:
        return "base-{name}".format(name=self._interval_name)

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
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y --no-install-recommends jq && rm -rf /var/lib/apt/lists/*

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


# ---------------------------------------------------------------------------
# Shared parse_log — handles Jest (v0-v3) and Vitest (v4) output
# ---------------------------------------------------------------------------


def tailwindcss_parse_log(test_log: str) -> TestResult:
    """Parse Jest/Vitest test output for Tailwind CSS.

    Jest (v0-v3):
        PASS tests/basic-usage.test.js
          ✓ test name (511 ms)
          ✗ test name
        Test Suites: 1 passed, 1 total
        Tests:       7 passed, 7 total

    Vitest (v4):
         ✓ |tailwindcss| src/utilities.test.ts > test name
         ✗ |tailwindcss| src/utilities.test.ts > test name
         Test Files  1 passed (1)
              Tests  298 passed (298)
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    # Jest patterns
    re_jest_pass_suite = re.compile(
        r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
    )
    re_jest_fail_suite = re.compile(
        r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
    )
    re_jest_pass_test = re.compile(
        r"^\s*[✔✓√]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
    )
    re_jest_fail_test = re.compile(
        r"^\s*[×✕✗✘✖]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
    )
    re_jest_skip_test = re.compile(
        r"^\s*[○◌]\s+(?:skipped\s+)?(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
    )
    # Jest fail indicator: ● Suite › Test name
    re_jest_fail_indicator = re.compile(r"^\s*●\s+(.+?)\s+›\s+(.+)$")

    # Vitest patterns
    re_vitest_pass = re.compile(
        r"^\s*[✓✔]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"
    )
    re_vitest_fail = re.compile(
        r"^\s*[×✗]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"
    )
    re_vitest_skip = re.compile(
        r"^\s*[-↓]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"
    )
    # Vitest file-level FAIL
    re_vitest_fail_file = re.compile(
        r"^\s*FAIL\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mts|mjs))"
    )
    # Vitest summary lines
    re_vitest_summary_passed = re.compile(
        r"^\s*Test Files\s+(\d+)\s+passed"
    )
    re_vitest_summary_failed = re.compile(
        r"^\s*Test Files\s+(\d+)\s+failed"
    )

    for line in test_log.splitlines():
        line = ansi_escape.sub("", line).strip()
        if not line:
            continue

        # Jest suite-level PASS/FAIL
        m = re_jest_pass_suite.match(line)
        if m:
            passed_tests.add(m.group(1).strip())
            continue

        m = re_jest_fail_suite.match(line)
        if m:
            failed_tests.add(m.group(1).strip())
            passed_tests.discard(m.group(1).strip())
            continue

        # Jest individual test pass/fail/skip
        m = re_jest_pass_test.match(line)
        if m:
            name = m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
            continue

        m = re_jest_fail_test.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_jest_skip_test.match(line)
        if m:
            skipped_tests.add(m.group(1).strip())
            continue

        # Jest fail indicator
        m = re_jest_fail_indicator.match(line)
        if m:
            name = "{suite} > {test}".format(
                suite=m.group(1).strip(), test=m.group(2).strip()
            )
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        # Vitest pass/fail/skip
        m = re_vitest_pass.match(line)
        if m:
            name = m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
            continue

        m = re_vitest_fail.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_vitest_skip.match(line)
        if m:
            skipped_tests.add(m.group(1).strip())
            continue

        m = re_vitest_fail_file.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_vitest_summary_passed.match(line)
        if m and not passed_tests:
            passed_tests.add("__vitest_suite_passed__")
            continue

        m = re_vitest_summary_failed.match(line)
        if m and not failed_tests:
            failed_tests.add("__vitest_suite_failed__")
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
# Shared shell script templates
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

# v0/v1/v2: npm install, no prebuild
_V0V1V2_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

npm install --ignore-scripts 2>&1
"""

# v0/v1/v2: run jest on specified files
_V0V1V2_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
TEST_FILES="$@"

NODE_OPTIONS="--max_old_space_size=4096" npx jest --verbose --no-coverage $TEST_FILES 2>&1 || true
"""

# v2late (v2.2): npm install + babelify (creates lib/ from src/)
# Needs NODE_OPTIONS=--openssl-legacy-provider for ncc postbabelify step on node:18
_V2LATE_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

npm install --ignore-scripts 2>&1

# Babelify: compile src/ -> lib/ (required for v2.2 test imports)
# prebabelify runs generate:plugin-list if available
NODE_OPTIONS=--openssl-legacy-provider npm run babelify 2>&1 || true
"""

# v2late: run jest on specified files
_V2LATE_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
TEST_FILES="$@"

NODE_OPTIONS="--max_old_space_size=4096" npx jest --verbose --no-coverage $TEST_FILES 2>&1 || true
"""

# v3: npm install + generate plugin list
_V3_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

npm install --ignore-scripts 2>&1

# Generate plugin list (required before tests)
npm run generate 2>&1 || npm run generate:plugin-list 2>&1 || npm run pretest 2>&1 || true
"""

# v3: run jest on specified files
_V3_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
TEST_FILES="$@"

NODE_OPTIONS="--max_old_space_size=4096" npx jest --verbose --no-coverage $TEST_FILES 2>&1 || true
"""

# v4: pnpm install (already done in prepare), run vitest
_V4_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

npm install -g pnpm@9.6.0 2>&1
pnpm install --no-frozen-lockfile --ignore-scripts 2>&1
"""

# v4: run vitest on specified files
_V4_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
TEST_FILES="$@"

NODE_OPTIONS="--max_old_space_size=4096" npx vitest run --reporter=verbose --hideSkippedTests $TEST_FILES 2>&1 || true
"""

# Shared run/test-run/fix-run wrappers (all eras use same pattern)
_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
bash /home/run_tests.sh {test_files}
"""

_TEST_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
bash /home/run_tests.sh {test_files}
"""

_FIX_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch || true; git apply --whitespace=nowarn --reject /home/fix.patch || true; }}
bash /home/run_tests.sh {test_files}
"""
