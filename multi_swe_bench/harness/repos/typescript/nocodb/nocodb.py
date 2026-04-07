import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
RUN apt update && apt install -y git jq
RUN npm install -g pnpm@9 lerna@3

{code}

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self.config)

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
                "strip_binary_diffs.py",
                """#!/usr/bin/env python3
\"\"\"Strip binary diffs from a patch file so git apply doesn't choke on them.\"\"\"
import re
import sys

def strip_binary_diffs(patch_path):
    with open(patch_path, 'r', errors='replace') as f:
        content = f.read()

    # Split into per-file diffs
    diffs = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
    text_diffs = []
    for diff in diffs:
        if not diff.strip():
            continue
        # Skip diffs that contain binary markers
        if 'Binary files' in diff or 'GIT binary patch' in diff:
            continue
        text_diffs.append(diff)

    with open(patch_path, 'w') as f:
        f.write('\\n'.join(text_diffs))

if __name__ == '__main__':
    for path in sys.argv[1:]:
        strip_binary_diffs(path)
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

# Detect package manager
if [ -f pnpm-lock.yaml ]; then
  echo "=== DETECTED: pnpm ==="
  # Remove use-node-version and engine-strict from .npmrc to avoid pnpm issues
  sed -i '/^use-node-version/d' .npmrc 2>/dev/null || true
  sed -i '/^engine-strict/d' .npmrc 2>/dev/null || true

  # Install dependencies (--no-frozen-lockfile for lockfile version compatibility)
  pnpm install --no-frozen-lockfile || pnpm bootstrap || true

  # Build nocodb-sdk (required by nocodb backend for mocha tests)
  if [ -d packages/nocodb-sdk ]; then
    cd packages/nocodb-sdk
    pnpm run generate:sdk || true
    pnpm run build:main || true
    cd /home/{pr.repo}
  fi
elif [ -f lerna.json ]; then
  echo "=== DETECTED: npm + lerna ==="
  npm install || true
  npx lerna bootstrap || true

  # Build nocodb-sdk if it exists
  if [ -d packages/nocodb-sdk ]; then
    cd packages/nocodb-sdk
    npm run generate:sdk || true
    npm run build:main || true
    cd /home/{pr.repo}
  fi
else
  echo "=== DETECTED: npm (fallback) ==="
  npm install || true

  if [ -d packages/nocodb-sdk ]; then
    cd packages/nocodb-sdk
    npm run generate:sdk || true
    npm run build:main || true
    cd /home/{pr.repo}
  fi
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

# Detect package manager
if [ -f pnpm-lock.yaml ]; then
  PKG_RUN="pnpm run"
else
  PKG_RUN="npm run"
fi

EXIT_CODE=0
set +e

if [ -f packages/nocodb/package.json ]; then
  echo "=== RUNNING JEST TESTS ==="
  (cd packages/nocodb && $PKG_RUN test 2>&1) || EXIT_CODE=$?
fi

if [ -f packages/nocodb/package.json ]; then
  echo "=== RUNNING MOCHA TESTS ==="
  (cd packages/nocodb && $PKG_RUN test:unit 2>&1) || EXIT_CODE=$?
fi

if [ -f packages/nocodb-sdk/package.json ]; then
  echo "=== RUNNING SDK TESTS ==="
  (cd packages/nocodb-sdk && $PKG_RUN test:unit 2>&1) || EXIT_CODE=$?
fi

exit $EXIT_CODE

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

# Detect package manager
if [ -f pnpm-lock.yaml ]; then
  PKG_RUN="pnpm run"
  APPLY_EXCLUDE="--exclude pnpm-lock.yaml"
else
  PKG_RUN="npm run"
  APPLY_EXCLUDE=""
fi

# Strip binary diffs that can't be applied (short index hashes)
python3 /home/strip_binary_diffs.py /home/test.patch

git apply $APPLY_EXCLUDE --whitespace=nowarn /home/test.patch

EXIT_CODE=0
set +e

if [ -f packages/nocodb/package.json ]; then
  echo "=== RUNNING JEST TESTS ==="
  (cd packages/nocodb && $PKG_RUN test 2>&1) || EXIT_CODE=$?
fi

if [ -f packages/nocodb/package.json ]; then
  echo "=== RUNNING MOCHA TESTS ==="
  (cd packages/nocodb && $PKG_RUN test:unit 2>&1) || EXIT_CODE=$?
fi

if [ -f packages/nocodb-sdk/package.json ]; then
  echo "=== RUNNING SDK TESTS ==="
  (cd packages/nocodb-sdk && $PKG_RUN test:unit 2>&1) || EXIT_CODE=$?
fi

exit $EXIT_CODE

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

# Detect package manager
if [ -f pnpm-lock.yaml ]; then
  PKG_RUN="pnpm run"
  APPLY_EXCLUDE="--exclude pnpm-lock.yaml"
else
  PKG_RUN="npm run"
  APPLY_EXCLUDE=""
fi

# Strip binary diffs that can't be applied (short index hashes)
python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch

git apply $APPLY_EXCLUDE --whitespace=nowarn /home/test.patch /home/fix.patch

EXIT_CODE=0
set +e

if [ -f packages/nocodb/package.json ]; then
  echo "=== RUNNING JEST TESTS ==="
  (cd packages/nocodb && $PKG_RUN test 2>&1) || EXIT_CODE=$?
fi

if [ -f packages/nocodb/package.json ]; then
  echo "=== RUNNING MOCHA TESTS ==="
  (cd packages/nocodb && $PKG_RUN test:unit 2>&1) || EXIT_CODE=$?
fi

if [ -f packages/nocodb-sdk/package.json ]; then
  echo "=== RUNNING SDK TESTS ==="
  (cd packages/nocodb-sdk && $PKG_RUN test:unit 2>&1) || EXIT_CODE=$?
fi

exit $EXIT_CODE

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


class ImageBaseXmysql(Image):
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
        return "node:18"

    def image_tag(self) -> str:
        return "base-xmysql"

    def workdir(self) -> str:
        return "base-xmysql"

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
RUN apt update && apt install -y git default-mysql-server default-mysql-client

{code}

{self.clear_env}

"""


class ImageDefaultXmysql(Image):
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
        return ImageBaseXmysql(self.pr, self.config)

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
                "strip_binary_diffs.py",
                """#!/usr/bin/env python3
\"\"\"Strip binary diffs from a patch file so git apply doesn't choke on them.\"\"\"
import re
import sys

def strip_binary_diffs(patch_path):
    with open(patch_path, 'r', errors='replace') as f:
        content = f.read()

    # Split into per-file diffs
    diffs = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
    text_diffs = []
    for diff in diffs:
        if not diff.strip():
            continue
        # Skip diffs that contain binary markers
        if 'Binary files' in diff or 'GIT binary patch' in diff:
            continue
        text_diffs.append(diff)

    with open(patch_path, 'w') as f:
        f.write('\\n'.join(text_diffs))

if __name__ == '__main__':
    for path in sys.argv[1:]:
        strip_binary_diffs(path)
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
npm install --ignore-scripts || true
npm install -g mocha || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

# Start MySQL
service mysql start || mysqld_safe &
sleep 3

# Create test database and user
mysql -u root -e "CREATE DATABASE IF NOT EXISTS test;" || true
mysql -u root -e "CREATE USER IF NOT EXISTS 'root'@'localhost';" || true

cd /home/{pr.repo}
npx mocha tests/*.js --exit

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

# Strip binary diffs that can't be applied (short index hashes)
python3 /home/strip_binary_diffs.py /home/test.patch

git apply --whitespace=nowarn /home/test.patch

# Start MySQL
service mysql start || mysqld_safe &
sleep 3

mysql -u root -e "CREATE DATABASE IF NOT EXISTS test;" || true

npx mocha tests/*.js --exit

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

# Strip binary diffs that can't be applied (short index hashes)
python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Start MySQL
service mysql start || mysqld_safe &
sleep 3

mysql -u root -e "CREATE DATABASE IF NOT EXISTS test;" || true

npx mocha tests/*.js --exit

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


@Instance.register("nocodb", "nocodb")
class NocoDB(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 21:
            return ImageDefaultXmysql(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        jest_pass_re = re.compile(r"^PASS:?\s+(.+?)(?:\s+\(\d+(\.\d+)?\s*s\))?$")
        jest_fail_re = re.compile(r"^FAIL:?\s+(.+?)(?:\s+\(\d+(\.\d+)?\s*s\))?$")

        mocha_pass_re = re.compile(r"^(\s*)[✓✔]\s+(.+?)(?:\s+\(\d+\s*ms\))?\s*$")
        mocha_fail_re = re.compile(r"^(\s*)[×✗✕]\s+(.+?)(?:\s+\(\d+\s*ms\))?\s*$")
        mocha_skip_re = re.compile(r"^(\s*)-\s+(.+)$")
        skip_keyword_re = re.compile(r"SKIP:?\s+(.+?)\s")

        # Mocha describe context: track suite nesting by indent level
        # Mocha spec output: describe blocks are plain text lines, tests are indented with markers
        describe_stack: list[tuple[int, str]] = []
        mocha_test_re = re.compile(r"^(\s*)[✓✔×✗✕-]\s+")

        def _get_describe_prefix(indent: int) -> str:
            parts = [name for lvl, name in describe_stack if lvl < indent]
            return " > ".join(parts) + " > " if parts else ""

        def _update_describe_stack(line: str, stripped: str) -> None:
            if not stripped:
                return
            if mocha_test_re.match(line):
                return
            if jest_pass_re.match(stripped) or jest_fail_re.match(stripped):
                return
            if skip_keyword_re.match(stripped):
                return
            if stripped.startswith("=== "):
                describe_stack.clear()
                return
            indent = len(line) - len(line.lstrip())
            if indent == 0:
                describe_stack.clear()
                return
            while describe_stack and describe_stack[-1][0] >= indent:
                describe_stack.pop()
            if re.match(r"^[A-Za-z]", stripped):
                describe_stack.append((indent, stripped))

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line)
            line = line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            _update_describe_stack(line, stripped)

            m = jest_pass_re.match(stripped)
            if m and m.group(1) not in failed_tests:
                passed_tests.add(m.group(1))
                continue

            m = jest_fail_re.match(stripped)
            if m:
                failed_tests.add(m.group(1))
                passed_tests.discard(m.group(1))
                continue

            indent = len(line) - len(line.lstrip())

            m = mocha_pass_re.match(line)
            if m:
                name = _get_describe_prefix(indent) + m.group(2)
                if name not in failed_tests:
                    passed_tests.add(name)
                continue

            m = mocha_fail_re.match(line)
            if m:
                name = _get_describe_prefix(indent) + m.group(2)
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

            m = mocha_skip_re.match(line)
            if m:
                name = _get_describe_prefix(indent) + m.group(2)
                skipped_tests.add(name)
                continue

            m = skip_keyword_re.match(stripped)
            if m:
                skipped_tests.add(m.group(1))
                continue

        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
