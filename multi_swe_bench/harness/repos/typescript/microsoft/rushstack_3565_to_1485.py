from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "rushstack"

# PRs whose Rush CLI crashes on Node 12 due to npm dependency drift
# (@azure/logger@^1.0.0 resolves to >=1.2.0 which needs Node 18+ for node: protocol)
# Use Node 16 (highest allowed by Rush's nodeSupportedVersionRange for most of these PRs)
# We also sed out nodeSupportedVersionRange in prepare.sh/run scripts to allow Node 16
NODE16_PRS = {2510, 3407, 3469, 3543, 3565}


class rushstack_3565_to_1485_ImageBase(Image):
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
        if self.pr.number in NODE16_PRS:
            return "node:16"
        return "node:12"

    def image_tag(self) -> str:
        if self.pr.number in NODE16_PRS:
            return "base-node16"
        return "base"

    def workdir(self) -> str:
        if self.pr.number in NODE16_PRS:
            return "base-node16"
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{REPO_DIR}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {REPO_DIR} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class rushstack_3565_to_1485_ImageDefault(Image):
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
        return rushstack_3565_to_1485_ImageBase(self.pr, self._config)

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
                "detect_projects.sh",
                """#!/bin/bash
PATCH_FILE="$1"
if [ ! -f "$PATCH_FILE" ]; then
    exit 0
fi
grep '^diff --git' "$PATCH_FILE" | sed 's|diff --git a/||;s| b/.*||' | while read -r fpath; do
    dir="$fpath"
    while [ "$dir" != "." ] && [ -n "$dir" ]; do
        if [ -f "/home/{repo_dir}/$dir/package.json" ]; then
            pkg_name=$(node -e "try{{console.log(require('/home/{repo_dir}/'+'$dir'+'/package.json').name)}}catch(e){{}}")
            if [ -n "$pkg_name" ]; then
                echo "$pkg_name"
            fi
            break
        fi
        dir=$(dirname "$dir")
    done
done | sort -u
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run_jest.sh",
                """#!/bin/bash
# Run jest directly in a project dir for parseable output
# Usage: run_jest.sh <project_dir>
# Requires: rush build already completed (compiled JS in lib/)
PDIR="$1"
REPO="/home/{repo_dir}"

if [ ! -d "$REPO/$PDIR" ]; then
    exit 0
fi

# Check for any jest config: gulp-core-build era (config/jest.json) or heft era (config/jest.config.json)
HAS_JEST_CONFIG=false
if [ -f "$REPO/$PDIR/config/jest.json" ] || [ -f "$REPO/$PDIR/config/jest.config.json" ]; then
    HAS_JEST_CONFIG=true
fi

if [ "$HAS_JEST_CONFIG" = false ]; then
    exit 0
fi

cd "$REPO/$PDIR"

# Check that lib/ directory exists (build must have compiled tests)
if [ ! -d "lib" ]; then
    echo "=== No lib/ directory in $PDIR, skipping jest ==="
    exit 0
fi

echo "=== Running jest in $PDIR ==="

# Find jest binary - check local, shared temp, then search common/temp
JEST_BIN=""
if [ -x "./node_modules/.bin/jest" ]; then
    JEST_BIN="./node_modules/.bin/jest"
elif [ -x "$REPO/common/temp/node_modules/.bin/jest" ]; then
    JEST_BIN="$REPO/common/temp/node_modules/.bin/jest"
else
    JEST_BIN=$(find "$REPO/common/temp" -not -path '*/rush-recycler/*' -path "*/jest-cli/bin/jest.js" -type f 2>/dev/null | head -1)
fi

if [ -z "$JEST_BIN" ]; then
    echo "=== jest binary not found for $PDIR, skipping ==="
    exit 0
fi

# Use --roots lib + --testRegex to find compiled .test.js files (works for both gulp and heft eras)
"$JEST_BIN" --rootDir . --roots lib --testRegex '.*\\.test\\.js$' --no-coverage --no-cache --no-watchman --verbose 2>&1 || true
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "detect_project_dirs.sh",
                """#!/bin/bash
PATCH_FILE="$1"
if [ ! -f "$PATCH_FILE" ]; then
    exit 0
fi
grep '^diff --git' "$PATCH_FILE" | sed 's|diff --git a/||;s| b/.*||' | while read -r fpath; do
    dir="$fpath"
    while [ "$dir" != "." ] && [ -n "$dir" ]; do
        if [ -f "/home/{repo_dir}/$dir/package.json" ]; then
            echo "$dir"
            break
        fi
        dir=$(dirname "$dir")
    done
done | sort -u
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "snapshot_save.sh",
                """#!/bin/bash
# snapshot_save.sh <patch_file> <repo_dir>
# Delete-Build-Compare strategy:
# 1. Save the EXPECTED state of output files (current post-patch state)
# 2. DELETE output files so rush build MUST regenerate them
# 3. snapshot_verify.sh later compares regenerated vs saved expected
#
# Only output files are deleted (dist/, etc/, lib/); source files (src/) are kept.
# Called AFTER patches are applied but BEFORE rush build.
PATCH_FILE="$1"
REPO_DIR="$2"
SNAPSHOT_STASH="/tmp/snapshot_expected"

rm -rf "$SNAPSHOT_STASH"
mkdir -p "$SNAPSHOT_STASH"

if [ ! -f "$PATCH_FILE" ]; then
    echo "snapshot_save: no patch file at $PATCH_FILE, skipping"
    exit 0
fi

# Extract files touched by the patch
FILES_TO_CHECK=$(grep -E '^\\+\\+\\+ b/' "$PATCH_FILE" | sed 's|^+++ b/||' | sort -u)

FILE_COUNT=0
DELETED_COUNT=0
for rel_path in $FILES_TO_CHECK; do
    abs_path="$REPO_DIR/$rel_path"
    if [ -f "$abs_path" ]; then
        mkdir -p "$(dirname "$SNAPSHOT_STASH/$rel_path")"
        cp "$abs_path" "$SNAPSHOT_STASH/$rel_path"
        FILE_COUNT=$((FILE_COUNT + 1))

        # Only delete OUTPUT files (not source) so build must regenerate them
        # Output patterns: dist/, etc/, lib/ build outputs, lockfiles, temp/
        IS_OUTPUT=false
        case "$rel_path" in
            */dist/*|*/etc/*|*/lib/*|*/temp/*)
                IS_OUTPUT=true ;;
        esac

        if $IS_OUTPUT; then
            rm -f "$abs_path"
            DELETED_COUNT=$((DELETED_COUNT + 1))
        fi
    fi
done

echo "$FILES_TO_CHECK" > "$SNAPSHOT_STASH/.file_list"
echo "snapshot_save: saved $FILE_COUNT expected files, deleted $DELETED_COUNT output files for regeneration"
""",
            ),
            File(
                ".",
                "snapshot_verify.sh",
                """#!/bin/bash
# snapshot_verify.sh <repo_dir>
# Compares regenerated (post-build) files against saved expected state.
# If output files were deleted by snapshot_save.sh and build didn't regenerate them,
# they'll be missing -> FAIL. If build crashed entirely, all deleted files missing -> all FAIL.
REPO_DIR="$1"
SNAPSHOT_STASH="/tmp/snapshot_expected"

if [ ! -f "$SNAPSHOT_STASH/.file_list" ]; then
    echo "snapshot_verify: no file list found, skipping"
    exit 0
fi

PASS_COUNT=0
FAIL_COUNT=0

while IFS= read -r rel_path; do
    [ -z "$rel_path" ] && continue
    expected="$SNAPSHOT_STASH/$rel_path"
    actual="$REPO_DIR/$rel_path"

    if [ ! -f "$expected" ]; then
        continue
    fi

    if [ ! -f "$actual" ]; then
        echo "SNAPSHOT_FAIL $rel_path (not regenerated by build)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    elif diff -q "$expected" "$actual" > /dev/null 2>&1; then
        echo "SNAPSHOT_PASS $rel_path"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo "SNAPSHOT_FAIL $rel_path (content mismatch)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done < "$SNAPSHOT_STASH/.file_list"

echo "snapshot_verify: $PASS_COUNT passed, $FAIL_COUNT failed"
""",
            ),
            File(
                ".",
                "fix_graceful_fs.sh",
                """#!/bin/bash
# Fix graceful-fs polyfill crash on Node 12 with pnpm 3.x
# The bug: "if (cb) cb.apply(this, arguments)" fails when cb is truthy but not a function
# The fix: check typeof cb === "function" instead
find /root/.rush -path '*/graceful-fs/polyfills.js' -type f 2>/dev/null | while read -r f; do
    sed -i 's/if (cb) cb.apply(this, arguments)/if (typeof cb === "function") cb.apply(this, arguments)/g' "$f"
done
""",
            ),
            File(
                ".",
                "fix_rush_link.sh",
                """#!/bin/bash
# Rush 5.17.2 + pnpm 3.1.1 linking bug workarounds:
# 1. If rush-link.json is missing, create a minimal one
# 2. Create missing shrinkwrap-deps.json for all projects so rush build doesn't fail
REPO="/home/{repo_dir}"
LINK_FILE="$REPO/common/temp/rush-link.json"
if [ ! -f "$LINK_FILE" ]; then
    echo '{{"localLinks": {{}}}}' > "$LINK_FILE"
fi
for proj_dir in $(find "$REPO" -name "rush.json" -path "$REPO/rush.json" -prune -o -name "package.json" -print 2>/dev/null | xargs -I{{}} dirname {{}}); do
    rush_temp="$proj_dir/.rush/temp"
    if [ ! -f "$rush_temp/shrinkwrap-deps.json" ]; then
        mkdir -p "$rush_temp"
        echo '{{}}' > "$rush_temp/shrinkwrap-deps.json"
    fi
done
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Disable strict peer deps in rush.json - the CLI flag overrides .npmrc
sed -i 's/"strictPeerDependencies": true/"strictPeerDependencies": false/' rush.json

# Upgrade pnpm 3.1.1 -> 3.8.0 to fix linking bug ("Cannot find installed dependency")
sed -i 's/"pnpmVersion": "3\\.1\\.1"/"pnpmVersion": "3.8.0"/' rush.json

# Remove nodeSupportedVersionRange so Rush doesn't reject the container's Node version
sed -i '/"nodeSupportedVersionRange"/d' rush.json

node common/scripts/install-run-rush.js install --bypass-policy || true
bash /home/fix_graceful_fs.sh
rm -rf common/temp/node_modules
node common/scripts/install-run-rush.js install --bypass-policy || true
bash /home/fix_rush_link.sh

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}

node common/scripts/install-run-rush.js install --bypass-policy || true
bash /home/fix_graceful_fs.sh
rm -rf common/temp/node_modules
node common/scripts/install-run-rush.js install --bypass-policy || true
bash /home/fix_rush_link.sh

bash /home/snapshot_save.sh /home/test.patch /home/{repo_dir}

PROJECTS=$(bash /home/detect_projects.sh /home/test.patch)
if [ -z "$PROJECTS" ]; then
    node common/scripts/install-run-rush.js build 2>&1 || true
else
    for proj in $PROJECTS; do
        node common/scripts/install-run-rush.js build --to "$proj" 2>&1 || true
    done
fi

bash /home/snapshot_verify.sh /home/{repo_dir}

PROJECT_DIRS=$(bash /home/detect_project_dirs.sh /home/test.patch)
if [ -n "$PROJECT_DIRS" ]; then
    for pdir in $PROJECT_DIRS; do
        bash /home/run_jest.sh "$pdir"
    done
fi
""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
sed -i 's/"strictPeerDependencies": true/"strictPeerDependencies": false/' rush.json
sed -i 's/"pnpmVersion": "3\\.1\\.1"/"pnpmVersion": "3.8.0"/' rush.json
sed -i '/"nodeSupportedVersionRange"/d' rush.json
node common/scripts/install-run-rush.js install --bypass-policy || true
bash /home/fix_graceful_fs.sh
rm -rf common/temp/node_modules
node common/scripts/install-run-rush.js install --bypass-policy || true
bash /home/fix_rush_link.sh

bash /home/snapshot_save.sh /home/test.patch /home/{repo_dir}

PROJECTS=$(bash /home/detect_projects.sh /home/test.patch)
if [ -z "$PROJECTS" ]; then
    node common/scripts/install-run-rush.js build 2>&1 || true
else
    for proj in $PROJECTS; do
        node common/scripts/install-run-rush.js build --to "$proj" 2>&1 || true
    done
fi

bash /home/snapshot_verify.sh /home/{repo_dir}

PROJECT_DIRS=$(bash /home/detect_project_dirs.sh /home/test.patch)
if [ -n "$PROJECT_DIRS" ]; then
    for pdir in $PROJECT_DIRS; do
        bash /home/run_jest.sh "$pdir"
    done
fi

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
sed -i 's/"strictPeerDependencies": true/"strictPeerDependencies": false/' rush.json
sed -i 's/"pnpmVersion": "3\\.1\\.1"/"pnpmVersion": "3.8.0"/' rush.json
sed -i '/"nodeSupportedVersionRange"/d' rush.json
node common/scripts/install-run-rush.js install --bypass-policy || true
bash /home/fix_graceful_fs.sh
rm -rf common/temp/node_modules
node common/scripts/install-run-rush.js install --bypass-policy || true
bash /home/fix_rush_link.sh

bash /home/snapshot_save.sh /home/test.patch /home/{repo_dir}

PROJECTS=$(bash /home/detect_projects.sh /home/test.patch)
if [ -z "$PROJECTS" ]; then
    node common/scripts/install-run-rush.js build 2>&1 || true
else
    for proj in $PROJECTS; do
        node common/scripts/install-run-rush.js build --to "$proj" 2>&1 || true
    done
fi

bash /home/snapshot_verify.sh /home/{repo_dir}

PROJECT_DIRS=$(bash /home/detect_project_dirs.sh /home/test.patch)
if [ -n "$PROJECT_DIRS" ]; then
    for pdir in $PROJECT_DIRS; do
        bash /home/run_jest.sh "$pdir"
    done
fi

""".format(pr=self.pr, repo_dir=REPO_DIR),
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


@Instance.register("microsoft", "rushstack_3565_to_1485")
class rushstack_3565_to_1485(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return rushstack_3565_to_1485_ImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        # Standard Jest output (Era A - node:14, gulp-core-build + Jest 23):
        #  PASS  lib/api/test/VersionMismatchFinder.test.js
        #  FAIL  lib/some/test.js
        re_pass_file = re.compile(r"^\s*PASS\s+(.+?)\s*$")
        re_fail_file = re.compile(r"^\s*FAIL\s+(.+?)\s*$")

        # Jest verbose individual test lines
        re_pass_test = re.compile(r"^\s*[✓✔√]\s+(.*?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_fail_test = re.compile(r"^\s*[✕✘×]\s+(.*?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_skip_test = re.compile(r"^\s*○\s+skipped\s+(.*?)$")

        re_snapshot_pass = re.compile(r"^SNAPSHOT_PASS\s+(.+?)(?:\s*\(.*\))?\s*$")
        re_snapshot_fail = re.compile(r"^SNAPSHOT_FAIL\s+(.+?)(?:\s*\(.*\))?\s*$")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line)
            cleaned = line.strip()
            if not cleaned:
                continue

            m = re_fail_file.match(cleaned)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_pass_file.match(cleaned)
            if m:
                test_name = m.group(1).strip()
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            m = re_fail_test.match(cleaned)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_pass_test.match(cleaned)
            if m:
                test_name = m.group(1).strip()
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            m = re_skip_test.match(cleaned)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

            m = re_snapshot_pass.match(cleaned)
            if m:
                test_name = f"snapshot:{m.group(1).strip()}"
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            m = re_snapshot_fail.match(cleaned)
            if m:
                test_name = f"snapshot:{m.group(1).strip()}"
                failed_tests.add(test_name)
                passed_tests.discard(test_name)
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
