import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ICU4XImageBase(Image):
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
        return "rust:latest"

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

WORKDIR /home/

{code}

{self.clear_env}

"""


class ICU4XImageDefault(Image):
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
        return ICU4XImageBase(self.pr, self.config)

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
                "find_crate_root.sh",
                """#!/bin/bash
# find_crate_root.sh <file_path>
# Walks up from file_path to find the nearest Cargo.toml, prints that directory.
# Falls back to repo root if no Cargo.toml found above the file.
DIR="$1"
REPO_ROOT="/home/{pr.repo}"

# If DIR is a file path, get its directory
if [ -f "$REPO_ROOT/$DIR" ]; then
  DIR=$(dirname "$DIR")
fi

while [ "$DIR" != "." ] && [ "$DIR" != "/" ]; do
  if [ -f "$REPO_ROOT/$DIR/Cargo.toml" ]; then
    echo "$DIR"
    exit 0
  fi
  DIR=$(dirname "$DIR")
done

# Check repo root
if [ -f "$REPO_ROOT/Cargo.toml" ]; then
  echo "."
  exit 0
fi

echo "."
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run_targeted_tests.sh",
                """#!/bin/bash
# run_targeted_tests.sh [patches...]
# Extracts affected Rust crates from patches and runs cargo test -p <crate>
# for each affected crate. Falls back to cargo test --workspace if no crates found.
set -e

REPO_ROOT="/home/{pr.repo}"
cd "$REPO_ROOT"

# Extract file paths from patches
FILES=$(cat "$@" 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | sort -u)

if [ -z "$FILES" ]; then
  echo "No files found in patches, testing workspace..."
  cargo test --workspace 2>&1
  exit $?
fi

# Find unique crate roots from affected files
CRATE_ROOTS=""
for f in $FILES; do
  crate_root=$(bash /home/find_crate_root.sh "$f")
  CRATE_ROOTS="$CRATE_ROOTS $crate_root"
done
CRATE_ROOTS=$(echo "$CRATE_ROOTS" | tr ' ' '\\n' | sort -u | grep -v '^$')

if [ -z "$CRATE_ROOTS" ]; then
  echo "No crate roots found, testing workspace..."
  cargo test --workspace 2>&1
  exit $?
fi

# For each crate root, extract the crate name from Cargo.toml and run tests
OVERALL_EXIT=0
TESTED_CRATES=""
for crate_root in $CRATE_ROOTS; do
  if [ "$crate_root" = "." ]; then
    cargo_toml="$REPO_ROOT/Cargo.toml"
  else
    cargo_toml="$REPO_ROOT/$crate_root/Cargo.toml"
  fi

  if [ ! -f "$cargo_toml" ]; then
    echo "Warning: $cargo_toml not found, skipping"
    continue
  fi

  # Extract crate name from [package] section
  crate_name=$(grep -A5 '\\[package\\]' "$cargo_toml" | grep '^name' | head -1 | sed 's/.*=\\s*"//;s/".*//')

  if [ -z "$crate_name" ]; then
    echo "Warning: Could not extract crate name from $cargo_toml, skipping"
    continue
  fi

  # Skip if we already tested this crate (dedup)
  if echo "$TESTED_CRATES" | grep -qw "$crate_name"; then
    continue
  fi
  TESTED_CRATES="$TESTED_CRATES $crate_name"

  echo "=== Testing crate: $crate_name (from $crate_root) ==="
  if ! cargo test -p "$crate_name" --all-features 2>&1; then
    echo "=== cargo test -p $crate_name --all-features failed, retrying without features ==="
    if ! cargo test -p "$crate_name" 2>&1; then
      echo "=== cargo test -p $crate_name failed, retrying with --lib only ==="
      cargo test -p "$crate_name" --lib 2>&1 || OVERALL_EXIT=$?
    fi
  fi
done

if [ -z "$TESTED_CRATES" ]; then
  echo "No testable crates found, testing workspace..."
  cargo test --workspace 2>&1 || OVERALL_EXIT=$?
fi

exit $OVERALL_EXIT
""".format(pr=self.pr),
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

# Warm up: run targeted tests (ignore failures at this stage)
bash /home/run_targeted_tests.sh /home/test.patch /home/fix.patch || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/run_targeted_tests.sh /home/test.patch /home/fix.patch

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_targeted_tests.sh /home/test.patch /home/fix.patch

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_targeted_tests.sh /home/test.patch /home/fix.patch

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


@Instance.register("unicode-org", "icu4x")
class ICU4X(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ICU4XImageDefault(self.pr, self._config)

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

        # Track current crate context for unique doc-test naming
        current_crate = ""

        # Match both regular tests: "test name ... ok"
        # and doc-tests: "test path/file.rs - item::path (line N) ... ok"
        re_pass_tests = [re.compile(r"test (.+) \.\.\. ok$")]
        re_fail_tests = [re.compile(r"test (.+) \.\.\. FAILED$")]
        re_skip_tests = [re.compile(r"test (.+) \.\.\. ignored$")]
        re_crate_marker = re.compile(r"=== Testing crate: (\S+)")
        re_doctest_marker = re.compile(r"\s*Doc-tests (\S+)")

        for line in test_log.splitlines():
            line = line.strip()

            # Track crate context
            crate_match = re_crate_marker.match(line)
            if crate_match:
                current_crate = crate_match.group(1)
                continue
            doctest_match = re_doctest_marker.match(line)
            if doctest_match:
                current_crate = doctest_match.group(1)
                continue

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    test_name = match.group(1)
                    # Prefix with crate name to avoid collisions across crates
                    if current_crate:
                        test_name = f"{current_crate}::{test_name}"
                    passed_tests.add(test_name)

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    test_name = match.group(1)
                    if current_crate:
                        test_name = f"{current_crate}::{test_name}"
                    failed_tests.add(test_name)

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    test_name = match.group(1)
                    if current_crate:
                        test_name = f"{current_crate}::{test_name}"
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
