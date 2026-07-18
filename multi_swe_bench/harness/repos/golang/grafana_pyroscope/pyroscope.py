import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# grafana/pyroscope — continuous profiling backend (Go multi-module repo).
#
# Discovery (dataset analysis):
#  - 87-PR Go range #2177..#4974, all base ref `main`, 87 distinct base SHAs.
#  - Multi-module: at various SHAs the repo contains go.mod files at `.`,
#    `api/`, `og/`, `ebpf/` (older), `lidia/`, `examples/golang-pgo/`. The
#    set varies over time, so run_tests.sh walks each test package up to its
#    nearest go.mod ancestor and runs `go test` from there with the
#    submodule-relative path.
#  - Test files (by top-level dir): pkg/ (66), ebpf/ (8), cmd/ (4), lidia/
#    (3), examples/ (2), operations/ (2), og/ (1).
#  - go.mod ranges from go 1.19 (earliest era) to go 1.25.x (latest), with a
#    `toolchain` directive in newer modules. The base ships Go 1.26 and
#    GOTOOLCHAIN=auto lets each PR self-fetch the exact toolchain if a module
#    ever needs one newer than the base (never, for this historical range).
#  - A handful of packages use cgo (compactor/speedscope/tree); CGO_ENABLED=1
#    plus build-essential + pkg-config covers them.
#  - Runs are fenced with `### PYRPKG ###` markers so test ids stay unique
#    across modules and packages.
#
# Image layering (canonical two-level SAFE template, aligned with image.py's
# DockerfileEnhancer):
#  - PyroscopeImageBase: dependency() returns a *string* (the Go toolchain), so
#    the enhancer engages and prepends the syntax/ARG/ENV/LABEL infra block. It
#    must NOT clone -- a shared string-dependency image that clones is force-
#    pinned to a single ${BASE_COMMIT} and history-stripped by the enhancer,
#    which breaks `git checkout <base.sha>` for all 86 other PRs sharing the
#    base (the original bug behind the poor resolved counts). This level only
#    provides the toolchain, apt deps, and Go env -- ONE shared base image.
#  - PyroscopeImageDefault: dependency() returns the Base *Image* (not a
#    string), so the enhancer leaves this Dockerfile verbatim. The clone lives
#    here, per-PR: clone full history, `git checkout ${BASE_COMMIT}` inline,
#    warm caches, then the verbatim Image._HARDENING_BLOCK strips origin/refs/
#    future history (keeping base.sha reachable). Per-PR pinning is correct.


# Base toolchain: latest Go 1.x on Debian bookworm. Since local Go (1.26) is
# >= every go.mod `go`/`toolchain` directive in the dataset (1.19..1.25),
# GOTOOLCHAIN=auto resolves to the local toolchain and needs no network.
_GO_IMAGE = "golang:1-bookworm"


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


# Archive-resilient apt: bookworm is current, but keep the archive.debian.org
# fallback for when the mirror is eventually retired -- mirrors the deprecated-
# base handling image.py applies, keyed off runtime reachability. pkg-config +
# build-essential cover the cgo packages (compactor/speedscope/tree).
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
    "    pkg-config \\\n"
    "    git \\\n"
    "    gnupg \\\n"
    "    make \\\n"
    "    python3 \\\n"
    "    sudo \\\n"
    "    wget \\\n"
    "    patch \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)


class PyroscopeImageBase(Image):
    """Level 1: toolchain-only base image (shared by all PRs).

    ``dependency()`` returns a *string* (the Go toolchain image), so the
    pipeline's ``DockerfileEnhancer`` engages and prepends the
    ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image must NOT clone
    the repository -- a shared string-dependency image that performs a
    ``git clone`` is force-pinned to a single ``${BASE_COMMIT}`` and
    history-stripped by the enhancer, which would break ``git checkout`` for
    every other PR sharing the base. So the clone lives in PyroscopeImageDefault
    (whose dependency() is an Image, left verbatim by the enhancer), done
    per-PR. This image only provides the Go toolchain, apt deps, and Go env.
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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # No `git clone` here on purpose -- see the class docstring. The string
        # dependency means DockerfileEnhancer injects the ARG/ENV/LABEL infra
        # block (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {_GO_IMAGE}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive
ENV GOTOOLCHAIN=auto
ENV CGO_ENABLED=1
# Pyroscope ships a go.work at most SHAs; let Go's default workspace resolution
# stand. -mod=mod is incompatible with workspace mode, so GOFLAGS is left unset.

{_APT_INSTALL}

CMD ["/bin/bash"]
"""


class PyroscopeImageDefault(Image):
    """Level 2: per-PR image (built on the shared toolchain base).

    ``dependency()`` returns PyroscopeImageBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim -- no pin, no history
    strip injected by the pipeline. The clone therefore lives here, per-PR: the
    image clones full history, checks out ``${BASE_COMMIT}`` inline, COPYs the
    scripts, warms the module/build cache (install.sh), then the verbatim
    ``Image._HARDENING_BLOCK`` strips origin/refs/future history (with the four
    post-condition asserts + submodule pass) while keeping base.sha reachable.
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

        # Warm the Go module cache (and, under GOTOOLCHAIN=auto, self-fetch any
        # newer toolchain) for every go.mod at the base commit, and best-effort
        # build each module, so the three eval stages start from a compiled,
        # offline state. Runs BEFORE the hardening strip; everything is `|| true`
        # so a flaky baseline never breaks the image build.
        install = """#!/bin/bash
set -e
git config --global --add safe.directory /home/__REPO__ || true
cd /home/__REPO__

for modfile in $(find . -maxdepth 4 -name go.mod -type f 2>/dev/null); do
  d=$(dirname "$modfile")
  (cd "$d" && go mod download 2>/dev/null && go build ./... >/dev/null 2>&1) || true
done
""".replace("__REPO__", repo)

        # Per-test-package go test. For each pkg dir, walk up to the nearest
        # go.mod ancestor and run `go test` from that module root using the
        # submodule-relative path. -vet=off avoids vet-only failures masking
        # the real test outcome. Workspace mode (go.work) is active when present
        # so cross-module imports (e.g. pkg/ingester/otlp -> api/otlp/common/v1)
        # resolve via the local sibling module.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__

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

        # base.sha stays checkout-able after the hardening strip because it is
        # HEAD (reachable, not pruned). Reset+checkout gives each stage a clean
        # base tree even if stages share a container.
        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git reset --hard
git checkout __SHA__
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__SHA__", sha)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        # `git apply --3way` is all-or-nothing across the whole patch -- if any
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
git reset --hard
git checkout __SHA__
__APPLY_SPLIT__
split_apply /home/test.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__SHA__", sha).replace(
            "__APPLY_SPLIT__", apply_split
        )

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git reset --hard
git checkout __SHA__
__APPLY_SPLIT__
split_apply /home/test.patch
split_apply /home/fix.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__SHA__", sha).replace(
            "__APPLY_SPLIT__", apply_split
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", install),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim -- the clone + hardening below are kept as written
        # (and pinning here is correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/install.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


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


# Routing for the release-bundled number_interval keys.
#
# Each record carries number_interval = its sorted prs_in_bundle joined by "-"
# (e.g. "2177-3654-3655"), and Instance.create() looks up
# f"{org}/{number_interval}" whenever number_interval is non-empty (falling back
# to f"{org}/{repo}" only when it is ""). Rather than hardcode every bundle key,
# install a tiny routing shim on Instance._registry: any grafana key shaped like
# dash-joined PR numbers that is not explicitly registered resolves to Pyroscope.
# Explicit registrations are always checked first, and no sibling grafana repo
# (loki/mimir/grafana/...) uses digit-dash keys, so this never mis-routes them.
# The plain @Instance.register("grafana", "pyroscope") above still covers any
# record that leaves number_interval empty.
class _PyroscopeBundleRegistry(dict):
    """``Instance._registry`` drop-in that virtually maps every
    ``grafana/<dash-joined PR numbers>`` bundle key to :class:`Pyroscope`,
    so the per-record number_interval values need not be listed one by one."""

    _BUNDLE_KEY = re.compile(r"^grafana/[0-9]+(?:-[0-9]+)*$")

    def __contains__(self, key: object) -> bool:
        if super().__contains__(key):
            return True
        return isinstance(key, str) and self._BUNDLE_KEY.match(key) is not None

    def __getitem__(self, key):
        if super().__contains__(key):
            return super().__getitem__(key)
        if isinstance(key, str) and self._BUNDLE_KEY.match(key):
            return Pyroscope
        raise KeyError(key)


# Wrap the shared registry in place (idempotent) so both existing and future
# registrations keep working while grafana bundle keys route dynamically.
if not isinstance(Instance._registry, _PyroscopeBundleRegistry):
    Instance._registry = _PyroscopeBundleRegistry(Instance._registry)
