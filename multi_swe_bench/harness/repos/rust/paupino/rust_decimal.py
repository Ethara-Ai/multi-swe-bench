import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The graded tests live behind the `db-postgres` cargo feature. At the base commit
# the Postgres test block in tests/decimal_tests.rs is gated on `feature = "postgres"`,
# which does not exist in Cargo.toml, so it is dead code; the test patch renames the
# gate to `db-postgres` and adds the new NaN/Infinity case. Without this feature the
# target tests are compiled out entirely and the run produces no signal at all.
CARGO_FEATURES = "db-postgres"

# Tests excluded from every stage so the three graded runs stay comparable.
#   * "generated" -> `mod generated` in tests/decimal_tests.rs, a CSV-table-driven suite
#     backed by the 300+ files under tests/generated/. Upstream's own
#     `cargo make test-db-postgres` task skips it for the database feature runs too.
#   * "postgres::driver::test::" -> the lib unit tests in src/postgres/driver.rs open a
#     TCP connection to a live PostgreSQL server (upstream CI provides a postgres:11.6
#     service container). Inside the grading container they fail with ConnectionRefused
#     in all three runs, which would leave fix_patch_result.failed_count non-zero and
#     break the health invariant. The graded Postgres tests are the byte-level FromSql
#     cases in tests/decimal_tests.rs, which need no server.
SKIPPED_TEST_FILTERS = ("generated", "postgres::driver::test::")

# --tests selects the lib unit tests plus the integration test targets and excludes
# doctests, whose result lines ("test src/lib.rs - Decimal (line 12) ... ok") are not
# parseable by parse_log() and would otherwise be silently dropped.
CARGO_BASE = f"cargo test --workspace --tests --features={CARGO_FEATURES}"

# One single cargo invocation, never a `&&` chain: a failing test must not be able to
# abort the command and hide the remaining test binaries' output. --no-fail-fast is
# load-bearing here — in the test-patch run the new case FAILS, and without it cargo
# would stop before running the other test binaries, corrupting the p2p baseline.
CARGO_TEST_CMD = (
    f"{CARGO_BASE} --no-fail-fast -- --test-threads=1 "
    + " ".join(f"--skip {name}" for name in SKIPPED_TEST_FILTERS)
).strip()


class RustDecimalImageBase(Image):
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
        return "rust:1.88.0"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential pkg-config libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class RustDecimalImageDefault(Image):
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
        return RustDecimalImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        git_apply_opts = "--binary --3way"

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

{cargo_base} --no-run || true

""".format(repo=self.pr.repo, sha=self.pr.base.sha, cargo_base=CARGO_BASE),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{cmd} 2>&1

""".format(repo=self.pr.repo, cmd=CARGO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply {apply_opts} /home/test.patch || git apply --binary /home/test.patch || git apply --whitespace=nowarn /home/test.patch
touch -c src/*.rs src/postgres/*.rs tests/*.rs Cargo.toml Cargo.lock 2>/dev/null || true
{cmd} 2>&1

""".format(repo=self.pr.repo, cmd=CARGO_TEST_CMD, apply_opts=git_apply_opts),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply {apply_opts} /home/test.patch /home/fix.patch || git apply --binary /home/test.patch /home/fix.patch || git apply --whitespace=nowarn /home/test.patch /home/fix.patch
touch -c src/*.rs src/postgres/*.rs tests/*.rs Cargo.toml Cargo.lock 2>/dev/null || true
{cmd} 2>&1

""".format(repo=self.pr.repo, cmd=CARGO_TEST_CMD, apply_opts=git_apply_opts),
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


@Instance.register("paupino", "rust-decimal")
class RustDecimal(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RustDecimalImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"^test (\S+) \.\.\. ok$")]
        re_fail_tests = [re.compile(r"^test (\S+) \.\.\. FAILED$")]
        re_skip_tests = [re.compile(r"^test (\S+) \.\.\. ignored")]

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
