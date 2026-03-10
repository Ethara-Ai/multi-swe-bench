import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# --------------------------------------------------------------------------- #
#  Shared shell scripts and Go stubs used by both ImageEarlyPRs / ImageLatePRs
# --------------------------------------------------------------------------- #

_RUN_SH = """#!/bin/bash

cd /home/{pr.repo}

# Restore db mock stubs if they were generated during prepare.
# The db_mock_gen.go is a copy of db.go with its build tag flipped from !mock to mock.
# The db_stubs_mock.go is a static fallback with minimal type/method stubs.
if [ -f db/db_mock_gen.go.bak ]; then
  cp db/db_mock_gen.go.bak db/db_mock_gen.go 2>/dev/null || true
elif [ -f /home/db_stubs_mock.go ] && [ -f db/db_stubs_mock.go.bak ]; then
  cp db/db_stubs_mock.go.bak db/db_stubs_mock.go 2>/dev/null || true
fi

exit_code=0
# Use find to enumerate packages robustly (go list may fail if any package has errors)
for pkg_dir in $(find . -name '*_test.go' -exec dirname {{}} \\; | sort -u); do
    pkg=$(cd "$pkg_dir" && go list -tags mock . 2>/dev/null)
    if [ -z "$pkg" ]; then
        continue
    fi
    if go build -tags mock "$pkg" 2>/dev/null; then
        go test -v -count=1 -tags mock "$pkg" 2>&1 || exit_code=$?
    else
        echo "FAIL\\t$pkg [build failed]"
    fi
done
exit $exit_code
"""

_TEST_RUN_SH = """#!/bin/bash

cd /home/{pr.repo}
git apply /home/test.patch

# Restore db mock stubs if patch removed them
if [ -f db/db_mock_gen.go.bak ] && [ ! -f db/db_mock_gen.go ]; then
  cp db/db_mock_gen.go.bak db/db_mock_gen.go 2>/dev/null || true
elif [ -f db/db_stubs_mock.go.bak ] && [ ! -f db/db_stubs_mock.go ]; then
  cp db/db_stubs_mock.go.bak db/db_stubs_mock.go 2>/dev/null || true
fi

exit_code=0
for pkg_dir in $(find . -name '*_test.go' -exec dirname {{}} \\; | sort -u); do
    pkg=$(cd "$pkg_dir" && go list -tags mock . 2>/dev/null)
    if [ -z "$pkg" ]; then
        continue
    fi
    if go build -tags mock "$pkg" 2>/dev/null; then
        go test -v -count=1 -tags mock "$pkg" 2>&1 || exit_code=$?
    else
        echo "FAIL\\t$pkg [build failed]"
    fi
done
exit $exit_code
"""

_FIX_RUN_SH = """#!/bin/bash

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch

# Restore db mock stubs if patch removed them
if [ -f db/db_mock_gen.go.bak ] && [ ! -f db/db_mock_gen.go ]; then
  cp db/db_mock_gen.go.bak db/db_mock_gen.go 2>/dev/null || true
elif [ -f db/db_stubs_mock.go.bak ] && [ ! -f db/db_stubs_mock.go ]; then
  cp db/db_stubs_mock.go.bak db/db_stubs_mock.go 2>/dev/null || true
fi

exit_code=0
for pkg_dir in $(find . -name '*_test.go' -exec dirname {{}} \\; | sort -u); do
    pkg=$(cd "$pkg_dir" && go list -tags mock . 2>/dev/null)
    if [ -z "$pkg" ]; then
        continue
    fi
    if go build -tags mock "$pkg" 2>/dev/null; then
        go test -v -count=1 -tags mock "$pkg" 2>&1 || exit_code=$?
    else
        echo "FAIL\\t$pkg [build failed]"
    fi
done
exit $exit_code
"""

# Go stub file for the db package.  Only compiled when -tags mock is set.
# At many base commits, db/db.go (tagged //go:build !mock) defines types and
# methods that other db/*.go files reference.  When building with -tags mock,
# db.go is excluded and those symbols are missing.  This stub supplies them.
_DB_STUBS_MOCK = """\
//go:build mock
// +build mock

package db

import (
\t"errors"
\t"net/http"
)

// --- Types normally defined in db.go ---

type PeopleExtra struct {
\tBody   string `json:"body"`
\tPerson string `json:"person"`
}

type LeaderData map[string]interface{}

// --- Method stubs on the database struct ---
// These are no-op / zero-value implementations, sufficient for compilation.
// The handler tests mock the Database interface, so these are never actually
// called during test execution.

func (db database) GetAllPeople() []Person {
\treturn nil
}

func (db database) AddUuidToPerson(id uint, uuid string) {
}

func (db database) GetPersonByPubkey(pubkey string) Person {
\treturn Person{}
}

func (db database) UpdatePerson(id uint, u map[string]interface{}) bool {
\treturn false
}

func (db database) GetPeopleForNewTicket(languages []interface{}) ([]Person, error) {
\treturn nil, errors.New("stub")
}

func (db database) GetListedPosts(r *http.Request) ([]PeopleExtra, error) {
\treturn nil, nil
}

func (db database) GetListedOffers(r *http.Request) ([]PeopleExtra, error) {
\treturn nil, nil
}

func (db database) GetBountiesLeaderboard() []LeaderData {
\treturn nil
}
"""


# Shell snippet used in prepare.sh to generate comprehensive db mock stubs.
# Instead of maintaining a static stub, we copy db.go and flip its build tag
# from !mock to mock.  This makes ALL types + methods available.
_PREPARE_DB_STUBS = r"""
# ---------- db stub generation for mock builds ----------
# db/db.go is tagged //go:build !mock, so it is excluded when building with
# -tags mock.  Many other db/*.go files (and handlers/) reference symbols
# defined only in db.go.  We create a mock-tagged copy so everything compiles.
if ! go build -tags mock ./db/ 2>/dev/null; then
  echo "db package does not compile with -tags mock, creating mock copy of db.go..."
  if [ -f db/db.go ]; then
    sed \
      -e 's|^//go:build !mock|//go:build mock|' \
      -e 's|^// +build !mock|// +build mock|' \
      db/db.go > db/db_mock_gen.go
    # Verify it compiles now
    if ! go build -tags mock ./db/ 2>/dev/null; then
      echo "WARNING: db still does not compile after mock copy, trying static stubs..."
      rm -f db/db_mock_gen.go
      cp /home/db_stubs_mock.go db/db_stubs_mock.go
      if ! go build -tags mock ./db/ 2>/dev/null; then
        echo "WARNING: db still does not compile after static stubs"
      else
        echo "db package compiles with static stubs"
        # Save backup for run scripts to restore after git apply
        cp db/db_stubs_mock.go db/db_stubs_mock.go.bak
      fi
    else
      echo "db package compiles with mock copy of db.go"
      # Save backup for run scripts to restore after git apply
      cp db/db_mock_gen.go db/db_mock_gen.go.bak
    fi
  fi
else
  echo "db package already compiles with -tags mock"
fi
"""


class SphinxTribesImageBase(Image):
    """Base image that handles repo cloning/copying."""

    def __init__(self, pr: PullRequest, config: Config, go_version: str):
        self._pr = pr
        self._config = config
        self._go_version = go_version

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return f"golang:{self._go_version}"

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


class ImageEarlyPRs(Image):
    """Image for sphinx-tribes early PRs (504-1070).

    PRs covered: 504, 646, 782, 874, 894, 950, 962, 981, 995, 1019, 1045, 1048, 1066, 1070 (14 PRs)
    Date range: 2023-03-30 to 2023-12-09
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

    def dependency(self) -> Image | None:
        return SphinxTribesImageBase(self.pr, self.config, "1.21")

    def image_prefix(self) -> str:
        return "envagent"

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
                ("""#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

""" + _PREPARE_DB_STUBS + "\n").format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH.format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH.format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH.format(pr=self.pr),
            ),
            File(
                ".",
                "db_stubs_mock.go",
                _DB_STUBS_MOCK,
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


class ImageLatePRs(Image):
    """Image for sphinx-tribes late PRs (1184-2603).

    PRs covered: 1184, 1192, 1216, ... 2603 (106 PRs)
    Date range: 2023-12-24 to 2025-03-02
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

    def dependency(self) -> Image | None:
        return SphinxTribesImageBase(self.pr, self.config, "1.21")

    def image_prefix(self) -> str:
        return "envagent"

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
                ("""#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

""" + _PREPARE_DB_STUBS + "\n").format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH.format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH.format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH.format(pr=self.pr),
            ),
            File(
                ".",
                "db_stubs_mock.go",
                _DB_STUBS_MOCK,
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


@Instance.register("stakwork", "sphinx-tribes")
class SphinxTribes(Instance):
    """Instance for stakwork/sphinx-tribes.

    Handles two PR ranges with different configurations:
    - Early PRs (504-1070): golang:1.19, go test -v ./...
    - Late PRs (1184-2603): golang:1.20, go test ./... -tags mock -race -v

    Total: 120 PRs
    Date range: 2023-03-30 to 2025-03-02
    """

    LATE_PR_START = 1184

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def _is_late_pr(self) -> bool:
        return self.pr.number >= self.LATE_PR_START

    def dependency(self) -> Optional[Image]:
        if self._is_late_pr():
            return ImageLatePRs(self.pr, self._config)
        return ImageEarlyPRs(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        """Parse Go test output."""
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_pattern = re.compile(r"--- PASS: (\S+)")
        failed_pattern = re.compile(r"--- FAIL: (\S+)")
        skipped_pattern = re.compile(r"--- SKIP: (\S+)")

        for line in log.splitlines():
            line = line.strip()

            pass_match = passed_pattern.search(line)
            if pass_match:
                test_name = pass_match.group(1)
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            fail_match = failed_pattern.search(line)
            if fail_match:
                test_name = fail_match.group(1)
                passed_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            skip_match = skipped_pattern.search(line)
            if skip_match:
                test_name = skip_match.group(1)
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )