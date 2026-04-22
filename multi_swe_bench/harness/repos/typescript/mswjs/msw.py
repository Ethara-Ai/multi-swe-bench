import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Era boundaries (by PR number)
#
# Group 1: PRs #60-#607   — yarn + jest --runInBand           (node:18)
# Group 2: PRs #1257-#1375 — yarn + jest --maxWorkers=3       (node:18)
# Group 3: PR  #1436       — pnpm + jest --maxWorkers=3       (node:18)
# Group 4: PRs #2000-#2578 — pnpm + vitest run               (node:18)
# Group 5: PR  #2669       — pnpm + vitest run               (node:20)
#
# Node split: PRs #60-#2578 on node:18, PR #2669 on node:20
# Package manager split: PRs #60-#1375 yarn, PRs #1436-#2669 pnpm
# Test runner split: PRs #60-#1436 jest, PRs #2000-#2669 vitest
# ---------------------------------------------------------------------------


class ImageBase18(Image):
    """Base image for PRs #60 through #2578 (node:18)."""

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
RUN apt update && apt install -y git
RUN corepack enable && corepack prepare yarn@1.22.22 --activate

{code}

{self.clear_env}

"""


class ImageBase20(Image):
    """Base image for PR #2669+ (node:20)."""

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
        return "node:20"

    def image_tag(self) -> str:
        return "base-node20"

    def workdir(self) -> str:
        return "base-node20"

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
RUN apt update && apt install -y git

{code}

{self.clear_env}

"""


# ---------------------------------------------------------------------------
# ImageDefault: Yarn + Jest --runInBand (PRs #60-#607)
# ---------------------------------------------------------------------------

class ImageDefaultYarnJestRunInBand(Image):

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
        return ImageBase18(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git fetch origin {pr.base.sha} || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
yarn install --frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn jest --runInBand

set +e

TEST_D_FILES=$(find src -name '*.test-d.ts' 2>/dev/null || true)
if [ -n "$TEST_D_FILES" ]; then
  TSC_OUT=$(npx tsc --noEmit --strict 2>&1 || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "^$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch
yarn install || true
yarn jest --runInBand

set +e

TEST_D_FILES=$(find src -name '*.test-d.ts' 2>/dev/null || true)
if [ -n "$TEST_D_FILES" ]; then
  TSC_OUT=$(npx tsc --noEmit --strict 2>&1 || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "^$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch
yarn install || true
yarn jest --runInBand

set +e

TEST_D_FILES=$(find src -name '*.test-d.ts' 2>/dev/null || true)
if [ -n "$TEST_D_FILES" ]; then
  TSC_OUT=$(npx tsc --noEmit --strict 2>&1 || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "^$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
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


# ---------------------------------------------------------------------------
# ImageDefault: Yarn + Jest --maxWorkers=3 (PRs #1257-#1375)
# ---------------------------------------------------------------------------

class ImageDefaultYarnJestMaxWorkers(Image):

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
        return ImageBase18(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git fetch origin {pr.base.sha} || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
yarn install --frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn jest --maxWorkers=3

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch
yarn install || true
yarn jest --maxWorkers=3

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch
yarn install || true
yarn jest --maxWorkers=3

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


# ---------------------------------------------------------------------------
# ImageDefault: pnpm + Jest --maxWorkers=3 (PR #1436)
# ---------------------------------------------------------------------------

class ImageDefaultPnpmJest(Image):

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
        return ImageBase18(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git fetch origin {pr.base.sha} || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
corepack enable
pnpm install --no-frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm jest --maxWorkers=3

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch
pnpm install --no-frozen-lockfile || true
pnpm jest --maxWorkers=3

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm install --no-frozen-lockfile || true
pnpm jest --maxWorkers=3

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


# ---------------------------------------------------------------------------
# ImageDefault: pnpm + Vitest (PRs #2000-#2578, node:18)
# ---------------------------------------------------------------------------

class ImageDefaultPnpmVitest18(Image):
    """Default image for PRs #2000-#2578 (pnpm + vitest, node:18)."""

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
        return ImageBase18(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        return [
            File(".", "fix.patch", pr.fix_patch),
            File(".", "test.patch", pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e
if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: Uncommitted changes detected"
    git diff
    exit 1
fi
echo "check_git_changes: No uncommitted changes"
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
git fetch origin {pr.base.sha} || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
corepack enable
pnpm install --no-frozen-lockfile || true
pnpm build || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm vitest run

set +e

VITEST_NODE_CONFIG=$(ls test/node/vitest.config.ts test/node/vitest.config.mts 2>/dev/null | head -1)
if [ -n "$VITEST_NODE_CONFIG" ]; then
  pnpm vitest run --config="$VITEST_NODE_CONFIG"
fi

if node -e "var p=require('./package.json'); process.exit(p.scripts && p.scripts['test:ts'] ? 0 : 1)" 2>/dev/null; then
  TSC_OUT=$(pnpm test:ts run 2>&1 || true)
  TEST_D_FILES=$(find src test -name '*.test-d.ts' 2>/dev/null || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch
pnpm install --no-frozen-lockfile || true
pnpm build || true
pnpm vitest run

set +e

VITEST_NODE_CONFIG=$(ls test/node/vitest.config.ts test/node/vitest.config.mts 2>/dev/null | head -1)
if [ -n "$VITEST_NODE_CONFIG" ]; then
  pnpm vitest run --config="$VITEST_NODE_CONFIG"
fi

if node -e "var p=require('./package.json'); process.exit(p.scripts && p.scripts['test:ts'] ? 0 : 1)" 2>/dev/null; then
  TSC_OUT=$(pnpm test:ts run 2>&1 || true)
  TEST_D_FILES=$(find src test -name '*.test-d.ts' 2>/dev/null || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm install --no-frozen-lockfile || true
pnpm build || true
pnpm vitest run

set +e

VITEST_NODE_CONFIG=$(ls test/node/vitest.config.ts test/node/vitest.config.mts 2>/dev/null | head -1)
if [ -n "$VITEST_NODE_CONFIG" ]; then
  pnpm vitest run --config="$VITEST_NODE_CONFIG"
fi

if node -e "var p=require('./package.json'); process.exit(p.scripts && p.scripts['test:ts'] ? 0 : 1)" 2>/dev/null; then
  TSC_OUT=$(pnpm test:ts run 2>&1 || true)
  TEST_D_FILES=$(find src test -name '*.test-d.ts' 2>/dev/null || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
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


# ---------------------------------------------------------------------------
# ImageDefault: pnpm + Vitest (PR #2669+, node:20)
# ---------------------------------------------------------------------------

class ImageDefaultPnpmVitest20(Image):

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
        return ImageBase20(self.pr, self.config)

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
git fetch origin {pr.base.sha} || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
corepack enable
pnpm install --no-frozen-lockfile || true
pnpm build || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm vitest run

set +e

VITEST_NODE_CONFIG=$(ls test/node/vitest.config.ts test/node/vitest.config.mts 2>/dev/null | head -1)
if [ -n "$VITEST_NODE_CONFIG" ]; then
  pnpm vitest run --config="$VITEST_NODE_CONFIG"
fi

if node -e "var p=require('./package.json'); process.exit(p.scripts && p.scripts['test:ts'] ? 0 : 1)" 2>/dev/null; then
  TSC_OUT=$(pnpm test:ts run 2>&1 || true)
  TEST_D_FILES=$(find src test -name '*.test-d.ts' 2>/dev/null || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch
pnpm install --no-frozen-lockfile || true
pnpm build || true
pnpm vitest run

set +e

VITEST_NODE_CONFIG=$(ls test/node/vitest.config.ts test/node/vitest.config.mts 2>/dev/null | head -1)
if [ -n "$VITEST_NODE_CONFIG" ]; then
  pnpm vitest run --config="$VITEST_NODE_CONFIG"
fi

if node -e "var p=require('./package.json'); process.exit(p.scripts && p.scripts['test:ts'] ? 0 : 1)" 2>/dev/null; then
  TSC_OUT=$(pnpm test:ts run 2>&1 || true)
  TEST_D_FILES=$(find src test -name '*.test-d.ts' 2>/dev/null || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude yarn.lock --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm install --no-frozen-lockfile || true
pnpm build || true
pnpm vitest run

set +e

VITEST_NODE_CONFIG=$(ls test/node/vitest.config.ts test/node/vitest.config.mts 2>/dev/null | head -1)
if [ -n "$VITEST_NODE_CONFIG" ]; then
  pnpm vitest run --config="$VITEST_NODE_CONFIG"
fi

if node -e "var p=require('./package.json'); process.exit(p.scripts && p.scripts['test:ts'] ? 0 : 1)" 2>/dev/null; then
  TSC_OUT=$(pnpm test:ts run 2>&1 || true)
  TEST_D_FILES=$(find src test -name '*.test-d.ts' 2>/dev/null || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
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


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

@Instance.register("mswjs", "msw")
class Msw(Instance):
    _YARN_ENSURE = "yarn install || true"
    _PNPM_ENSURE = "pnpm install --no-frozen-lockfile || true"
    _LOCKFILE_EXCLUDE = "--exclude yarn.lock --exclude pnpm-lock.yaml"

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def _group(self) -> int:
        """Route PR to its toolchain group."""
        n = self.pr.number
        if n <= 607:
            return 1  # yarn + jest --runInBand
        elif n <= 1375:
            return 2  # yarn + jest --maxWorkers=3
        elif n <= 1436:
            return 3  # pnpm + jest --maxWorkers=3
        elif n <= 2578:
            return 4  # pnpm + vitest (node:18)
        else:
            return 5  # pnpm + vitest (node:20)

    def dependency(self) -> Optional[Image]:
        group = self._group()
        if group == 1:
            return ImageDefaultYarnJestRunInBand(self.pr, self._config)
        elif group == 2:
            return ImageDefaultYarnJestMaxWorkers(self.pr, self._config)
        elif group == 3:
            return ImageDefaultPnpmJest(self.pr, self._config)
        elif group == 4:
            return ImageDefaultPnpmVitest18(self.pr, self._config)
        else:
            return ImageDefaultPnpmVitest20(self.pr, self._config)

    def _install_ensure(self) -> str:
        group = self._group()
        if group <= 2:
            return self._YARN_ENSURE
        return self._PNPM_ENSURE

    def _test_cmd(self) -> str:
        group = self._group()
        if group == 1:
            return "yarn jest --runInBand"
        elif group == 2:
            return "yarn jest --maxWorkers=3"
        elif group == 3:
            return "pnpm jest --maxWorkers=3"
        else:
            return "pnpm vitest run"

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        re_tsc_error = re.compile(r"(\S+\.test-d\.(?:tsx|ts))\(\d+,\d+\):\s*error\s+TS\d+")

        group = self._group()

        if group <= 3:
            re_pass = re.compile(r"PASS\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")
            re_fail = re.compile(r"FAIL\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue

                pass_match = re_pass.search(clean)
                if pass_match:
                    passed_tests.add(pass_match.group(1))
                    continue

                fail_match = re_fail.search(clean)
                if fail_match:
                    failed_tests.add(fail_match.group(1))
                    continue

                tsc_match = re_tsc_error.search(clean)
                if tsc_match:
                    failed_tests.add(tsc_match.group(1))

        else:
            re_pass = re.compile(r"(?:PASS|[✓✔])\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")
            re_fail = re.compile(r"(?:FAIL|[❯×✗])\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")
            re_skip = re.compile(r"[↓⊘]\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue

                pass_match = re_pass.search(clean)
                if pass_match:
                    passed_tests.add(pass_match.group(1))
                    continue

                fail_match = re_fail.search(clean)
                if fail_match:
                    failed_tests.add(fail_match.group(1))
                    continue

                skip_match = re_skip.search(clean)
                if skip_match:
                    skipped_tests.add(skip_match.group(1))
                    continue

                tsc_match = re_tsc_error.search(clean)
                if tsc_match:
                    failed_tests.add(tsc_match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
