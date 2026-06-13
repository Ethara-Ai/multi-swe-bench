import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# grafana/pyroscope — continuous profiling backend (Go multi-module repo).
#
# Discovery (dataset analysis):
#  - 87-PR Go range #2177..#4974, all base ref `main`.
#  - Multi-module: at various SHAs the repo contains go.mod files at `.`,
#    `api/`, `og/`, `ebpf/` (older), `lidia/`, `examples/golang-pgo/`. The
#    set varies over time, so run_tests.sh walks each test package up to its
#    nearest go.mod ancestor and runs `go test` from there with the
#    submodule-relative path.
#  - Test files (by top-level dir): pkg/ (66), ebpf/ (8), cmd/ (4), lidia/
#    (3), examples/ (2), operations/ (2), og/ (1).
#  - go.mod ranges from go 1.19 (earliest era) to go 1.25.x (latest), with a
#    `toolchain` directive in newer modules. GOTOOLCHAIN=auto lets each PR
#    self-fetch the right Go toolchain.
#  - A handful of packages use cgo (compactor/speedscope/tree); CGO_ENABLED=1
#    plus build-essential covers them.
#  - Runs are fenced with `### PYRPKG ###` markers so test ids stay unique
#    across modules and packages.


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


class PyroscopeImageBase(Image):
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

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV GOTOOLCHAIN=auto
ENV CGO_ENABLED=1
# Pyroscope ships a go.work at most SHAs; let Go's default workspace
# resolution stand. -mod=mod is incompatible with workspace mode.
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class PyroscopeImageDefault(Image):
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
        return PyroscopeImageBase(self.pr, self._config)

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

# Warm the Go module cache for every go.mod found at the base commit.
for modfile in $(find . -maxdepth 4 -name go.mod -type f 2>/dev/null); do
  (cd "$(dirname "$modfile")" && go mod download 2>/dev/null) || true
done
""".replace("__REPO__", repo).replace("__SHA__", sha)

        # Per-test-package go test. For each pkg dir, walk up to the nearest
        # go.mod ancestor and run `go test` from that module root using the
        # submodule-relative path. -vet=off avoids vet-only failures masking
        # the real test outcome.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__

# Workspace mode (go.work) is active when present so cross-module imports
# (e.g. pkg/ingester/otlp -> api/otlp/common/v1) resolve via the local
# sibling module instead of failing with "no required module provides...".

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  # Find nearest go.mod ancestor.
  d="$pkg"
  while [ -n "$d" ] && [ ! -f "$d/go.mod" ]; do
    parent=$(dirname "$d")
    if [ "$parent" = "$d" ]; then d=""; break; fi
    d="$parent"
  done
  if [ -z "$d" ] || [ ! -f "$d/go.mod" ]; then
    modroot="."
    rel="$pkg"
  elif [ "$d" = "." ]; then
    modroot="."
    rel="$pkg"
  else
    modroot="$d"
    rel="${pkg#$d/}"
    [ "$rel" = "$pkg" ] && rel="."
  fi
  echo "### PYRPKG: $pkg ###"
  (cd "$modroot" && go mod download 2>/dev/null; \\
   go test -v -count=1 -vet=off -timeout=20m "./$rel/" 2>&1) || true
done
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

        # `git apply --3way` is all-or-nothing across the whole patch — if any
        # single hunk fails the 3way merge, none of the patch's NEW files get
        # created either. Split the patch by `diff --git` boundaries and apply
        # each file's hunks independently, so unrelated files (e.g. new
        # generated .pb.go under a sibling sub-module) still land when an
        # unrelated hunk on a stale file (e.g. go.work.sum) can't merge.
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


@Instance.register("grafana", "pyroscope")
class Pyroscope(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PyroscopeImageDefault(self.pr, self._config)

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
        #   --- PASS: TestProfile (0.01s)
        #   --- FAIL: TestCompactor (0.02s)
        #   --- SKIP: TestEBPF (0.00s)
        # Fenced by `### PYRPKG: <pkg> ###` so ids stay unique across modules.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### PYRPKG:\s+(\S+)\s+###")

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
