import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Build tag used by the repo's Makefile (`GOTEST = go test -tags $(TAGS) ./...`)
_GO_BUILD_TAGS = "sqlite_math_functions"

# Test command template (CGO_ENABLED is required for mattn/go-sqlite3)
_GO_TEST_CMD = f'go test -tags "{_GO_BUILD_TAGS}" -v -count=1 ./...'


class TrdsqlImageBase(Image):
    """Base image: golang with CGo system deps and the trdsql repo cloned.

    Tagged `base-pr-<number>`, not a shared `base`, because the Dockerfile QC
    contract requires the PR layer to inherit
    `mswebench/<org>_m_<repo>:base-pr-<N>`. A shared tag also hides a real
    hazard: this image bakes in one BASE_COMMIT, so a single reused `base`
    stays pinned to whichever PR built it first.
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

    def dependency(self) -> Union[str, "Image"]:
        return "golang:1.19"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        if self.config.need_clone:
            fetch_block = f"""RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}
{self.clear_env}
CMD ["/bin/bash"]"""
        else:
            fetch_block = f"{self.clear_env}\nCOPY {repo} /home/{repo}"

        # golang:1.19 is Debian-based. gcc/libc6-dev/pkg-config are the CGo
        # toolchain needed to compile mattn/go-sqlite3, which trdsql links
        # against; `go env -w CGO_ENABLED=1` pins cgo on for every later stage
        # rather than relying on the image default. DEBIAN_FRONTEND is not set
        # here — DockerfileEnhancer's ENV block already exports it.
        return f"""FROM {self.dependency()}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gcc libc6-dev pkg-config \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN go env -w CGO_ENABLED=1

{fetch_block}
"""


class TrdsqlImageDefault(Image):
    """PR-level image: checks out the base commit, copies patches and scripts."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return TrdsqlImageBase(self.pr, self.config)

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

# Download the dependencies of the base commit.
go mod download || true

# Pre-warm the module cache with the modules introduced by the fix patch
# (github.com/multiprocessio/go-sqlite3-stdlib and its transitive deps) so
# that fix-run.sh does not have to resolve them from scratch.
git apply /home/fix.patch && {{ go mod tidy || true; go mod download || true; }} || true
git reset --hard

# Warm-up: run tests to verify baseline (failures tolerated)
go mod tidy || true
{go_test_cmd} || true

""".format(pr=self.pr, go_test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
go mod tidy || true
{go_test_cmd}

""".format(pr=self.pr, go_test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go mod tidy || true
{go_test_cmd}

""".format(pr=self.pr, go_test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go mod tidy || true
{go_test_cmd}

""".format(pr=self.pr, go_test_cmd=_GO_TEST_CMD),
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


@Instance.register("noborus", "trdsql")
class Trdsql(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TrdsqlImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes first — without this, regex patterns
        # fail on colored terminal output from test runners.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            # Keep the full `TestFunc/subtest` name: the fix patch's target
            # tests are subtests, and full names are unique across packages.
            return test_name

        for line in clean_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = get_base_name(pass_match.group(1))
                    if test_name in failed_tests:
                        continue
                    skipped_tests.discard(test_name)
                    passed_tests.add(test_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = get_base_name(fail_match.group(1))
                    passed_tests.discard(test_name)
                    skipped_tests.discard(test_name)
                    failed_tests.add(test_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = get_base_name(skip_match.group(1))
                    if test_name in passed_tests:
                        continue
                    if test_name in failed_tests:
                        continue
                    skipped_tests.add(test_name)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
