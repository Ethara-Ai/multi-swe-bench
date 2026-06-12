import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_PREFIX = "github.com/Wei-Shaw/sub2api/"


def parse_go_test_log(log: str) -> TestResult:
    """Parse `go test -json` output. Names are kept package-qualified
    (`pkg/path::TestName`); subtests appear as `TestName/sub`. The repo-
    qualified package prefix is stripped to keep ids short and stable."""
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


class Sub2apiImageBase(Image):
    """sub2api single-era base (PRs 7-2247, release_line v0.1.x throughout).
    Monorepo: Go module lives at `backend/` (NOT repo root); frontend/ is
    JS/TS and out of scope. go.mod range `go 1.24.0` → `go 1.26.3`; built
    with golang:1.26 which is backward-compatible across that range.

    Pure Go (no CGO). modernc.org/sqlite is the only DB driver. Integration
    tests use testcontainers-go (postgres/redis) but are all `//go:build
    integration` gated → skipped by default (`go test` without `-tags=integration`).
    Unit tests use `//go:build unit` (197 files) — we pass `-tags=unit` to
    include them alongside the 305 untagged tests."""

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
        return "golang:1.26"

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

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local
ENV GOFLAGS=-buildvcs=false -mod=mod

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}
"""


class Sub2apiImageDefault(Image):
    """Per-PR image: checkout base commit, prefetch modules at backend/, run
    the targeted Go unit tests under backend/."""

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
        return Sub2apiImageBase(self.pr, self._config)

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
cd backend
timeout 600 go mod download || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}/backend
# Go package dirs the PR's test patch touches under backend/. Strip the
# `backend/` prefix so paths are relative to the backend module root.
# Skip integration/e2e — both are //go:build tagged so harmless if matched,
# but defensive grep keeps targeted runs lean.
TEST_DIRS=$({{ grep -E '^diff --git a/backend/\\S+_test\\.go' /home/test.patch \\
    | sed -E 's#^diff --git a/backend/(.+) b/.*#\\1#' \\
    | grep -vE '(^|/)(integration|e2e)/' \\
    | grep -vE '_integration_test\\.go$|_e2e_test\\.go$' \\
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -tags=unit -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_BASELINE_TEST_DIRS"; exit 0; fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.test' --exclude='*.wasm' --exclude='*.exe' --exclude='*.db')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
cd backend
if grep -qE '^diff --git a/backend/go\\.(mod|sum)' /home/test.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/backend/\\S+_test\\.go' /home/test.patch \\
    | sed -E 's#^diff --git a/backend/(.+) b/.*#\\1#' \\
    | grep -vE '(^|/)(integration|e2e)/' \\
    | grep -vE '_integration_test\\.go$|_e2e_test\\.go$' \\
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -tags=unit -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.test' --exclude='*.wasm' --exclude='*.exe' --exclude='*.db')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
cd backend
if grep -qhE '^diff --git a/backend/go\\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/backend/\\S+_test\\.go' /home/test.patch \\
    | sed -E 's#^diff --git a/backend/(.+) b/.*#\\1#' \\
    | grep -vE '(^|/)(integration|e2e)/' \\
    | grep -vE '_integration_test\\.go$|_e2e_test\\.go$' \\
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -tags=unit -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
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


@Instance.register("Wei-Shaw", "sub2api")
class SUB2API(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Sub2apiImageDefault(self.pr, self._config)

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
