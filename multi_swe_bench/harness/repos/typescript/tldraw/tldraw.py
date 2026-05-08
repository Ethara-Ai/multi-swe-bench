import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Shared base classes -- parameterized by Node version + package manager style
# ---------------------------------------------------------------------------


class TldrawVersionBase(Image):
    """Base image for tldraw/tldraw -- clones repo, installs system deps.

    Args:
        node_image: Docker image name (e.g. "node:16-bookworm", "node:20-bookworm")
        interval_name: Used for image_tag/workdir dedup (e.g. "tldraw_0_to_565")
        use_corepack: True for Yarn Berry (era 2), False for Yarn Classic (era 1)
    """

    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        node_image: str,
        interval_name: str,
        use_corepack: bool = False,
    ):
        self._pr = pr
        self._config = config
        self._node_image = node_image
        self._interval_name = interval_name
        self._use_corepack = use_corepack

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

        if self._use_corepack:
            yarn_setup = "RUN corepack enable && corepack prepare yarn@stable --activate || npm install -g yarn --force"
        else:
            yarn_setup = "RUN npm install -g yarn@1 || true"

        return """FROM {image_name}

{global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN if grep -q 'buster\\|stretch' /etc/apt/sources.list 2>/dev/null; then \
      sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \
      sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \
      sed -i '/stretch-updates/d' /etc/apt/sources.list && \
      sed -i '/buster-updates/d' /etc/apt/sources.list && \
      echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid; fi
RUN apt-get update && apt-get install -y --no-install-recommends jq && rm -rf /var/lib/apt/lists/*
{yarn_setup}

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            yarn_setup=yarn_setup,
            clear_env=self.clear_env,
        )


# ---------------------------------------------------------------------------
# Shared parse_log for all epochs
# ---------------------------------------------------------------------------


def tldraw_parse_log(test_log: str) -> TestResult:
    """Parse Jest/Vitest test output for tldraw.

    Handles:
    - Jest verbose: PASS/FAIL file paths, checkmark/cross individual tests
    - Jest via lazyrepo: test-ci::<workspace>  PASS/FAIL <path>
    - Vitest verbose: checkmark/cross individual tests, suite pass/fail
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

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
    # Jest fail indicator: Suite > Test name
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
        r"^\s*FAIL\s+(\S+\.(?:test|spec|test-d)(?:-d)?\.(?:ts|tsx|js|jsx|mts|mjs))"
    )
    # Vitest summary: "Test Files  N passed (N)"
    re_vitest_summary_passed = re.compile(
        r"^\s*Test Files\s+(\d+)\s+passed"
    )
    re_vitest_summary_failed = re.compile(
        r"^\s*Test Files\s+(\d+)\s+failed"
    )

    for line in test_log.splitlines():
        # Strip ANSI codes
        line = ansi_escape.sub("", line)

        # Strip lazyrepo prefixes like "test-ci::packages/tlsync  "
        line = re.sub(r"^test-ci::\S+\s+", "", line)
        # Strip lerna prefixes like "@tldraw/core: "
        line = re.sub(r"^@tldraw/[\w-]+:\s*", "", line)

        line = line.strip()
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
            if m.group(1).strip() in passed_tests:
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

_ERA2_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Era 2: Yarn Berry via corepack
echo "prepare: era 2 (Yarn Berry) detected"
corepack enable 2>/dev/null || true
yarn install --no-immutable 2>&1 || yarn install 2>&1 || true
"""

_ERA1_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Era 1: Yarn Classic install
echo "prepare: era 1 (Yarn Classic) detected"
yarn install --frozen-lockfile 2>&1 || yarn install 2>&1 || true

# Build workspace packages so cross-package imports resolve
if [ -f lerna.json ]; then
    echo "prepare: lerna monorepo detected, bootstrapping..."
    npx lerna bootstrap --no-ci 2>&1 || true
    npx lerna run build --stream 2>&1 || true
elif [ -f packages/tldraw/package.json ]; then
    echo "prepare: building workspace packages..."
    yarn build:packages 2>&1 || yarn build 2>&1 || true
fi
"""

_ERA2_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}

# Era 2: use yarn test-ci which handles both lazyrepo (v2.x) and vitest (v3+)
echo "run_tests: era 2, trying yarn test-ci"
OUTPUT=$(NODE_OPTIONS="--max_old_space_size=4096" yarn test-ci 2>&1) || true
echo "$OUTPUT"

# If test-ci failed due to lazyrepo dependency (refresh-assets etc), fall back to direct test runner
if echo "$OUTPUT" | grep -q "Failed tasks:\|failed"; then
    if ! echo "$OUTPUT" | grep -qE "PASS |FAIL |✓|✗|Tests.*passed"; then
        echo "run_tests: test-ci had non-test failures, trying direct vitest/jest"
        # Try vitest first (v3+)
        if [ -f vitest.config.ts ] || [ -f vitest.config.mts ]; then
            NODE_OPTIONS="--max_old_space_size=4096" npx vitest run 2>&1 || true
        else
            # Fall back to jest via workspace
            NODE_OPTIONS="--max_old_space_size=4096" yarn run -T jest --ci --runInBand 2>&1 || true
        fi
    fi
fi
"""

_ERA1_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}

# Era 1: detect lerna vs simple yarn test
if [ -f lerna.json ]; then
    # Check if workspace:* protocol is used (lerna can't handle it, need yarn berry)
    if grep -rq '"workspace:\*"\|"workspace:\^"' packages/*/package.json 2>/dev/null; then
        echo "run_tests: workspace protocol detected, using yarn test-ci"
        NODE_OPTIONS="--max_old_space_size=4096" yarn test-ci 2>&1 || true
    else
        # Try lerna run test:ci first
        echo "run_tests: lerna monorepo, trying test:ci"
        OUTPUT=$(NODE_OPTIONS="--max_old_space_size=4096" npx lerna run test:ci --stream 2>&1) || true
        echo "$OUTPUT"
        # If lerna found no packages with test:ci, fall back to test
        if echo "$OUTPUT" | grep -q "No packages found with the lifecycle script"; then
            echo "run_tests: test:ci not found, falling back to yarn test"
            NODE_OPTIONS="--max_old_space_size=4096" yarn test --ci --runInBand 2>&1 || true
        fi
    fi
else
    echo "run_tests: simple monorepo, running yarn test"
    NODE_OPTIONS="--max_old_space_size=4096" yarn test --ci --runInBand 2>&1 || true
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
bash /home/run_tests.sh {test_files}
"""

_FIX_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch || true; git apply --whitespace=nowarn --reject /home/fix.patch || true; }}
bash /home/run_tests.sh {test_files}
"""
