import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# canonical/lxd — system container & VM manager (Go).
#
# Discovery (dataset analysis):
#  - 69-PR Go range #3046..#17269, single Go module.
#  - Base refs: master (44), main (13), and stable-2.0/3.0/4.0/5.0/5.21
#    release branches (a handful each).
#  - Test files concentrate in lxd/ (controller), lxc/ (cli), lxc-to-lxd/
#    (migration), client/, shared/.
#  - lxd has a HEAVY cgo dependency on Canonical's dqlite (a SQLite fork)
#    and liblxc (system container runtime). Neither is in standard Debian
#    repos — both must be built from source. The Makefile's `make deps`
#    target clones canonical/dqlite + lxc/lxc, autoreconfs/mesons them,
#    and installs into a private prefix; the resulting CGO_CFLAGS /
#    CGO_LDFLAGS / LD_LIBRARY_PATH / PKG_CONFIG_PATH are emitted by
#    `make env`. Without those libs the build tag `libsqlite3` stays
#    unset and most lxd/* packages won't compile.
#  - Strategy: build dqlite + liblxc ONCE in the base image (against a
#    recent SHA), capture `make env` to a script, and per-PR sourcing
#    it gives the right CGO flags. Older PRs whose dqlite/liblxc ABI
#    drifted past the baked version will fail — accepted loss.


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if path.endswith("_test.go"):
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


class LxdImageBase(Image):
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
        return "golang:1-bookworm"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # System deps from lxd's doc/installing.md + meson/ninja for liblxc
        # build. xdelta3 is required by some shared SimpleStreams tests.
        # libcap2-bin gives setcap which `make deps` sometimes needs.
        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
ENV CGO_ENABLED=1
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential pkg-config \\
    autoconf automake libtool meson ninja-build \\
    libacl1-dev libapparmor-dev libcap-dev liblz4-dev libseccomp-dev \\
    libsqlite3-dev libudev-dev libuv1-dev libdbus-1-dev liburing-dev \\
    libcap2-bin xdelta3 \\
    && rm -rf /var/lib/apt/lists/*

{code}

# Build dqlite + liblxc once in the base image. The resulting headers,
# libs, and pkg-config files are placed under /home/lxd/.deps; `make env`
# emits the CGO/LD_LIBRARY/PKG_CONFIG paths we need.
RUN cd /home/{self.pr.repo} && \\
    git config --global --add safe.directory /home/{self.pr.repo} && \\
    (make deps 2>&1 | tail -5) || \\
    echo "make deps failed in base — older PRs may still work via go.mod-pinned libs"

# Persist the env so per-PR scripts can `source` it deterministically.
RUN cd /home/{self.pr.repo} && \\
    (make -s env 2>/dev/null || true) > /home/lxd_env.sh && \\
    chmod +x /home/lxd_env.sh && \\
    cat /home/lxd_env.sh || true

{self.clear_env}

"""


class LxdImageDefault(Image):
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
        return LxdImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        check_git = """#!/bin/bash
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
"""

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

go mod download 2>/dev/null || true
""".replace("__REPO__", repo).replace("__SHA__", sha)

        # Per-package `go test`. Source the saved lxd env so CGO finds the
        # base-built dqlite + liblxc. -vet=off avoids vet-only failures
        # masking the real test outcome.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
source /home/lxd_env.sh 2>/dev/null || true
export CGO_LDFLAGS_ALLOW="(-Wl,-wrap,pthread_create)|(-Wl,-z,now)"
# TAG_SQLITE3 is "libsqlite3" if dqlite.h is reachable (base build).
TAG_SQLITE3=$(printf "#include <dqlite.h>\\nvoid main(){dqlite_node_id n = 1;}" | \\
  cc ${CGO_CFLAGS:-} -o /dev/null -xc - >/dev/null 2>&1 && echo "libsqlite3")

# Two eras: modern lxd has go.mod at root and module is github.com/canonical/lxd;
# pre-2021 lxd was github.com/lxc/lxd and used GOPATH + vendor/ (no go.mod).
# Detect by presence of go.mod and switch modes accordingly.
if [ -f go.mod ]; then
  go mod download 2>/dev/null || true
  for pkg in __PKGS__; do
    [ -d "$pkg" ] || continue
    echo "### LXDPKG: $pkg ###"
    go test -v -count=1 -vet=off -timeout=20m -tags "$TAG_SQLITE3" "./$pkg/" 2>&1 || true
  done
else
  # GOPATH layout: place /home/lxd under $GOPATH/src/github.com/lxc/lxd
  export GOPATH=/go
  export GO111MODULE=off
  export PATH="$GOPATH/bin:$PATH"
  MODPATH="$GOPATH/src/github.com/lxc/lxd"
  mkdir -p "$(dirname $MODPATH)"
  [ ! -e "$MODPATH" ] && ln -sf /home/__REPO__ "$MODPATH"
  for pkg in __PKGS__; do
    [ -d "$pkg" ] || continue
    echo "### LXDPKG: $pkg ###"
    (cd "$MODPATH/$pkg" && \\
      go test -v -count=1 -vet=off -timeout=20m -tags "$TAG_SQLITE3" . 2>&1) || true
  done
fi
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        # `git apply --3way` is all-or-nothing across the whole patch — split
        # by file boundaries and apply each independently so unrelated files
        # still land when a stale hunk on one file can't merge.
        apply_split = """split_apply() {
  local pf="$1"
  local td
  td=$(mktemp -d)
  awk 'BEGIN{i=0} /^diff --git /{i++; f=sprintf("%s/p%04d.patch","'"$td"'",i)} f{print > f}' "$pf"
  local applied=0 failed=0
  for f in "$td"/p*.patch; do
    [ -s "$f" ] || continue
    if git apply --3way --whitespace=nowarn __EXCLUDES__ "$f" >/dev/null 2>&1; then
      applied=$((applied+1))
    elif git apply --whitespace=nowarn __EXCLUDES__ "$f" >/dev/null 2>&1; then
      applied=$((applied+1))
    else
      failed=$((failed+1))
    fi
  done
  rm -rf "$td"
  echo "split_apply $pf: applied=$applied failed=$failed"
}
""".replace("__EXCLUDES__", excludes)

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
__APPLY_SPLIT__
split_apply /home/test.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__APPLY_SPLIT__", apply_split)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
__APPLY_SPLIT__
split_apply /home/test.patch
split_apply /home/fix.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__APPLY_SPLIT__", apply_split)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
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


@Instance.register("canonical", "lxd")
class Lxd(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LxdImageDefault(self.pr, self._config)

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
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestInstance (0.01s)
        #   --- FAIL: TestDqlite (0.02s)
        #   --- SKIP: TestLxc (0.00s)
        # Fenced by `### LXDPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### LXDPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.rstrip()
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            elif status == "SKIP":
                skipped_tests.add(tid)

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
