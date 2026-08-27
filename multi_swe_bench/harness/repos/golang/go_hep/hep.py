from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


TEST_SURFACE = "./groot/..."
WARM_SURFACE = "./groot/rtree/..."


class HepImageBase(Image):
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
        return "golang:1.14"

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

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}
"""


class HepImageDefault(Image):
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
        return HepImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "pr"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = """export CI=true

# groot/riofs, groot/rsrv and groot/rtree reach a live XRootD server in Lyon.
# Pinning it to loopback makes those tests fail fast and identically in every
# stage; without it a single dropped connection between the test and fix runs
# trips Report.check() rule 2 and invalidates the instance.  Fatal on failure,
# because a silently skipped pin puts the graded run back on the network.
if ! grep -q 'ccxrootdgotest.in2p3.fr' /etc/hosts; then
    if ! echo '127.0.0.1 ccxrootdgotest.in2p3.fr' >> /etc/hosts; then
        echo "harness: could not blackhole the XRootD test endpoint" >&2
        exit 1
    fi
fi

GO_LOG="$(mktemp)"

# The graded surface is the whole groot tree, not just groot/rtree.  Go links
# a package's tests into one binary, so the rtree tests cannot build until the
# fix patch lands -- grading rtree alone would leave the test stage completely
# empty.  Every other groot package still compiles and runs there, so the test
# stage reports real results and rtree's build failure stays visible.
#
# `cmd && RC=0 || RC=$?` is an AND-OR list, so `set -e` does not fire on a
# failing suite and the status is preserved instead of discarded by `|| true`.
cd /home/{pr.repo}
go test -v -count=1 -timeout 1800s {surface} > "$GO_LOG" 2>&1 && GO_RC=0 || GO_RC=$?
cat "$GO_LOG"

# A run that reached the toolchain always emits a recognisable verdict: `ok
# <pkg>`, `? <pkg> [no test files]`, a `--- PASS/FAIL/SKIP:` line, or -- for
# rtree in the test stage, by design -- `FAIL <pkg> [build failed]`.  A
# container with no usable Go matches none of them and stops here rather than
# handing parse_log an empty log that Report.check() rule 1 would reject with
# no diagnostic.
if ! grep -qE '(^ok\\s|^\\?\\s|--- (PASS|FAIL|SKIP):|\\[build failed\\])' "$GO_LOG"; then
    echo "harness: go test produced no recognizable verdict (exit $GO_RC)" >&2
    exit 1
fi

exit 0
""".format(pr=self.pr, surface=TEST_SURFACE)

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

# Pass 1: the base-commit dependency set.  Warming is scoped to rtree, not to
# the whole graded surface: the graded runs only ever execute on the host
# arch, where a cold `go test ./groot/...` costs ~26s, while warming the full
# tree here would recompile every groot package under arm64 emulation at
# image-build time for no graded-run benefit.
go mod download || true
go test -count=1 -run 'ZZZ_NO_TEST_MATCHES' {surface} || true

# Pass 2: the post-fix dependency set.  fix.patch adds
# github.com/containous/yaegi v0.8.1 to go.mod/go.sum, so this is the only way
# to get that module into /go/pkg/mod at build time instead of at graded-run
# time.  `|| true` keeps a transient registry failure from aborting the image
# build -- it would then surface downstream as a fix stage that fails to
# build, which Report.check() rejects on rule 1/3, rather than as an opaque
# build error.
git apply --whitespace=nowarn /home/fix.patch
go mod download || true
go test -count=1 -run 'ZZZ_NO_TEST_MATCHES' {surface} || true

# Revert via reset+clean rather than `git apply -R`.  A reverse apply has to
# match the working tree byte for byte and fails on any line-ending or
# whitespace conversion git performed on the way in; reset+clean is defined by
# the commit, not by the patch, so it cannot half-revert.  `git apply` never
# stages, so every file the patch created is untracked and falls to clean -fd.
git reset --hard
git clean -fd

# Proves the warm-up patch was fully reverted; the graded runs start clean.
bash /home/check_git_changes.sh
""".format(pr=self.pr, surface=WARM_SURFACE),
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


@Instance.register("go-hep", "hep")
class Hep(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HepImageDefault(self.pr, self._config)

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
