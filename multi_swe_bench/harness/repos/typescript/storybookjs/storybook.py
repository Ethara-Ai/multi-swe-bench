import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Shared base classes — parameterized by Node version + package manager style
# ---------------------------------------------------------------------------


class StorybookVersionBase(Image):
    """Base image for storybookjs/storybook — clones repo, installs system deps.

    Args:
        node_image: Docker image name (e.g. "node:16", "node:22")
        interval_name: Used for image_tag/workdir dedup (e.g. "storybook_17338_to_11537")
        use_corepack: True for Yarn Berry (code-epoch), False for Yarn 1 (lib-epoch)
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


def storybook_parse_log(test_log: str) -> TestResult:
    """Parse Jest/Vitest test output for Storybook.

    Handles:
    - Jest verbose: PASS/FAIL file paths, checkmark/cross individual tests
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
    # Vitest file-level FAIL (with optional [...] suffix): FAIL path/to/file.ts [...]
    re_vitest_fail_file = re.compile(
        r"^\s*FAIL\s+(\S+\.(?:test|spec|test-d)(?:-d)?\.(?:ts|tsx|js|jsx|mts|mjs))"
    )
    # Vitest summary: "Test Files  N passed (N)" — used for typecheck-only suites
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
# Default Storybook Instance (fallback for PRs without number_interval)
# Uses node:22 + corepack (code-epoch default)
# ---------------------------------------------------------------------------


class StorybookImageBase(Image):
    """Default base image — node:22, corepack yarn, clone repo."""

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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

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
RUN corepack enable && corepack prepare yarn@stable --activate || npm install -g yarn --force

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


class StorybookImageDefault(Image):
    """Default per-PR image — code-epoch style."""

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
        return StorybookImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def _test_files(self) -> list[str]:
        files = []
        for m in re.findall(r"diff --git a/(\S+)", self.pr.test_patch):
            if ".test." in m or ".spec." in m or ".test-d." in m or ".stories." in m:
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
                _CODE_EPOCH_PREPARE_SH.format(
                    repo=self.pr.repo, base_sha=self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                _CODE_EPOCH_RUN_TESTS_SH.format(repo=self.pr.repo),
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


@Instance.register("storybookjs", "storybook")
class Storybook(Instance):
    """Default instance for storybookjs/storybook (code-epoch, no number_interval)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StorybookImageDefault(self.pr, self._config)

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
        return storybook_parse_log(test_log)


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

_CODE_EPOCH_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install dependencies from code/ subdir (Yarn Berry)
if [ -f code/yarn.lock ] || [ -f code/package.json ]; then
    echo "prepare: code-epoch detected, installing from code/"
    cd code
    yarn install --no-immutable 2>&1 || yarn install 2>&1 || npm install --legacy-peer-deps 2>&1 || true

    # Build workspace packages so cross-package imports resolve (dist/ dirs)
    echo "prepare: building code workspace packages..."
    for dir in lib/*/; do
        if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
            (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
        fi
    done
    for dir in addons/*/; do
        if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
            (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
        fi
    done
    for dir in renderers/*/; do
        if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
            (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
        fi
    done
    for dir in frameworks/*/; do
        if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
            (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
        fi
    done
    if [ -f core/tsconfig.json ] && [ -d core/src ]; then
        (cd core && npx tsc --outDir dist --declaration 2>/dev/null) || true
    fi
    echo "prepare: code workspace build complete"
    cd ..

# Fallback: lib-epoch PR in code-epoch interval (has root yarn.lock but no code/)
elif [ -f yarn.lock ]; then
    echo "prepare: lib-epoch fallback — installing from root (Yarn)"
    yarn install --frozen-lockfile --ignore-scripts 2>&1 || yarn install --ignore-scripts 2>&1 || npm install --legacy-peer-deps --ignore-scripts 2>&1 || true
    yarn install --frozen-lockfile 2>&1 || yarn install 2>&1 || true

    echo "prepare: building lib packages..."
    for dir in lib/*/; do
        if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
            (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
        fi
    done
    for dir in addons/*/; do
        if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
            (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
        fi
    done
    for dir in app/*/; do
        if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
            (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
        fi
    done
    echo "prepare: package build complete"

else
    echo "prepare: WARNING — no yarn.lock found, attempting root install"
    yarn install --no-immutable 2>&1 || yarn install 2>&1 || npm install --legacy-peer-deps 2>&1 || true
fi
"""

_LIB_EPOCH_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install dependencies from repo root (Yarn 1 classic)
echo "prepare: lib-epoch detected, installing from root"
yarn install --frozen-lockfile --ignore-scripts 2>&1 || yarn install --ignore-scripts 2>&1 || npm install --legacy-peer-deps --ignore-scripts 2>&1 || true

# Run postinstall scripts selectively (skip node-sass etc.)
yarn install --frozen-lockfile 2>&1 || yarn install 2>&1 || true

# Build all workspace packages so cross-package imports resolve
echo "prepare: building lib packages..."
for dir in lib/*/; do
    if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
        (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
    fi
done
for dir in addons/*/; do
    if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
        (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
    fi
done
for dir in app/*/; do
    if [ -f "$dir/tsconfig.json" ] && [ -d "$dir/src" ]; then
        (cd "$dir" && npx tsc --outDir dist --declaration 2>/dev/null) || true
    fi
done
echo "prepare: package build complete"
"""

_CODE_EPOCH_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
TEST_FILES="$@"

# Detect if test files are lib-epoch (lib/, addons/, app/ paths) vs code-epoch (code/ paths)
HAS_LIB_PATHS=false
HAS_CODE_PATHS=false
for f in $TEST_FILES; do
    case "$f" in
        lib/*|addons/*|app/*|examples/*) HAS_LIB_PATHS=true ;;
        code/*) HAS_CODE_PATHS=true ;;
    esac
done

# --- Lib-epoch fallback (for PRs with lib/addons/app paths) ---
if [ "$HAS_LIB_PATHS" = true ] && [ "$HAS_CODE_PATHS" = false ]; then
    echo "run_tests: Detected lib-epoch paths, using root jest"
    if [ -f jest.config.js ]; then
        NODE_OPTIONS="--max_old_space_size=4096" yarn jest --verbose --no-coverage --config jest.config.js $TEST_FILES 2>&1 || true
    else
        NODE_OPTIONS="--max_old_space_size=4096" npx jest --verbose --no-coverage $TEST_FILES 2>&1 || true
    fi
    exit 0
fi

# --- Code-epoch: strip code/ prefix ---
STRIPPED=""
for f in $TEST_FILES; do
    STRIPPED="$STRIPPED ${{f#code/}}"
done

# --- Find per-package vitest config ---
PKG_CONFIG=""
for f in $TEST_FILES; do
    stripped="${{f#code/}}"
    pkg_dir=$(echo "$stripped" | cut -d/ -f1-2)
    if [ -f "code/$pkg_dir/vitest.config.ts" ]; then
        PKG_CONFIG="$pkg_dir/vitest.config.ts"
        break
    fi
    pkg_dir1=$(echo "$stripped" | cut -d/ -f1)
    if [ -f "code/$pkg_dir1/vitest.config.ts" ]; then
        PKG_CONFIG="$pkg_dir1/vitest.config.ts"
        break
    fi
done

# --- Vitest: per-package config found (preferred) ---
if [ -n "$PKG_CONFIG" ]; then
    cd code
    echo "run_tests: Using package vitest config: $PKG_CONFIG"
    NODE_OPTIONS="--max_old_space_size=4096" yarn vitest run --reporter=verbose --config "$PKG_CONFIG" $STRIPPED 2>&1 || true

# --- Vitest: root config exists ---
elif [ -f code/vitest.config.ts ] || [ -f code/vitest.config.js ]; then
    cd code
    echo "run_tests: Using root vitest config"
    NODE_OPTIONS="--max_old_space_size=4096" yarn vitest run --reporter=verbose $STRIPPED 2>&1 || true

# --- Jest: code/jest.config.js exists ---
elif [ -f code/jest.config.js ]; then
    cd code
    NODE_OPTIONS="--max_old_space_size=4096" yarn jest --verbose --no-coverage --config ./jest.config.js $STRIPPED 2>&1 || true

# --- Fallback: try npx vitest/jest ---
else
    cd code
    echo "run_tests: No standard config found, trying npx jest then vitest"
    if command -v jest &>/dev/null || [ -f node_modules/.bin/jest ]; then
        NODE_OPTIONS="--max_old_space_size=4096" npx jest --verbose --no-coverage $STRIPPED 2>&1 || true
    else
        NODE_OPTIONS="--max_old_space_size=4096" npx vitest run --reporter=verbose $STRIPPED 2>&1 || true
    fi
fi
"""

_LIB_EPOCH_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
TEST_FILES="$@"

# Lib-epoch jest: root jest.config.js
if [ -f jest.config.js ]; then
    NODE_OPTIONS="--max_old_space_size=4096" yarn jest --verbose --no-coverage --config jest.config.js $TEST_FILES 2>&1 || true
else
    NODE_OPTIONS="--max_old_space_size=4096" npx jest --verbose --no-coverage $TEST_FILES 2>&1 || true
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
