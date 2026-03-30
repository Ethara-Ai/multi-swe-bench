from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class StdlibImageBase(Image):
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


class StdlibImageDefault(Image):
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
        return StdlibImageBase(self.pr, self._config)

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

# Remove @stdlib git dependencies that fail inside Docker (no SSH keys).
# npm converts git+https://github.com/... to ssh://git@github.com/... which
# requires authentication. These deps are unnecessary — tests resolve @stdlib/*
# from the monorepo's own lib/node_modules/@stdlib/ via Node module resolution.
node -e "
  var pkg = JSON.parse(require('fs').readFileSync('package.json', 'utf8'));
  var deps = pkg.dependencies || {{}};
  var removed = 0;
  Object.keys(deps).forEach(function(k) {{
    if (k.startsWith('@stdlib/') && deps[k].indexOf('git+') !== -1) {{
      delete deps[k];
      removed++;
    }}
  }});
  if (removed > 0) {{
    require('fs').writeFileSync('package.json', JSON.stringify(pkg, null, 2) + '\\n');
    console.log('Removed ' + removed + ' @stdlib git dependencies from package.json');
  }}
"

npm install --ignore-scripts || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Extract test files from test.patch (only test/*.js files under @stdlib modules)
# Exclude native/module/cli tests that require node-gyp or special setup
TEST_FILES=$(grep -oP '(?<=diff --git a/)lib/node_modules/@stdlib/[^\\s]*/test/test[^\\s]*\\.js' /home/test.patch 2>/dev/null | \\
  grep -v 'test\\.native\\|test\\.module\\|test\\.cli' | sort -u)

# Only run test files that already exist at the base commit (before patches)
EXISTING_FILES=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        EXISTING_FILES="$EXISTING_FILES $f"
    fi
done

if [ -n "$EXISTING_FILES" ]; then
    for f in $EXISTING_FILES; do
        echo "### FILE: $f"
        node "$f" 2>&1 || true
    done
else
    echo "No pre-existing test files found in test patch - all tests are new"
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package.json --whitespace=nowarn /home/test.patch

# Extract test files from test.patch
TEST_FILES=$(grep -oP '(?<=diff --git a/)lib/node_modules/@stdlib/[^\\s]*/test/test[^\\s]*\\.js' /home/test.patch 2>/dev/null | \\
  grep -v 'test\\.native\\|test\\.module\\|test\\.cli' | sort -u)

if [ -n "$TEST_FILES" ]; then
    for f in $TEST_FILES; do
        echo "### FILE: $f"
        node "$f" 2>&1 || true
    done
else
    echo "No test files found in test patch"
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package.json --whitespace=nowarn /home/test.patch /home/fix.patch

# Extract test files from test.patch
TEST_FILES=$(grep -oP '(?<=diff --git a/)lib/node_modules/@stdlib/[^\\s]*/test/test[^\\s]*\\.js' /home/test.patch 2>/dev/null | \\
  grep -v 'test\\.native\\|test\\.module\\|test\\.cli' | sort -u)

if [ -n "$TEST_FILES" ]; then
    for f in $TEST_FILES; do
        echo "### FILE: $f"
        node "$f" 2>&1 || true
    done
else
    echo "No test files found in test patch"
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


@Instance.register("stdlib-js", "stdlib")
class Stdlib(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StdlibImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # TAP line format: "ok 1 description" or "not ok 2 description # SKIP reason"
        test_pattern = re.compile(
            r"^(ok|not ok) (\d+) (.*?)(?: #.*)?$"
        )

        lines = test_log.split("\n")
        tap_started = False
        current_test_name = None
        current_file = None

        for line in lines:
            line = line.strip()

            # Track file markers emitted by shell scripts
            if line.startswith("### FILE: "):
                current_file = line[len("### FILE: "):].strip()
                continue

            if line.startswith("TAP version 13"):
                tap_started = True
                current_test_name = None
                continue

            if not tap_started:
                continue

            # Plan line marks end of TAP block; reset for multi-file concatenated output
            if line.startswith("1.."):
                tap_started = False
                current_test_name = None
                continue

            if line.startswith("# "):
                current_test_name = line[2:].strip()

            elif line.startswith(("ok", "not ok")):
                match = test_pattern.match(line)
                if match:
                    status, test_num, line_test_name = match.groups()
                    line_test_name = line_test_name.strip()

                    # Build unique test name: [file] > [suite]: num description
                    if current_test_name:
                        base_name = f"{current_test_name}: {test_num} {line_test_name}"
                    else:
                        base_name = f"{test_num} {line_test_name}"

                    test_name = (
                        f"[{current_file}] > {base_name}"
                        if current_file
                        else base_name
                    )

                    if "# SKIP" in line:
                        skipped_tests.add(test_name)
                        continue

                    if status == "ok":
                        passed_tests.add(test_name)
                    elif status == "not ok":
                        failed_tests.add(test_name)

        # Deduplication: failed takes priority over passed, skipped takes priority over passed
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
