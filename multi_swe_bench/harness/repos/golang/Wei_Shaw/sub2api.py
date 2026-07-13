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


class Sub2apiImageDefault(Image):
    """Per-PR image using the single-image pattern.

    dependency() returns a string => DockerfileEnhancer injects REPO_URL/BASE_COMMIT
    ARGs, proxy certs, clone, checkout, and _HARDENING_BLOCK automatically.
    Each PR image clones and hardens to its OWN BASE_COMMIT -- no shared base image.

    Monorepo: Go module lives at backend/ (NOT repo root).
    Pure Go (no CGO). Integration tests are //go:build integration gated.
    Unit tests use //go:build unit -- passed -tags=unit in test commands."""

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
        # String => DockerfileEnhancer runs => clone + checkout + hardening injected per-PR
        return "golang:1.26"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        copy_cmds = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return (
            "ENV CGO_ENABLED=0\n"
            "ENV GOTOOLCHAIN=local\n"
            'ENV GOFLAGS="-buildvcs=false -mod=mod"\n'
            f"{copy_cmds}\n"
            "RUN bash /home/prepare.sh"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}/backend
timeout 600 go mod download || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash
set -eo pipefail
cd /home/""" + self.pr.repo + r"""/backend
TEST_DIRS=$({ grep -E '^diff --git a/backend/\S+_test\.go' /home/test.patch \
    | sed -E 's#^diff --git a/backend/(.+) b/.*#\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | grep -vE '_integration_test\.go$|_e2e_test\.go$' \
    | sed -E 's#/[^/]+$##' | sort -u; } || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -tags=unit -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_BASELINE_TEST_DIRS"; exit 0; fi
""",
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
set -eo pipefail
cd /home/""" + self.pr.repo + r"""
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \
    --exclude='*.test' --exclude='*.wasm' --exclude='*.exe' --exclude='*.db')
git apply --whitespace=nowarn "${EXCLUDES[@]}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${EXCLUDES[@]}" /home/test.patch 2>/dev/null || true
cd backend
if grep -qE '^diff --git a/backend/go\.(mod|sum)' /home/test.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({ grep -E '^diff --git a/backend/\S+_test\.go' /home/test.patch \
    | sed -E 's#^diff --git a/backend/(.+) b/.*#\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | grep -vE '_integration_test\.go$|_e2e_test\.go$' \
    | sed -E 's#/[^/]+$##' | sort -u; } || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -tags=unit -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
""",
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
set -eo pipefail
cd /home/""" + self.pr.repo + r"""
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \
    --exclude='*.test' --exclude='*.wasm' --exclude='*.exe' --exclude='*.db')
git apply --whitespace=nowarn "${EXCLUDES[@]}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${EXCLUDES[@]}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${EXCLUDES[@]}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${EXCLUDES[@]}" /home/fix.patch 2>/dev/null || true
cd backend
if grep -qhE '^diff --git a/backend/go\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({ grep -E '^diff --git a/backend/\S+_test\.go' /home/test.patch \
    | sed -E 's#^diff --git a/backend/(.+) b/.*#\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | grep -vE '_integration_test\.go$|_e2e_test\.go$' \
    | sed -E 's#/[^/]+$##' | sort -u; } || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -tags=unit -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
""",
            ),
        ]


@Instance.register("Wei-Shaw", "10-36-66-92-110-166-178-208-216-221-236-278-300-316-332-489-513-519-543-561-621-670-724-761-872-944-986-1010-1036-1047-1162-1262-1382-1391-1460-1575-1731-1850-1948-2247")
class SUB2API_PERPR40(Instance):
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


@Instance.register("Wei-Shaw", "45-110-166-208-213-221-236-247-278-316-377-493-513-550-579-597-621-630-670-723-761-806-807-908-944-986-1007-1047-1132-1162-1262-1382-1391-1460-1575-1635-1683-1731-1850-1948-2116-2120-2247")
class SUB2API_43PR(Instance):
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


@Instance.register("Wei-Shaw", "110-166-208-213-221-236-247-278-316-377-493-513-550-579-597-630-670-723-761-806-908-944-986-1007-1047-1132-1162-1262-1382-1460-1575-1635-1683-1731-1948-2116-2247")
class SUB2API_37PR(Instance):
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
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        return parse_go_test_log(log)


@Instance.register("Wei-Shaw", "213-550-579-621-1382-2116")
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
