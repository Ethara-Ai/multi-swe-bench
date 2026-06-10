import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RiverImageBase(Image):
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
        # riverqueue/river's `go` directive spans 1.21.0 (PR ~27) through
        # 1.25.0 (PR ~1233) within the dataset. Go is backward compatible and
        # the toolchain auto-fetches newer minor versions via GOTOOLCHAIN=auto,
        # so the newest base image in the range builds and tests every era.
        # CI runs against Go 1.25/1.26, so 1.26 is the safe ceiling.
        return "golang:1.26-bookworm"

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

        # river's test suite is backed by PostgreSQL. Install a local server and
        # raise max_connections well above the default 100: on multi-core hosts
        # the test database manager opens `max(4, NumCPU)` pools each sized to
        # `max(4, NumCPU)` connections, which exhausts the default limit. fsync
        # off speeds up the throwaway test databases. util-linux provides
        # `taskset`, used at run time to bound NumCPU (and thus pool fan-out).
        return f"""FROM {image_name}

{self.global_env}

ENV GOTOOLCHAIN=auto
ENV DEBIAN_FRONTEND=noninteractive
RUN git config --global --add safe.directory '*'
RUN apt-get update && apt-get install -y --no-install-recommends \\
    postgresql \\
    postgresql-contrib \\
    util-linux \\
    && rm -rf /var/lib/apt/lists/*
RUN PGCONF=$(ls /etc/postgresql/*/main/postgresql.conf | head -1) && \\
    echo "max_connections = 1000" >> "$PGCONF" && \\
    echo "fsync = off" >> "$PGCONF"

WORKDIR /home/

{code}

{self.clear_env}

"""


class RiverImageDefault(Image):
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
        return RiverImageBase(self.pr, self._config)

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
                "common.sh",
                """#!/bin/bash
# Shared helpers for the riverqueue/river run/test/fix scripts.
#
# river is a Go workspace with several submodules (root, cmd/river,
# riverdriver/*, rivershared, rivertype) whose layout changed across history.
# Tests are PostgreSQL-backed. The harness runs each script in a fresh
# container, so PostgreSQL must be started and the test databases prepared on
# every invocation.

# Bound NumCPU so the test database manager's pool fan-out
# (max(4, NumCPU) databases, each pool sized max(4, NumCPU)) stays well under
# PostgreSQL's max_connections, and so `testdbman` creates exactly as many
# numbered databases as the manager will request. `runtime.NumCPU()` honours
# the CPU affinity mask, so taskset is an effective, host-independent cap.
# Falls back to no cap on hosts with fewer than 4 CPUs (already low fan-out).
RIVER_TASK="taskset -c 0-3"
$RIVER_TASK true 2>/dev/null || RIVER_TASK=""
export RIVER_TASK

setup_river_db() {
  PGCONF=$(ls /etc/postgresql/*/main/postgresql.conf | head -1)
  grep -q '^max_connections = 1000' "$PGCONF" 2>/dev/null || {
    echo 'max_connections = 1000' >> "$PGCONF"
    echo 'fsync = off' >> "$PGCONF"
  }
  service postgresql start || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break
    sleep 1
  done
  su - postgres -c "psql -c \\"ALTER USER postgres PASSWORD 'postgres';\\"" >/dev/null 2>&1 || true
  su - postgres -c "psql -c \\"CREATE DATABASE river_dev;\\"" >/dev/null 2>&1 || true

  # PG* env lets pgx resolve testdbman's host-less `postgres:///<db>` URLs to
  # the local TCP server with credentials.
  export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=postgres PGPASSWORD=postgres
  export ADMIN_DATABASE_URL="postgres://postgres:postgres@127.0.0.1:5432"
  export DATABASE_URL="postgres://postgres:postgres@127.0.0.1:5432/river_dev?sslmode=disable"
  export GOTOOLCHAIN=auto

  # Early/mid eras ship `internal/cmd/testdbman`, which creates and migrates the
  # numbered test databases the pool manager hands out (`river_testdb_N` in the
  # earliest era, `river_test_N` later). Later eras build per-test schemas in a
  # single `river_test` database and have no testdbman.
  if [ -d internal/cmd/testdbman ]; then
    $RIVER_TASK go run ./internal/cmd/testdbman create >/dev/null 2>&1 || true
  fi

  # The base database name the suite expects differs by era; detect it from
  # what testdbman created (river_testdb vs river_test). When absent, the
  # current era self-manages schemas inside river_test.
  if su - postgres -c "psql -tAc \\"SELECT 1 FROM pg_database WHERE datname='river_testdb'\\"" 2>/dev/null | grep -q 1; then
    RIVER_DB_BASE=river_testdb
  else
    RIVER_DB_BASE=river_test
    su - postgres -c "psql -c \\"CREATE DATABASE river_test;\\"" >/dev/null 2>&1 || true
  fi
  export TEST_DATABASE_URL="postgres://postgres:postgres@127.0.0.1:5432/${RIVER_DB_BASE}?sslmode=disable"
  echo "river test database base: ${RIVER_DB_BASE}"
}

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn "$f" \\
    || git apply --whitespace=nowarn --3way "$f" \\
    || git apply --whitespace=nowarn --reject "$f" \\
    || true
}

# Run `go test` in each workspace submodule (every directory containing a
# go.mod). `-buildvcs=false` keeps the embedded Main.Version as "(unknown)"
# rather than git's "(devel)" so version-stamping tests behave as in CI.
# `-p 1 -parallel 4` keeps the database connection load bounded and stable.
run_go_tests() {
  find . -name go.mod -not -path './.git/*' -exec dirname {} \\; | sort | while read -r d; do
    echo "=== MODULE: ${d} ==="
    ( cd "$d" && $RIVER_TASK go test -v -count=1 -buildvcs=false -timeout 600s -p 1 -parallel 4 ./... ) || true
  done
}
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

git config --global --add safe.directory '*'
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the module/build cache so the eval runs are fast and offline-friendly.
# Compiling the test binaries (-run '^$' runs no tests) downloads every test
# dependency without needing a database. `|| true` because a package's TestMain
# may try to connect to PostgreSQL during this DB-less warm-up.
export GOTOOLCHAIN=auto
find . -name go.mod -not -path './.git/*' -exec dirname {{}} \\; | while read -r d; do
  ( cd "$d" && go test -run '^$' -count=1 -buildvcs=false ./... ) || true
done

# Discard any incidental go.sum/go.work.sum drift from the warm-up so the
# working tree is clean for patch application.
git reset --hard
git checkout {pr.base.sha}

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh
setup_river_db
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh
setup_river_db
apply_patch /home/test.patch
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh
setup_river_db
apply_patch /home/test.patch
apply_patch /home/fix.patch
run_go_tests

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


@Instance.register("riverqueue", "river")
class River(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RiverImageDefault(self.pr, self._config)

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
        # `go test` output is not colorized by default, but strip ANSI escapes
        # defensively in case the log was captured through a colorizing tee.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")
        # A package summary line ("ok   <import-path>", "FAIL <import-path>",
        # "?    <import-path>") closes the block of tests printed above it.
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        # Tests are buffered per package so the package import path can be
        # prepended -- this keeps names globally unique when several packages
        # (and submodules) are tested in one run.
        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            pass_match = re_pass.match(line)
            if pass_match:
                pending_pass.add(pass_match.group(1))
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                pending_fail.add(fail_match.group(1))
                continue

            skip_match = re_skip.match(line)
            if skip_match:
                pending_skip.add(skip_match.group(1))
                continue

            pkg_match = re_pkg.match(line)
            if pkg_match:
                flush(pkg_match.group(1))

        # Flush tests not followed by a summary line (e.g. truncated/timed-out
        # log) so they are still counted.
        flush("unknown")

        # Enforce TestResult disjointness invariants: a test reported as both
        # passed and failed (e.g. flaky retry) counts as failed.
        passed_tests -= failed_tests
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
