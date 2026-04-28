from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.24"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass = re.compile(r"--- PASS: (\S+)")
    re_fail = [
        re.compile(r"--- FAIL: (\S+)"),
        re.compile(r"FAIL:?\s?(.+?)\s"),
    ]
    re_skip = re.compile(r"--- SKIP: (\S+)")

    for line in test_log.splitlines():
        line = line.strip()

        m = re_pass.match(line)
        if m:
            name = m.group(1)
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)

        for rp in re_fail:
            m = rp.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)

        m = re_skip.match(line)
        if m:
            name = m.group(1)
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


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

_DETECT_GO_VERSION_SH = r"""#!/bin/bash
REPO_DIR="${1:-.}"
MAX_MAJOR=0
MAX_MINOR=0

while IFS= read -r -d "" GOMOD; do
  VER=$(grep -m1 "^go " "$GOMOD" | awk '{print $2}')
  [ -z "$VER" ] && continue
  MAJOR=$(echo "$VER" | cut -d. -f1)
  MINOR=$(echo "$VER" | cut -d. -f2)
  if [ "$MAJOR" -gt "$MAX_MAJOR" ] 2>/dev/null || \
     { [ "$MAJOR" -eq "$MAX_MAJOR" ] && [ "$MINOR" -gt "$MAX_MINOR" ]; } 2>/dev/null; then
    MAX_MAJOR=$MAJOR
    MAX_MINOR=$MINOR
  fi
done < <(find "$REPO_DIR" -name go.mod -not -path "*/vendor/*" -print0)

if [ "$MAX_MAJOR" -gt 0 ]; then
  echo "${MAX_MAJOR}.${MAX_MINOR}"
else
  echo "unknown"
fi
"""

_FIND_MODULES_SH = r"""#!/bin/bash
REPO_DIR="$1"
shift
PATCHES="$@"

DIRS=$(cat $PATCHES 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\.go$' | xargs -I{} dirname {} | sort -u)

if [ -z "$DIRS" ]; then
  echo "ROOT:./..."
  exit 0
fi

declare -A MODULE_PKGS

for DIR in $DIRS; do
  CHECK="$REPO_DIR/$DIR"
  MOD_DIR=""
  while [ "$CHECK" != "$REPO_DIR" ] && [ "$CHECK" != "/" ]; do
    if [ -f "$CHECK/go.mod" ]; then
      MOD_DIR="$CHECK"
      break
    fi
    CHECK=$(dirname "$CHECK")
  done
  if [ -z "$MOD_DIR" ]; then
    MOD_DIR="$REPO_DIR"
  fi
  REL_MOD="${MOD_DIR#$REPO_DIR/}"
  if [ "$MOD_DIR" = "$REPO_DIR" ]; then
    REL_MOD="ROOT"
  fi
  if [ "$REL_MOD" = "ROOT" ]; then
    REL_PKG="$DIR"
  elif [ "$DIR" = "$REL_MOD" ]; then
    REL_PKG="."
  else
    REL_PKG="${DIR#${REL_MOD}/}"
  fi
  EXISTING="${MODULE_PKGS[$REL_MOD]:-}"
  if [ -n "$EXISTING" ]; then
    MODULE_PKGS[$REL_MOD]="$EXISTING ./$REL_PKG"
  else
    MODULE_PKGS[$REL_MOD]="./$REL_PKG"
  fi
done

for MOD in "${!MODULE_PKGS[@]}"; do
  echo "$MOD:${MODULE_PKGS[$MOD]}"
done
"""

_RUN_TESTS_PER_MODULE_SH = r"""#!/bin/bash
REPO_DIR="$1"
MODULE_LINES="$2"
EXIT_CODE=0

while IFS= read -r LINE; do
  [ -z "$LINE" ] && continue
  MOD="${LINE%%:*}"
  PKGS="${LINE#*:}"
  if [ "$MOD" = "ROOT" ]; then
    MOD_DIR="$REPO_DIR"
  else
    MOD_DIR="$REPO_DIR/$MOD"
  fi
  if [ ! -d "$MOD_DIR" ]; then
    echo "=== Skipping module: $MOD (directory does not exist) ==="
    continue
  fi
  cd "$MOD_DIR"
  VALID_PKGS=""
  for PKG in $PKGS; do
    PKG_DIR="${PKG#./}"
    if [ "$PKG_DIR" = "..." ] || [ -d "$MOD_DIR/$PKG_DIR" ]; then
      VALID_PKGS="$VALID_PKGS $PKG"
    else
      echo "=== Skipping package: $PKG (directory does not exist) ==="
    fi
  done
  VALID_PKGS=$(echo "$VALID_PKGS" | xargs)
  if [ -z "$VALID_PKGS" ]; then
    echo "=== Skipping module: $MOD (no valid packages) ==="
    continue
  fi
  echo "=== Testing module: $MOD (packages: $VALID_PKGS) ==="
  go test -v -count=1 -timeout 15m $VALID_PKGS || EXIT_CODE=$?
done <<< "$MODULE_LINES"

exit $EXIT_CODE
"""


# ---------------------------------------------------------------------------
# Simple variant (PRs 68-302, number_interval = sdk-go_302_to_68)
# Single go.mod, PKGS-based test execution
# ---------------------------------------------------------------------------


class _SdkGoSimpleImageBase(Image):
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
        return _GO_IMAGE

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
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class _SdkGoSimpleImageDefault(Image):
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
        return _SdkGoSimpleImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
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

go test -v -count=1 -timeout 15m ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
# Extract packages affected by patches
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -v -count=1 -timeout 15m $PKGS

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
# Extract packages affected by patches
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -v -count=1 -timeout 15m $PKGS

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
# Extract packages affected by patches
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -v -count=1 -timeout 15m $PKGS

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


@Instance.register("cloudevents", "sdk-go_302_to_68")
class SdkGoSimple(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _SdkGoSimpleImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)


# ---------------------------------------------------------------------------
# Multi-module variant (PRs 522+, number_interval = "")
# Multiple go.mod files, module-aware test execution
# ---------------------------------------------------------------------------


class _SdkGoMultiImageBase(Image):
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
        return _GO_IMAGE

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
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class _SdkGoMultiImageDefault(Image):
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
        return _SdkGoMultiImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "detect_go_version.sh", _DETECT_GO_VERSION_SH),
            File(".", "find_modules.sh", _FIND_MODULES_SH),
            File(".", "run_tests_per_module.sh", _RUN_TESTS_PER_MODULE_SH),
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

REQUIRED_GO=$(bash /home/detect_go_version.sh /home/{pr.repo})
CURRENT_GO=$(go version | grep -oP '\\d+\\.\\d+' | head -1)
echo "detect_go_version: required=$REQUIRED_GO current=$CURRENT_GO"

find /home/{pr.repo} -name go.mod -not -path "*/vendor/*" -print0 | while IFS= read -r -d "" GOMOD; do
  MOD_DIR=$(dirname "$GOMOD")
  echo "Running go mod tidy in $MOD_DIR"
  (cd "$MOD_DIR" && go mod tidy 2>&1) || echo "go mod tidy failed in $MOD_DIR (non-fatal)"
done

MODULE_LINES=$(bash /home/find_modules.sh /home/{pr.repo} /home/test.patch /home/fix.patch)
bash /home/run_tests_per_module.sh /home/{pr.repo} "$MODULE_LINES" || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
MODULE_LINES=$(bash /home/find_modules.sh /home/{pr.repo} /home/test.patch /home/fix.patch)
bash /home/run_tests_per_module.sh /home/{pr.repo} "$MODULE_LINES"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
MODULE_LINES=$(bash /home/find_modules.sh /home/{pr.repo} /home/test.patch /home/fix.patch)
bash /home/run_tests_per_module.sh /home/{pr.repo} "$MODULE_LINES"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
MODULE_LINES=$(bash /home/find_modules.sh /home/{pr.repo} /home/test.patch /home/fix.patch)
bash /home/run_tests_per_module.sh /home/{pr.repo} "$MODULE_LINES"

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


@Instance.register("cloudevents", "sdk-go")
class SdkGo(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _SdkGoMultiImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)
