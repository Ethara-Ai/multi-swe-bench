"""google/syzkaller harness for the GOPATH era (PR < 1700).

Covers number_interval: syzkaller_0_to_1699.

Pre-modules syzkaller had no go.mod (the file first appeared around
PR #1928). Sources had to live under
``$GOPATH/src/github.com/google/syzkaller`` and ``GO111MODULE=off``
was required. The base image is ``golang:1.13-buster`` because that's
the newest Go that still supports GOPATH-mode builds cleanly while
also being able to clone the older vendored dependencies. Debian
Buster is EOL so package archives are redirected to archive.debian.org.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.13-buster"
_TAG_SUFFIX = "gopath"


class _ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        gopath_pkg = f"github.com/{self.pr.org}/{self.pr.repo}"

        if self.config.need_clone:
            code = (
                f"RUN mkdir -p /go/src/{gopath_pkg} && "
                f"git clone https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/go/src/{gopath_pkg}"
            )
        else:
            code = f"COPY {self.pr.repo} /go/src/{gopath_pkg}"

        return f"""FROM {image_name}

ENV DEBIAN_FRONTEND=noninteractive
ENV GO111MODULE=off
ENV GOPATH=/go
ENV PATH=/go/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g' /etc/apt/sources.list && \\
    sed -i 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' /etc/apt/sources.list && \\
    sed -i '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    git make gcc g++ pkg-config ca-certificates clang-format \\
    && rm -rf /var/lib/apt/lists/*

{self.global_env}

{code}

WORKDIR /go/src/{gopath_pkg}

{self.clear_env}

"""


class _ImageDefault(Image):
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
        return _ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        gopath_pkg = f"github.com/{self.pr.org}/{self.pr.repo}"

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

""".format(),
            ),
            File(
                ".",
                "syz_setup.sh",
                """#!/bin/bash
# Best-effort: generate syscall descriptions. Older revisions (PR<1689)
# expose `make generate`; newer ones use `make descriptions`. Failures
# are tolerated because many unit tests don't actually need the
# generated code, and tests that do will simply fail to compile (and
# be reported as failed by the harness).
cd /go/src/{gopath_pkg}
if grep -qE '^descriptions:' Makefile 2>/dev/null; then
  make descriptions 2>&1 | tail -30 || true
elif grep -qE '^generate:' Makefile 2>/dev/null; then
  make generate 2>&1 | tail -30 || true
fi
""".format(gopath_pkg=gopath_pkg),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash

cd /go/src/{gopath_pkg}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

bash /home/syz_setup.sh || true
go test -short -count=1 ./... 2>&1 | tail -50 || true

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
bash /home/syz_setup.sh || true
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done)
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -short -v -count=1 -timeout 20m $PKGS

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/syz_setup.sh || true
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done)
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -short -v -count=1 -timeout 20m $PKGS

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/syz_setup.sh || true
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done)
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -short -v -count=1 -timeout 20m $PKGS

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
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


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass = re.compile(r"--- PASS: (\S+)")
    re_fail = [
        re.compile(r"--- FAIL: (\S+)"),
        re.compile(r"FAIL:?\s?(.+?)\s"),
    ]
    re_skip = re.compile(r"--- SKIP: (\S+)")

    for line in test_log.splitlines():
        line = line.strip()

        m = re_pass.match(line)
        if m:
            name = m.group(1)
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)

        for rp in re_fail:
            m = rp.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)

        m = re_skip.match(line)
        if m:
            name = m.group(1)
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("google", "syzkaller_0_to_1699")
class Syzkaller_0_to_1699(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)
