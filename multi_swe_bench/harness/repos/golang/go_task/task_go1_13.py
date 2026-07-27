"""go-task/task harness for Go 1.13 era.

Covers number_interval: task_go1_13.

PRs #152-485: modules v2 -> v3 transition, Go 1.13 compatible.
Uses golang:1.13 as the Docker base image.
Covers both v2 (with vendor) and early v3 (modules-only) PRs.
Tests require the ``task`` binary in PATH, so ``go install`` runs first.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.13"
_TAG_SUFFIX = "go1_13"

# Toolchain env this era needs, emitted into the shared base image.
_ERA_ENV = ""

# Package set copied from the default list in Image.dockerfile() (image.py) so
# this hand-written base provisions exactly what the shared build provisions.
# `apt-get update` falls back to archive.debian.org because the older golang
# images ride Debian releases whose mirrors have been retired -- the same fix
# Image._get_apt_update_command() applies, keyed off reachability rather than
# the fixed DEPRECATED_DEBIAN_IMAGES list (which does not name golang tags).
_APT_INSTALL = (
    "RUN { apt-get update 2>/dev/null || "
    "{ sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && "
    "sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && "
    "sed -i '/-updates/d' /etc/apt/sources.list && "
    "apt-get update; }; } && \\\n"
    "    apt-get install -y --no-install-recommends \\\n"
    "    ca-certificates \\\n"
    "    curl \\\n"
    "    build-essential \\\n"
    "    git \\\n"
    "    gnupg \\\n"
    "    make \\\n"
    "    python3 \\\n"
    "    sudo \\\n"
    "    wget \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)


def _checkout_dir(pr: PullRequest) -> str:
    """Path the repo is cloned to inside the image."""
    return f"/home/{pr.repo}"


class _ImageBase(Image):
    """Level 1: toolchain-only base image, shared by every PR of the era.

    ``dependency()`` returns a *string*, so ``DockerfileEnhancer.enhance()``
    (image.py) engages and prepends the ``# syntax`` directive, the
    ``REPO_URL``/``BASE_COMMIT`` ARGs, the proxy/TZ/cert ENV block, the OCI
    labels and the CA-cert symlinks.

    This image deliberately does NOT clone the repository. A shared image that
    clones would be rewritten by ``DockerfileEnhancer._standardize_repo_fetch``
    into a ``${BASE_COMMIT}`` checkout plus ``Image._HARDENING_BLOCK``, pinning
    the whole era to whichever PR happened to build the base first and deleting
    the history every other PR needs. The clone therefore lives in
    ``_ImageDefault``, per PR.
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

    def dependency(self) -> Union[str, "Image"]:
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        sections = [f"FROM {_GO_IMAGE}", "WORKDIR /home/", _APT_INSTALL]

        if _ERA_ENV:
            sections.append(_ERA_ENV)
        if self.global_env:
            sections.append(self.global_env)
        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class _ImageDefault(Image):
    """Level 2: per-PR image built on the shared era base.

    ``dependency()`` returns an ``Image``, so ``DockerfileEnhancer.enhance()``
    returns this Dockerfile verbatim -- which is why the clone, the
    ``${BASE_COMMIT}`` checkout and the history strip are spelled out here. The
    strip is the canonical ``Image._HARDENING_BLOCK`` (concatenated raw so its
    ``${BASE_COMMIT}`` and ``%(refname)`` tokens stay literal), so the fix
    cannot be recovered from git history while ``base.sha`` stays reachable as
    HEAD.
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

    def dependency(self) -> Image | None:
        return _ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        checkout_dir = _checkout_dir(self.pr)

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
                "prepare.sh",
                """#!/bin/bash

# The Dockerfile already cloned the repo and checked out ${{BASE_COMMIT}}, so
# this only asserts a clean tree and warms the module/build caches.
cd {checkout_dir}
git reset --hard
bash /home/check_git_changes.sh

go install -v ./... || true
go test -v -count=1 ./... || true

# Warming the caches can rewrite go.mod/go.sum; restore the tracked tree so the
# image ships base.sha byte-for-byte and the eval patches apply cleanly.
git reset --hard || true

""".format(checkout_dir=checkout_dir),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd {checkout_dir}
go install -v ./... || true
# The package list is derived from the patch diff headers, but a patch adds,
# renames and deletes files: a directory named there may not exist at THIS
# stage (created by the fix, or removed by it). `go test` treats a missing
# package as a fatal error and aborts before running anything, so keep only
# the directories that exist right now and still hold .go files.
PKGS=""
for d in $(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$'); do
  # vendor/ holds third-party code: not ours to test, and in module mode it is
  # not part of this module at all.
  case "$d" in ./vendor/*) continue;; esac
  # Let the toolchain decide what is a real package. A directory can exist and
  # hold .go files yet still not be a package -- e.g. every file excluded by a
  # build constraint such as `//+build ignore` -- and `go test` treats that as
  # a fatal "cannot find module for path" before running anything.
  go list "$d" >/dev/null 2>&1 || continue
  PKGS="$PKGS $d"
done
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -v -count=1 -timeout 15m $PKGS

""".format(checkout_dir=checkout_dir),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd {checkout_dir}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go install -v ./... || true
# The package list is derived from the patch diff headers, but a patch adds,
# renames and deletes files: a directory named there may not exist at THIS
# stage (created by the fix, or removed by it). `go test` treats a missing
# package as a fatal error and aborts before running anything, so keep only
# the directories that exist right now and still hold .go files.
PKGS=""
for d in $(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$'); do
  # vendor/ holds third-party code: not ours to test, and in module mode it is
  # not part of this module at all.
  case "$d" in ./vendor/*) continue;; esac
  # Let the toolchain decide what is a real package. A directory can exist and
  # hold .go files yet still not be a package -- e.g. every file excluded by a
  # build constraint such as `//+build ignore` -- and `go test` treats that as
  # a fatal "cannot find module for path" before running anything.
  go list "$d" >/dev/null 2>&1 || continue
  PKGS="$PKGS $d"
done
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -v -count=1 -timeout 15m $PKGS

""".format(checkout_dir=checkout_dir),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd {checkout_dir}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go install -v ./... || true
# The package list is derived from the patch diff headers, but a patch adds,
# renames and deletes files: a directory named there may not exist at THIS
# stage (created by the fix, or removed by it). `go test` treats a missing
# package as a fatal error and aborts before running anything, so keep only
# the directories that exist right now and still hold .go files.
PKGS=""
for d in $(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$'); do
  # vendor/ holds third-party code: not ours to test, and in module mode it is
  # not part of this module at all.
  case "$d" in ./vendor/*) continue;; esac
  # Let the toolchain decide what is a real package. A directory can exist and
  # hold .go files yet still not be a package -- e.g. every file excluded by a
  # build constraint such as `//+build ignore` -- and `go test` treats that as
  # a fatal "cannot find module for path" before running anything.
  go list "$d" >/dev/null 2>&1 || continue
  PKGS="$PKGS $d"
done
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -v -count=1 -timeout 15m $PKGS

""".format(checkout_dir=checkout_dir),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        checkout_dir = _checkout_dir(self.pr)
        copy_files = " ".join(file.name for file in self.files())

        header = f"""FROM {base.image_name()}:{base.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN mkdir -p {checkout_dir} && git clone https://github.com/{self.pr.org}/{self.pr.repo}.git {checkout_dir}

WORKDIR {checkout_dir}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh || true

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""

        return header + Image._HARDENING_BLOCK + tail


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


class _TaskInstanceBase(Instance):
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


@Instance.register("go-task", "task_go1_13")
class TaskGo1_13(_TaskInstanceBase):
    pass


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Every record in go-task__task_lht_final.jsonl carries a `number_interval` that
# is its `prs_in_bundle` joined by "-" (e.g. "101-204-305-409"). Instance.create()
# prefers that key, looking up f"go-task/{number_interval}", so each delivered
# bundle whose toolchain requirement lands in this era (golang:1.13) is registered
# to TaskGo1_13. A bundle sits in the era of the highest Go version it needs -- the
# `go` directive at its base sha, or a higher one introduced by its patches.
_BUNDLE_NIS_TASK_GO1_13 = [
    "152-154-156-157-159",
    "164-165-172-173",
    "175-180-182",
    "188-198-200",
    "205-211-212-213",
    "207-216-219-220-237-246-292-311-337-347-349-356",
    "228-239-245",
    "248-249-261",
    "263-266-271",
    "281-283-286-298-302-303-315-317-328-329-330",
    "358-364-366-371-372-385-387-406-414",
    "407-415",
    "460-462-463-468-469-470-471",
    "485-489-490-491-496",
]
for _ni in _BUNDLE_NIS_TASK_GO1_13:
    Instance.register("go-task", _ni)(TaskGo1_13)
