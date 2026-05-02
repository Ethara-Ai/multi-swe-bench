from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Binary diffs cause 'cannot apply binary patch without full index line'
    errors with git apply.  These are typically documentation or media assets
    not needed for testing.
    """
    if not patch_content:
        return patch_content

    lines = patch_content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('diff --git'):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith('diff --git'):
                if lines[i].startswith('GIT binary patch') or lines[i].startswith('Binary files'):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)


class ElectronImageBase(Image):
    """Base image for Electron: Ubuntu 22.04 with Node.js, Python, and build
    tools.  Clones the electron/electron repository.

    Electron uses a GN+Ninja build system (Chromium) that requires a full
    depot_tools checkout and hours of compilation.  For the harness we only
    need the source tree (for patches) and Node.js (to run the JS/TS test
    suite against an Electron binary).
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "cmake",
            "ninja-build",
            "pkg-config",
            "libgtk-3-dev",
            "libnotify-dev",
            "libdbus-1-dev",
            "libxrandr-dev",
            "libxcomposite-dev",
            "libxdamage-dev",
            "libxcursor-dev",
            "libcups2-dev",
            "libpango1.0-dev",
            "libasound2-dev",
            "libxtst-dev",
            "libnss3-dev",
            "xvfb",
            "xauth",
            "python3-dbusmock",
            "openssh-client",
        ]

    def extra_setup(self) -> str:
        return (
            '# Rewrite SSH git URLs to HTTPS (abstract-socket dep uses SSH)\n'
            'RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \\\n'
            '    git config --global url."https://github.com/".insteadOf "git@github.com:"\n'
            '\n'
            'RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \\\n'
            '    apt-get install -y nodejs && \\\n'
            '    rm -rf /var/lib/apt/lists/*\n'
            'RUN corepack enable'
        )


class ElectronImageDefault(Image):
    """Per-PR image for Electron.  Depends on ElectronImageBase, copies patch
    files, and prepares the checkout at the PR base commit.
    """

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
        return ElectronImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _filter_binary_patches(self.pr.fix_patch)
        filtered_test_patch = _filter_binary_patches(self.pr.test_patch)

        # Extract major version from base.ref (e.g. "36-x-y" -> "36")
        major_version = self.pr.base.ref.split("-")[0]

        return [
            File(
                ".",
                "fix.patch",
                f"{filtered_fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{filtered_test_patch}",
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
source /home/electron_test_utils.sh

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Stub third_party/nan so spec/package.json "nan": "file:../../third_party/nan"
# resolves without a full Chromium checkout.  Older branches (35-38) reference it.
if grep -q 'third_party/nan' spec/package.json 2>/dev/null; then
    mkdir -p /home/third_party/nan
    echo '{{"name":"nan","version":"2.20.0","main":"nan.h"}}' > /home/third_party/nan/package.json
fi

# Re-install deps after checkout using the correct package manager
install_deps /home/{pr.repo}

# Pre-generate electron.d.ts at base commit so spec-runner skips
# generateTypeDefinitions().  This avoids TS conflicts when patches
# introduce @types/node version changes (Yarn 4 migration PRs).
if [ -f package.json ] && grep -q 'create-typescript-definitions' package.json; then
    echo "=== Pre-generating electron.d.ts at base commit ==="
    npm run create-typescript-definitions 2>&1 || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "electron_test_utils.sh",
                """#!/bin/bash
# Electron test utilities: uses --electronVersion to download pre-built
# Electron binary instead of requiring a full source build.

export DISPLAY=:99
export IGNORE_YARN_INSTALL_ERROR=1
ELECTRON_MAJOR_VERSION="{major_version}"

start_xvfb() {{
    if ! pgrep -x Xvfb > /dev/null 2>&1; then
        Xvfb :99 -screen 0 1024x768x24 -ac +extension GLX +render -noreset &
        sleep 1
    fi
}}

install_deps() {{
    # Branch-aware dependency install.
    # 35-39 branches use npm; 40+ use yarn 4 via .yarn/releases/.
    local repo_dir="${{1:-/home/electron}}"
    cd "$repo_dir"

    if [ -d .yarn/releases ] && ls .yarn/releases/yarn-*.cjs 1>/dev/null 2>&1; then
        echo "Installing deps with yarn..."
        YARN_ENABLE_IMMUTABLE_INSTALLS=false node .yarn/releases/yarn-*.cjs install 2>&1 || true
    elif [ -f package-lock.json ]; then
        echo "Installing deps with npm..."
        npm install --no-audit --no-fund 2>&1 || true
    elif [ -f package.json ]; then
        echo "Installing deps with npm (no lockfile)..."
        npm install --no-audit --no-fund 2>&1 || true
    fi
}}

derive_test_files() {{
    local patch_file="$1"
    local spec_files=""
    local node_test_files=""

    if [ ! -f "$patch_file" ]; then
        return
    fi

    local patch_paths=$(grep -oP '(?<=^diff --git a/)\\S+' "$patch_file" 2>/dev/null || true)

    for f in $patch_paths; do
        case "$f" in
            spec/*-spec.ts|spec/*-spec.js|spec/**/*-spec.ts|spec/**/*-spec.js)
                spec_files="$spec_files $f"
                ;;
            spec/*.ts|spec/*.js)
                local dir=$(dirname "$f")
                if ls "$dir"/*-spec.ts "$dir"/*-spec.js 2>/dev/null | head -1 > /dev/null 2>&1; then
                    spec_files="$spec_files $(ls "$dir"/*-spec.ts "$dir"/*-spec.js 2>/dev/null | head -5 | tr '\\n' ' ')"
                fi
                ;;
            test/parallel/*.js)
                node_test_files="$node_test_files $f"
                ;;
            shell/*|lib/*|chromium_src/*)
                local base=$(basename "$f" | sed 's/\\.[^.]*$//')
                local related=$(find spec/ -name "*${{base}}*-spec.*" 2>/dev/null | head -5 | tr '\\n' ' ')
                if [ -n "$related" ]; then
                    spec_files="$spec_files $related"
                fi
                ;;
        esac
    done

    if [ -n "$spec_files" ]; then
        echo "SPEC_FILES='$(echo $spec_files | tr ' ' '\\n' | sort -u | tr '\\n' ' ')'"
    fi
    if [ -n "$node_test_files" ]; then
        echo "NODE_TEST_FILES='$(echo $node_test_files | tr ' ' '\\n' | sort -u | tr '\\n' ' ')'"
    fi
}}

run_electron_tests() {{
    local repo_dir="$1"
    cd "$repo_dir"

    start_xvfb

    eval $(derive_test_files /home/test.patch)

    local ran_tests=false

    if [ -n "$SPEC_FILES" ]; then
        echo "=== Running Electron Spec Tests ==="
        echo "Test files: $SPEC_FILES"
        if [ -f script/spec-runner.js ]; then
            node script/spec-runner.js --electronVersion "latest@$ELECTRON_MAJOR_VERSION" --no-sandbox --files $SPEC_FILES 2>&1 || true
        fi
        ran_tests=true
    fi

    if [ -n "$NODE_TEST_FILES" ]; then
        echo "=== Running Node.js Parallel Tests ==="
        for test_file in $NODE_TEST_FILES; do
            if [ -f "$test_file" ]; then
                echo "Running: $test_file"
                node "$test_file" 2>&1 || true
            fi
        done
        ran_tests=true
    fi

    if [ "$ran_tests" = false ]; then
        echo "No test files detected from patches."
        echo "No tests to run."
    fi
}}
""".format(major_version=major_version),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
source /home/electron_test_utils.sh

cd /home/{pr.repo}

# run.sh executes on the clean base commit — no patches applied.
# Derive test targets from test.patch to know what to run.
run_electron_tests /home/{pr.repo}

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
source /home/electron_test_utils.sh

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi

# Yarn 4 migration fix: test.patch patches spec-runner.js to use
# YARN_SCRIPT_PATH (Yarn 4 API) but script/yarn.js at base commit
# only exports YARN_VERSION.  Also spec/package.json gets workspace
# "*" refs that npm cannot resolve.  Fix: revert workspace refs to
# file: paths, delete stale spec/yarn.lock, npm-install spec deps,
# and use --skipYarnInstall to bypass broken installSpecModules().
if grep -q 'YARN_SCRIPT_PATH' script/spec-runner.js 2>/dev/null; then
    echo "=== Yarn 4 migration detected (test stage) ==="
    # Revert workspace "*" refs to file: refs so npm can install
    if [ -f spec/package.json ]; then
        sed -i 's|"@electron-ci/echo": "\\*"|"@electron-ci/echo": "file:./fixtures/native-addon/echo"|g' spec/package.json
        sed -i 's|"@electron-ci/external-ab": "\\*"|"@electron-ci/external-ab": "file:./fixtures/native-addon/external-ab"|g' spec/package.json
        sed -i 's|"@electron-ci/is-valid-window": "\\*"|"@electron-ci/is-valid-window": "file:./fixtures/native-addon/is-valid-window"|g' spec/package.json
        sed -i 's|"@electron-ci/osr-gpu": "\\*"|"@electron-ci/osr-gpu": "file:./fixtures/native-addon/osr-gpu"|g' spec/package.json
        sed -i 's|"@electron-ci/uv-dlopen": "\\*"|"@electron-ci/uv-dlopen": "file:./fixtures/native-addon/uv-dlopen"|g' spec/package.json
    fi
    rm -f spec/yarn.lock
    cd /home/{pr.repo}/spec && npm install --ignore-scripts --no-audit --no-fund 2>&1 || true
    cd /home/{pr.repo}
    SKIP_YARN_INSTALL=1
elif grep -q 'package.json' /home/test.patch 2>/dev/null; then
    install_deps /home/{pr.repo}
fi

if [ "${{SKIP_YARN_INSTALL:-}}" = "1" ]; then
    start_xvfb
    eval $(derive_test_files /home/test.patch)
    if [ -n "$SPEC_FILES" ]; then
        echo "=== Running Electron Spec Tests ==="
        echo "Test files: $SPEC_FILES"
        node script/spec-runner.js --electronVersion "latest@$ELECTRON_MAJOR_VERSION" --no-sandbox --skipYarnInstall --files $SPEC_FILES 2>&1 || true
    else
        echo "No test files detected from patches."
        echo "No tests to run."
    fi
else
    run_electron_tests /home/{pr.repo}
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
source /home/electron_test_utils.sh

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi

if [ -f .yarnrc.yml ] && grep -q 'yarnPath' .yarnrc.yml && [ -d .yarn/releases ]; then
    echo "=== Yarn 4 migration detected (fix stage) ==="
    rm -f yarn.lock yarn.lock.rej
    YARN_ENABLE_IMMUTABLE_INSTALLS=false node .yarn/releases/yarn-*.cjs install 2>&1 || true
    SKIP_YARN_INSTALL=1
elif grep -q 'package.json' /home/test.patch /home/fix.patch 2>/dev/null; then
    install_deps /home/{pr.repo}
fi

if [ "${{SKIP_YARN_INSTALL:-}}" = "1" ]; then
    start_xvfb
    eval $(derive_test_files /home/test.patch)
    if [ -n "$SPEC_FILES" ]; then
        echo "=== Running Electron Spec Tests ==="
        echo "Test files: $SPEC_FILES"
        node script/spec-runner.js --electronVersion "latest@$ELECTRON_MAJOR_VERSION" --no-sandbox --skipYarnInstall --files $SPEC_FILES 2>&1 || true
    else
        echo "No test files detected from patches."
        echo "No tests to run."
    fi
else
    run_electron_tests /home/{pr.repo}
fi

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


@Instance.register("electron", "electron")
class Electron(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ElectronImageDefault(self.pr, self._config)

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

        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Mocha individual test patterns
        # ✓ test name (NNms)
        re_mocha_pass = re.compile(
            r"^\s+[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$"
        )
        # NN) test name
        re_mocha_fail_numbered = re.compile(
            r"^\s+\d+\)\s+(.+?)\s*$"
        )
        # ✗ test name  or  ✖ test name
        re_mocha_fail_symbol = re.compile(
            r"^\s+[✗✖✘]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$"
        )
        # - test name  (pending/skipped in Mocha)
        re_mocha_pending = re.compile(
            r"^\s+-\s+(.+?)\s*$"
        )

        # Mocha summary line patterns
        # N passing (Ns)
        re_summary_passing = re.compile(
            r"^\s+(\d+)\s+passing\b"
        )
        # N failing
        re_summary_failing = re.compile(
            r"^\s+(\d+)\s+failing\b"
        )
        # N pending
        re_summary_pending = re.compile(
            r"^\s+(\d+)\s+pending\b"
        )

        # Node.js TAP-like output patterns (test/parallel/)
        # ok N - test description
        re_tap_pass = re.compile(
            r"^ok\s+\d+\s+-?\s*(.+?)\s*$"
        )
        # not ok N - test description
        re_tap_fail = re.compile(
            r"^not ok\s+\d+\s+-?\s*(.+?)\s*$"
        )
        # TAP skip: ok N description # SKIP reason
        re_tap_skip = re.compile(
            r"^ok\s+\d+\s+.*#\s*[Ss][Kk][Ii][Pp]\b"
        )

        in_failing_section = False
        summary_found = False

        for line in clean_log.splitlines():
            stripped = line.rstrip()

            # Detect Mocha "N failing" section header (failures listed after)
            if re_summary_failing.match(stripped):
                in_failing_section = True
                summary_found = True
                continue

            if re_summary_passing.match(stripped):
                summary_found = True
                in_failing_section = False
                continue

            if re_summary_pending.match(stripped):
                summary_found = True
                continue

            # Mocha individual pass
            match = re_mocha_pass.match(stripped)
            if match:
                test_name = match.group(1).strip()
                if test_name:
                    passed_tests.add(test_name)
                continue

            # Mocha individual fail (symbol-prefixed)
            match = re_mocha_fail_symbol.match(stripped)
            if match:
                test_name = match.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                continue

            # Mocha inline numbered failure "N) test name" — only valid
            # before the summary.  After "N failing", numbered items are
            # suite names in the failure-detail block, not test names.
            if not summary_found:
                match = re_mocha_fail_numbered.match(stripped)
                if match:
                    test_name = match.group(1).strip()
                    if test_name and not test_name.startswith('at ') and not test_name.startswith('Error'):
                        failed_tests.add(test_name)
                    continue

            if in_failing_section:
                if not stripped or (stripped and not stripped.startswith(' ')):
                    in_failing_section = False

            # Mocha pending
            match = re_mocha_pending.match(stripped)
            if match:
                test_name = match.group(1).strip()
                if test_name:
                    skipped_tests.add(test_name)
                continue

            # TAP skip (must check before TAP pass)
            if re_tap_skip.match(stripped):
                tap_name_match = re.match(r"^ok\s+\d+\s+-?\s*(.+?)(?:\s*#\s*[Ss][Kk][Ii][Pp].*)?$", stripped)
                if tap_name_match:
                    test_name = tap_name_match.group(1).strip()
                    if test_name:
                        skipped_tests.add(test_name)
                continue

            # TAP pass
            match = re_tap_pass.match(stripped)
            if match:
                test_name = match.group(1).strip()
                if test_name:
                    passed_tests.add(test_name)
                continue

            # TAP fail
            match = re_tap_fail.match(stripped)
            if match:
                test_name = match.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                continue

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
