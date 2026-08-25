import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "golang:1.23"
# The repo's Makefile runs `go test -timeout 60s`, but several suites start an
# embedded etcd server, which is far slower under an emulated arm64 build than
# on CI. A longer timeout keeps the three stages comparable instead of turning
# emulation latency into spurious failures.
GO_TEST_CMD = "go test -json -count=1 -timeout 900s ./... 2>&1"


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

    def dependency(self) -> str:
        return BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""FROM {self.dependency()}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

export CI=true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go mod download || true

if git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    go mod download || true
fi
git checkout -- . || true
git reset --hard
git clean -fd
bash /home/check_git_changes.sh

go build ./... || true
go test -count=1 -run XXX_NO_SUCH_TEST ./... || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}

{gotest}
""".format(pr=self.pr, gotest=GO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

{gotest}
""".format(pr=self.pr, gotest=GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi

{gotest}
""".format(pr=self.pr, gotest=GO_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("strukturag", "nextcloud-spreed-signaling")
class NEXTCLOUD_SPREED_SIGNALING(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # `go test -json` emits one JSON object per line. Test-level events carry
        # both Package and Test, so the name is unique across packages (Go allows
        # the same TestX in several) and subtests keep their "TestX/sub" path.
        pkg_actions: dict[str, str] = {}
        pkg_has_tests: set[str] = set()

        # A package that fails to COMPILE produces no json event at all -- go
        # writes the compile errors to stderr as plain text and closes with
        # "FAIL <pkg> [build failed]". The package-level guard below therefore
        # never sees it, and every test in that package would vanish from the
        # stage with no signal at all. Catch the plain-text form here.
        build_failed_re = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]")

        for raw in log.split("\n"):
            line = raw.strip()
            if not line.startswith("{"):
                m = build_failed_re.match(line)
                if m:
                    failed_tests.add(f"{m.group(1)}::[build]")
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue

            action = ev.get("Action")
            pkg = ev.get("Package")
            test = ev.get("Test")
            if not pkg or action not in ("pass", "fail", "skip"):
                continue

            if not test:
                pkg_actions[pkg] = action
                continue

            pkg_has_tests.add(pkg)
            name = f"{pkg}::{test}"
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A package that failed without producing any test event did not build
        # (or died in TestMain). Surface it so the stage is not silently
        # credited with zero tests.
        for pkg, action in pkg_actions.items():
            if action == "fail" and pkg not in pkg_has_tests:
                failed_tests.add(f"{pkg}::[build]")

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
