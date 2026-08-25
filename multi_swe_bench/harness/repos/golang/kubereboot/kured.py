from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class KuredImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        # go.mod declares `go 1.19` at the dataset base commit, and CI resolves
        # the toolchain from go.mod (`go-version-file: go.mod`).
        # golang:1.21-bookworm is the contemporary release for this PR era,
        # satisfies the >=1.19 directive, and publishes amd64 + arm64.
        return "golang:1.21-bookworm"

    def image_tag(self) -> str:
        # Per-PR, not a shared "base": the enhancer pins this image to
        # ${BASE_COMMIT} via `git checkout`, so the base layer is specific to
        # one PR. A shared tag would let image dedup (keyed on
        # image_full_name()) build it once and silently reuse the wrong commit
        # for every other PR in the dataset.
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

        # No syntax directive, proxy ARGs, cert symlinks or OCI labels here --
        # DockerfileEnhancer.enhance() injects those, and it bails out entirely
        # if a `# syntax=` line is already present. The clone stays last because
        # the enhancer expands it into clone + checkout + history hardening + CMD.
        return f"""FROM {image_name}

{self.global_env}

ENV CI=true
ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local

WORKDIR /home/

{self.clear_env}

{code}

"""


class KuredImageDefault(Image):
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
        return KuredImageBase(self.pr, self.config)

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
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the module and build caches while the network is still available during
# the image build. `|| true` is required: a transient download failure or an
# arm64 build hiccup must not abort the image, and the three graded stages
# re-run the real test command anyway.
go mod download || true
go build ./... || true
go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 ./...

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


@Instance.register("kubereboot", "kured")
class Kured(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return KuredImageDefault(self.pr, self._config)

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
        # Strip ANSI escapes first -- colored terminal output would otherwise
        # defeat every anchored pattern below.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` prints one status line per test and per subtest, with
        # subtests indented under their parent:
        #     --- PASS: TestCanAcquireMultiple (0.00s)
        #         --- PASS: TestCanAcquireMultiple/empty_lock (0.00s)
        #
        # `\S+` captures the test node id only, so the trailing ` (0.00s)` never
        # enters the name -- a duration that varies between stages would make a
        # single test look like two and desync the 3-stage name union.
        #
        # The full `Parent/subtest` id is kept: truncating to the parent would
        # collapse every case of a table-driven test into one colliding name.
        #
        # Package-level `FAIL\tgithub.com/...\t[build failed]` lines are
        # deliberately not matched -- they are not test node ids.
        re_status = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")

        for line in log.splitlines():
            match = re_status.match(line)
            if not match:
                continue

            status = match.group(1)
            name = match.group(2)

            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # TestResult.__post_init__ requires the three sets to be pairwise
        # disjoint; a retried or re-reported test can otherwise land in two.
        skipped_tests -= failed_tests
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
