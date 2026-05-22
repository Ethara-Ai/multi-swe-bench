import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_PREFIX = "github.com/rook/rook/"


def parse_go_test_log(log: str) -> TestResult:
    """Parse `go test -json` output. Each test emits a JSON event with
    `Action` (run/pass/fail/skip), `Package`, and `Test`. Names are kept
    package-qualified (`pkg/path::TestName`) since Go test function names
    recur across packages; subtests appear as `TestName/sub`."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw in log.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = _json.loads(raw)
        except Exception:
            continue
        test = ev.get("Test")
        action = ev.get("Action")
        pkg = ev.get("Package", "") or ""
        if not test or action not in ("pass", "fail", "skip"):
            continue
        if pkg.startswith(_REPO_PREFIX):
            pkg = pkg[len(_REPO_PREFIX):]
        name = f"{pkg}::{test}"
        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    # Enforce TestResult disjoint-set invariant.
    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class RookEra1ImageBase(Image):
    """rook era 1 (PRs 6764-10055, v1.5->1.9): go.mod `go 1.13`-`1.16`.
    Pure-Go Kubernetes/Ceph operator — CGO disabled, `go test` unit suite
    under `pkg/` + `cmd/`. Built with Go 1.16 (>= every go.mod in this era).
    `-buildvcs` is intentionally NOT set: it predates Go 1.18."""

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
        return "golang:1.16"

    def image_tag(self) -> str:
        return "base-go116"

    def workdir(self) -> str:
        return "base-go116"

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

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local
ENV GOFLAGS=-mod=mod

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git jq ca-certificates && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}
"""


class RookEra1ImageDefault(Image):
    """Per-PR image: checkout base commit, prefetch modules, run the targeted
    Go unit tests."""

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
        return RookEra1ImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
go mod download || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export ROOK_UNIT_JQ_PATH="$(which jq)"
# Go package dirs the PR's test patch touches (pkg/ + cmd/ unit tests only;
# the tests/ tree holds integration tests that need a real cluster).
TEST_DIRS=$({{ grep -E '^diff --git a/(pkg|cmd)/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' | sed -E 's#/[^/]+$##' | sort -u; }} || true)
EXIST=""
for d in $TEST_DIRS; do if [ -d "$d" ]; then EXIST="$EXIST ./$d/"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_DIRS"; exit 0; fi
go test -json -count=1 $EXIST 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export ROOK_UNIT_JQ_PATH="$(which jq)"
EXCLUDES=(--exclude='Documentation/*' --exclude='design/*' --exclude='*.png' \
    --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/go\\.(mod|sum)' /home/test.patch 2>/dev/null; then
    go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/(pkg|cmd)/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' | sed -E 's#/[^/]+$##' | sort -u; }} || true)
EXIST=""
for d in $TEST_DIRS; do if [ -d "$d" ]; then EXIST="$EXIST ./$d/"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_DIRS"; exit 0; fi
go test -json -count=1 $EXIST 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export ROOK_UNIT_JQ_PATH="$(which jq)"
EXCLUDES=(--exclude='Documentation/*' --exclude='design/*' --exclude='*.png' \
    --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/go\\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/(pkg|cmd)/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' | sed -E 's#/[^/]+$##' | sort -u; }} || true)
EXIST=""
for d in $TEST_DIRS; do if [ -d "$d" ]; then EXIST="$EXIST ./$d/"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_DIRS"; exit 0; fi
go test -json -count=1 $EXIST 2>&1
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("rook", "rook_10055_to_6764")
class ROOK_10055_TO_6764(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RookEra1ImageDefault(self.pr, self._config)

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
        return parse_go_test_log(log)
