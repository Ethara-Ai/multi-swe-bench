import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_PREFIXES = (
    "github.com/siderolabs/talos/",
    "github.com/talos-systems/talos/",
)


def parse_go_test_log(log: str) -> TestResult:
    """Parse `go test -json` output. Names are kept package-qualified
    (`pkg/path::TestName`); subtests appear as `TestName/sub`. Both the
    modern `siderolabs/talos` and pre-rename `talos-systems/talos` module
    prefixes are stripped so PR# 4570-6690 (talos-systems) and later
    PRs (siderolabs) produce stable, comparable ids.

    Module rename occurred at PR# 6690 (release ~1.2)."""
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
        for prefix in _REPO_PREFIXES:
            if pkg.startswith(prefix):
                pkg = pkg[len(prefix):]
                break
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


class TalosEra1ImageBase(Image):
    """talos era 1 (Go 1.17->1.20, PR# range 4570-8243, releases 0.13->1.0
    spanning into 1.1). Pre-go.work era for the earliest PRs (4570-5968) —
    those need explicit per-submodule `go mod download` since the workspace
    file wasn't present yet. Built with golang:1.20 (>= every go.mod in the
    era; Go backward-compatible).

    Two module paths span this era: `github.com/talos-systems/talos` (PRs
    4570-6690, pre-rename) and `github.com/siderolabs/talos` (PRs 6690+).
    parse_log strips both. The pkg/machinery sub-module ALWAYS exists as a
    separate Go module and is touched by 29/69 PRs across the dataset."""

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
        return "golang:1.20"

    def image_tag(self) -> str:
        return "base-go120"

    def workdir(self) -> str:
        return "base-go120"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone --no-single-branch https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

LABEL org.opencontainers.image.title="siderolabs/talos" \\
      org.opencontainers.image.description="siderolabs/talos Docker image" \\
      org.opencontainers.image.source="https://github.com/siderolabs/talos" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

ENV DEBIAN_FRONTEND=noninteractive
ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local
ENV GOFLAGS=-buildvcs=false -mod=mod

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && rm -rf /var/lib/apt/lists/*

{code}

CMD ["/bin/bash"]
"""


class TalosEra1ImageDefault(Image):
    """Per-PR image: checkout base commit, prefetch modules (workspace and
    pkg/machinery), run the targeted Go unit tests per-dir."""

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
        return TalosEra1ImageBase(self.pr, self._config)

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
# Root module download (go.work post-1.2 downloads workspace members too).
timeout 600 go mod download || true
# Pre-go.work era PRs (4570-5968) need explicit per-submodule download.
if [ -f pkg/machinery/go.mod ]; then
    ( cd pkg/machinery && timeout 300 go mod download ) || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# Per-dir `go test` automatically resolves to the nearest go.mod, handling
# pkg/machinery and other nested submodules without extra logic.
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -v -json -count=1 . ) 2>&1 || true; RAN=1; fi
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
    --exclude='*.test' --exclude='*.wasm' --exclude='*.exe' --exclude='*.db' \\
    --exclude='*.efi' --exclude='*.descriptors')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
# All 69 PRs touch go.mod (100% rate) — reinstall unconditionally.
if grep -qE '^diff --git a/(\\S*/)?go\\.(mod|sum)' /home/test.patch 2>/dev/null; then
    timeout 600 go mod download || true
    if [ -f pkg/machinery/go.mod ]; then
        ( cd pkg/machinery && timeout 300 go mod download ) || true
    fi
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -v -json -count=1 . ) 2>&1 || true; RAN=1; fi
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
    --exclude='*.test' --exclude='*.wasm' --exclude='*.exe' --exclude='*.db' \\
    --exclude='*.efi' --exclude='*.descriptors')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(\\S*/)?go\\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 go mod download || true
    if [ -f pkg/machinery/go.mod ]; then
        ( cd pkg/machinery && timeout 300 go mod download ) || true
    fi
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -v -json -count=1 . ) 2>&1 || true; RAN=1; fi
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

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{self.pr.repo}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=$BASE_COMMIT

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
"""


class TALOS_8243_TO_4570(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TalosEra1ImageDefault(self.pr, self._config)

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


_BUNDLE_NIS_ERA3 = [
    "4570-4573",
    "4609-4610",
    "5290-5292",
    "5399-5439-5452-5459-5462",
    "5492-5501-5532-5539-5561-5567-5572",
    "5791-5821-5836-5901-5903",
    "5968-5972-5973",
    "6194-6222-6227-6228",
    "6252-6257-6262-6263",
    "6259-6271-6299-6300-6304",
    "6315-6336-6369-6376-6384-6386",
    "6418-6447-6449",
    "6491-6538-6542",
    "6690-6691",
    "6728-6731",
    "6777-6782",
    "6827-6830-6831",
    "6851-6866-6867",
    "6932-6953-6954",
    "7148-7149",
    "7192-7194",
    "7332-7414-7417-7419",
    "7600-7601",
    "7650-7653-7655",
    "7714-7723",
    "7778-7782",
    "7843-7863-7866",
    "8243-8244",
]
for _ni in _BUNDLE_NIS_ERA3:
    Instance._registry[f"siderolabs/{_ni}"] = TALOS_8243_TO_4570
