
from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class BatchToolImageBase(Image):
    """Toolchain + cloned source. Built before the PR image."""

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
        return "golang:1.24-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

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

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV LC_ALL=C.UTF-8
ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}
"""


class BatchToolImageDefault(Image):
    """Per-PR layer: patches, graded scripts, warmed module/build caches."""

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
        return BatchToolImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    # Bare "pr" tag -- same single-PR precondition as the base layer above.
    def image_tag(self) -> str:
        return "pr"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = """export CI=true

GO_LOG="$(mktemp)"

# `cmd && RC=0 || RC=$?` is an AND-OR list, so `set -e` does not fire on a
# failing suite and the status is preserved instead of discarded by `|| true`.
cd /home/{pr.repo}
go test -v -count=1 -timeout 900s ./... > "$GO_LOG" 2>&1 && GO_RC=0 || GO_RC=$?
cat "$GO_LOG"
# A run that produced results always emits at least one `ok <pkg>`,
# `? <pkg> [no test files]` or `--- PASS/FAIL/SKIP:` line.  A tree where every
# package fails to compile emits only `FAIL <pkg> [build failed]`, which is why
# a bare FAIL is not accepted here.  The test stage -- where package `call`
# legitimately fails to build -- still emits `ok` for the other eleven
# packages, so it clears this gate as intended.
if ! grep -qE '(^ok\\s|^\\?\\s|--- (PASS|FAIL|SKIP):)' "$GO_LOG"; then
    echo "harness: go test produced no test results (exit $GO_RC)" >&2
    exit 1
fi

exit 0
""".format(pr=self.pr)

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

# Pass 1: the base-commit dependency set.
go mod download || true
go test -count=1 -run 'ZZZ_NO_TEST_MATCHES' ./... || true

# Pass 2: the post-fix dependency set.  fix.patch adds golang.org/x/sync
# v0.16.0 to go.mod/go.sum, so this is the only way to get that module into
# /go/pkg/mod at build time instead of at graded-run time.  `|| true` keeps a
# transient registry failure from aborting the image build -- it would then
# surface downstream as an empty TestResult, which Report.check() rejects on
# the fix_patch_result.all_count > 0 rule, rather than as an opaque build
# error.
git apply --whitespace=nowarn /home/fix.patch
go mod download || true
go test -count=1 -run 'ZZZ_NO_TEST_MATCHES' ./... || true

# Revert via reset+clean rather than `git apply -R`.  A reverse apply has to
# match the working tree byte for byte and fails on any line-ending or
# whitespace conversion git performed on the way in; reset+clean is defined by
# the commit, not by the patch, so it cannot half-revert.  `git apply` never
# stages, so every file the patch created is untracked and falls to clean -fd.
git reset --hard
git clean -fd

# Proves the warm-up patch was fully reverted; the graded runs start clean.
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

"""
                + test_cmd,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

""".format(pr=self.pr)
                + test_cmd,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

""".format(pr=self.pr)
                + test_cmd,
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


@Instance.register("ryclarke", "batch-tool")
class BatchTool(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BatchToolImageDefault(self.pr, self._config)

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
        """Parse `go test -v ./...` output.

        Tests are keyed by the Go import path of the package that declares
        them, then "::", then the test name -- and, for subtests, the full
        slash-joined path:

            github.com/ryclarke/batch-tool/scm/github::TestNew
            github.com/ryclarke/batch-tool/catalog::TestPrintFunctions/PrintLabels

        The package prefix is NOT decoration.  Bare `TestXxx` names are unique
        only *within* a package, and this repo breaks that assumption three
        ways over: `TestNew` is declared in scm/bitbucket/provider_test.go:11,
        scm/fake/provider_test.go:10 AND scm/github/provider_test.go:9.  Keying
        on the bare name collapses those three distinct tests into one entry,
        silently dropping two tests of P2P coverage; worse, the
        `passed -= failed` reconciliation below would then let one package's
        failure mark all three as failed.  Measured on the real three-stage
        logs, bare-name keying loses exactly 2 of 195/176/200 status lines per
        stage -- a Check 4A collision, not a hypothetical.

        Names carry no timing, no run-order metadata and no counts, and the
        import path is fixed by go.mod, so the same test yields a
        byte-identical key in all three graded stages (report.py's Rule 4
        anomaly is driven by exactly that kind of drift).

        The `::` separator is chosen to cooperate with report.py:
        `_candidate_identifiers()` splits on `::` and takes the tail, so
        `_authored_via_diff()` still recognises "TestDoBatching" in the test
        patch's added lines and classifies the seven new cases as n2p.  And
        `_test_name_matches_files()` takes the head -- an import path, which
        matches no file in fix_patch_files -- so the Rule 5 cheating guard
        cannot mis-fire on it.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        re_go = re.compile(r"^--- (PASS|FAIL|SKIP):\s+(\S+)")

        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+)")

        pending: list[tuple[str, str]] = []

        def flush(package: str) -> None:
            for status, name in pending:
                key = f"{package}::{name}" if package else name
                if status == "PASS":
                    passed_tests.add(key)
                elif status == "FAIL":
                    failed_tests.add(key)
                else:
                    skipped_tests.add(key)
            pending.clear()

        for raw_line in test_log.splitlines():
            stripped = ansi.sub("", raw_line).strip()
            if not stripped:
                continue

            match = re_go.match(stripped)
            if match:
                pending.append((match.group(1), match.group(2)))
                continue

            pkg_match = re_pkg.match(stripped)
            if pkg_match:
                flush(pkg_match.group(1))

        flush("")

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
