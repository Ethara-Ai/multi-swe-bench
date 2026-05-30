import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Shared base image for every atlas PR.

    Contents: golang:1.22 + git + a full git clone of ariga/atlas at HEAD.
    Per-PR checkout is deferred to prepare.sh so this layer dedupes
    across all 73 PRs (one image_tag = "base") and is built once.

    The clone uses ${REPO_URL} explicitly so DockerfileEnhancer's
    `_standardize_repo_fetch` skips it (its negative-lookahead pattern
    excludes lines that already reference ${REPO_URL}). That keeps the
    base free of a BASE_COMMIT-based checkout, so all PRs share it.
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

    def dependency(self) -> str:
        return "golang:1.22"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Use a HARDCODED URL so DockerfileEnhancer's _standardize_repo_fetch
        # rewrites this line into the standard clone + WORKDIR + reset +
        # checkout ${{BASE_COMMIT}} + CMD block. That keeps the base
        # validator-clean. The per-PR prepare.sh still does its own
        # `git checkout {{pr.base.sha}}` on top, so the base layer is still
        # effectively shared across all 73 PRs via image_tag="base".
        return f"""FROM golang:1.22
ENV GOTOOLCHAIN=auto
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /home/
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
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

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
                "run_tests.sh",
                """#!/bin/bash
# Iterate over every Go module in the repo and run its unit tests.
# Skips internal/integration which requires a live database.
# Per-module `|| true` is intentional: in this multi-module repo one
# module's failures must not stop the others. parse_log still sees the
# real --- PASS/FAIL/SKIP lines emitted by go test.
set -o pipefail
export GOTOOLCHAIN=auto
export CI=true
cd /home/{pr.repo}
for mod in $(find . -maxdepth 4 -name go.mod | sort); do
    dir=$(dirname "$mod")
    if [ "$dir" = "./internal/integration" ]; then
        echo "=== SKIP $dir (requires DB) ==="
        continue
    fi
    echo "=== Running tests in $dir ==="
    (cd "$dir" && go test -v -count=1 ./...) || true
done
""".format(pr=self.pr),
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

export GOTOOLCHAIN=auto
for mod in $(find . -maxdepth 4 -name go.mod | sort); do
    dir=$(dirname "$mod")
    if [ "$dir" = "./internal/integration" ]; then continue; fi
    (cd "$dir" && go mod download) || true
done

bash /home/run_tests.sh || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/run_tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_name()}:{base.image_tag()}
{copy_commands}RUN bash /home/prepare.sh
"""


@Instance.register("ariga", "atlas")
class Atlas(Instance):
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
        # Strip ANSI codes before any regex matching.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^\s*--- PASS:\s+(\S+)")
        re_fail = re.compile(r"^\s*--- FAIL:\s+(\S+)")
        re_skip = re.compile(r"^\s*--- SKIP:\s+(\S+)")
        # Package summary lines, e.g.:
        #   ok      ariga.io/atlas/sql/mysql        0.105s
        #   FAIL    ariga.io/atlas/sql/postgres     0.083s
        #   FAIL    ariga.io/atlas/schemahcl [build failed]
        re_pkg_ok = re.compile(r"^ok\s+(\S+)\s")
        re_pkg_fail = re.compile(r"^FAIL\s+(\S+)(?:\s|$)")

        # Atlas is a multi-module Go repo: `TestX` exists in sql/mysql,
        # sql/postgres, sql/sqlite, etc. Disambiguate by prefixing the
        # package path captured from the summary line that closes each
        # package's section.
        buffer: list[tuple[str, str]] = []

        def flush(pkg: str) -> None:
            for status, name in buffer:
                full = f"{pkg}::{name}"
                if status == "pass":
                    if full in failed_tests:
                        continue
                    skipped_tests.discard(full)
                    passed_tests.add(full)
                elif status == "fail":
                    passed_tests.discard(full)
                    skipped_tests.discard(full)
                    failed_tests.add(full)
                elif status == "skip":
                    if full in passed_tests or full in failed_tests:
                        continue
                    skipped_tests.add(full)
            buffer.clear()

        for line in test_log.splitlines():
            m = re_pass.match(line)
            if m:
                buffer.append(("pass", m.group(1)))
                continue
            m = re_fail.match(line)
            if m:
                buffer.append(("fail", m.group(1)))
                continue
            m = re_skip.match(line)
            if m:
                buffer.append(("skip", m.group(1)))
                continue
            m = re_pkg_ok.match(line)
            if m:
                flush(m.group(1))
                continue
            m = re_pkg_fail.match(line)
            if m:
                flush(m.group(1))
                continue

        if buffer:
            flush("__unknown__")

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
