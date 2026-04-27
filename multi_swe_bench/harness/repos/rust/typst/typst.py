import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TypstImageBase(Image):
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


class TypstImageDefault(Image):
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
        return TypstImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        return [
            File(
                ".",
                "fix.patch",
                f"{pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{pr.test_patch}",
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

cd /home/{pr_repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Fetch PR head ref for binary patch recovery
git fetch origin refs/pull/{pr_number}/head:refs/pr_head 2>/dev/null || true
PR_HEAD=$(git rev-parse refs/pr_head 2>/dev/null || true)
if [ -n "$PR_HEAD" ]; then
    echo "$PR_HEAD" > /home/pr_head_sha
    echo "prepare: fetched PR head: $PR_HEAD"
else
    echo "prepare: WARNING - could not fetch PR head ref"
fi

# Use rust-version from Cargo.toml if available
RUST_VER=$(grep -m1 'rust-version' Cargo.toml | sed 's/.*= *"\\(.*\\)".*/\\1/' || true)
if [ -n "$RUST_VER" ]; then
    rustup install "$RUST_VER"
    rustup default "$RUST_VER"
fi

cargo test --workspace || true

""".format(pr_repo=pr.repo, base_sha=pr.base.sha, pr_number=pr.number),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr_repo}
cargo test --workspace

""".format(pr_repo=pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr_repo}

# Apply test patch with binary file recovery
apply_patch() {{
    local PATCH="$1"
    if git apply "$PATCH" 2>/dev/null; then
        return 0
    fi

    echo "Normal apply failed for $PATCH, handling binary files..."
    PR_HEAD_SHA=$(cat /home/pr_head_sha 2>/dev/null || true)
    if [ -z "$PR_HEAD_SHA" ]; then
        echo "No PR head SHA, cannot fix binary patches"
        exit 1
    fi

    # Extract binary file paths
    BINARY_FILES=()
    DELETED_FILES=()
    while IFS= read -r line; do
        if echo "$line" | grep -q "Binary files .* and /dev/null differ"; then
            fpath=$(echo "$line" | sed 's|Binary files a/\(.*\) and /dev/null differ|\1|')
            DELETED_FILES+=("$fpath")
        elif echo "$line" | grep -q "Binary files .* and b/.* differ"; then
            fpath=$(echo "$line" | sed 's|.*and b/\(.*\) differ|\1|')
            BINARY_FILES+=("$fpath")
        fi
    done < <(grep "Binary files" "$PATCH")

    # Filter out binary sections and apply text-only hunks
    python3 -c "
import sys
lines = open(sys.argv[1]).readlines()
out, skip = [], False
for i, line in enumerate(lines):
    if line.startswith('diff --git'):
        skip = False
        for j in range(i+1, min(i+5, len(lines))):
            if 'Binary files' in lines[j]: skip = True; break
            if lines[j].startswith('diff --git'): break
    if not skip: out.append(line)
open(sys.argv[1]+'.txt','w').writelines(out)
" "$PATCH"
    [ -s "${{PATCH}}.txt" ] && git apply "${{PATCH}}.txt" 2>/dev/null || true
    rm -f "${{PATCH}}.txt"

    for fpath in "${{BINARY_FILES[@]}}"; do
        git checkout "$PR_HEAD_SHA" -- "$fpath" 2>/dev/null || echo "WARNING: could not checkout $fpath"
    done
    for fpath in "${{DELETED_FILES[@]}}"; do
        rm -f "$fpath"
    done
}}

apply_patch /home/test.patch
cargo test --workspace

""".format(pr_repo=pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr_repo}

# Apply patches with binary file recovery
apply_patch() {{
    local PATCH="$1"
    if git apply "$PATCH" 2>/dev/null; then
        return 0
    fi

    echo "Normal apply failed for $PATCH, handling binary files..."
    PR_HEAD_SHA=$(cat /home/pr_head_sha 2>/dev/null || true)
    if [ -z "$PR_HEAD_SHA" ]; then
        echo "No PR head SHA, cannot fix binary patches"
        exit 1
    fi

    BINARY_FILES=()
    DELETED_FILES=()
    while IFS= read -r line; do
        if echo "$line" | grep -q "Binary files .* and /dev/null differ"; then
            fpath=$(echo "$line" | sed 's|Binary files a/\(.*\) and /dev/null differ|\1|')
            DELETED_FILES+=("$fpath")
        elif echo "$line" | grep -q "Binary files .* and b/.* differ"; then
            fpath=$(echo "$line" | sed 's|.*and b/\(.*\) differ|\1|')
            BINARY_FILES+=("$fpath")
        fi
    done < <(grep "Binary files" "$PATCH")

    python3 -c "
import sys
lines = open(sys.argv[1]).readlines()
out, skip = [], False
for i, line in enumerate(lines):
    if line.startswith('diff --git'):
        skip = False
        for j in range(i+1, min(i+5, len(lines))):
            if 'Binary files' in lines[j]: skip = True; break
            if lines[j].startswith('diff --git'): break
    if not skip: out.append(line)
open(sys.argv[1]+'.txt','w').writelines(out)
" "$PATCH"
    [ -s "${{PATCH}}.txt" ] && git apply "${{PATCH}}.txt" 2>/dev/null || true
    rm -f "${{PATCH}}.txt"

    for fpath in "${{BINARY_FILES[@]}}"; do
        git checkout "$PR_HEAD_SHA" -- "$fpath" 2>/dev/null || echo "WARNING: could not checkout $fpath"
    done
    for fpath in "${{DELETED_FILES[@]}}"; do
        rm -f "$fpath"
    done
}}

apply_patch /home/test.patch
apply_patch /home/fix.patch
cargo test --workspace

""".format(pr_repo=pr.repo),
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


@Instance.register("typst", "typst")
class Typst(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TypstImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"test (\S+) ... ok")]
        re_fail_tests = [re.compile(r"test (\S+) ... FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) ... ignored")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
