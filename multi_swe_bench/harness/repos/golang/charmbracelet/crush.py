import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# charmbracelet/crush — a Go-based terminal AI coding assistant.
#
# Discovery (dataset analysis):
#  - 74-PR Go range #323..#2730, single-module repo, almost entirely on `main`.
#  - All test files live under `internal/*` (74 PRs).
#  - module: github.com/charmbracelet/crush; go 1.26.3 (latest).
#  - A few cgo references (config/resolve_real_test.go, ui/model/keys.go etc.)
#    so CGO_ENABLED=1 and a C toolchain are required.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs each. Runs are fenced with `### CRUSHPKG ###`
#    markers so test ids stay unique across packages.
#
# Registry shape (aligned with harness/image.py DockerfileEnhancer):
#  - CrushImageBase: dependency() returns a *string* Go toolchain image, so the
#    pipeline's DockerfileEnhancer engages and prepends the ARG/ENV/LABEL infra.
#    It must NOT clone — a shared string-dep image that clones is force-pinned to
#    one ${BASE_COMMIT} and history-stripped by the enhancer, which would break
#    `git checkout` for the other 73 PRs sharing the base. So it is toolchain-only.
#  - CrushImageDefault: dependency() returns CrushImageBase (an Image, not a
#    string), so the enhancer returns its Dockerfile verbatim. The clone +
#    `git checkout ${BASE_COMMIT}` + verbatim Image._HARDENING_BLOCK therefore
#    live here, done per-PR (pinning here is correct: it is per-PR, not shared).

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


class CrushImageBase(Image):
    """Shared toolchain-only base image (built once, reused by all 74 PRs).

    ``dependency()`` returns a *string* (the Go toolchain image), so the
    pipeline's ``DockerfileEnhancer`` engages and prepends the
    ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image must NOT clone
    the repository — a shared string-dependency image that performs a
    ``git clone`` is force-pinned to a single ``${BASE_COMMIT}`` and
    history-stripped by the enhancer, breaking ``git checkout`` for every other
    PR sharing the base. So the clone lives in CrushImageDefault (per-PR). This
    image only provides the Go toolchain, the cgo C toolchain, and Go build env.
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
        # No `git clone` here on purpose — see the class docstring. The string
        # dependency means DockerfileEnhancer injects the ARG/ENV/LABEL infra
        # block (but no clone/hardening, since this Dockerfile has no clone).
        # golang:1-bookworm is Debian bookworm (apt mirror live), no archive fix.
        return f"""FROM {_GO_IMAGE}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential pkg-config patch \\
    && rm -rf /var/lib/apt/lists/*

ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
ENV CGO_ENABLED=1
# Several test packages (e.g. internal/llm/provider) run a TestMain that
# initializes config, which by default fetches the provider list over the
# network from catwalk.charm.sh. In an isolated build that fetch panics and
# aborts the whole package test binary (0 results). Force the embedded provider
# list so tests run fully offline.
ENV CRUSH_DISABLE_PROVIDER_AUTO_UPDATE=1

CMD ["/bin/bash"]
"""


class CrushImageDefault(Image):
    """Per-PR image (built on the shared toolchain base).

    ``dependency()`` returns CrushImageBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim — no pin, no history
    strip injected by the pipeline. The clone therefore lives here, per-PR: the
    image clones full history, checks out ``${BASE_COMMIT}`` inline, COPYs the
    scripts, warms the build cache (install.sh), then the verbatim
    ``Image._HARDENING_BLOCK`` strips origin/refs/reflog/future history (with the
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

    def dependency(self) -> Image | None:
        return CrushImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        # Build-time warm-up: download modules and warm the (cgo) build cache at
        # base.sha so the three eval runs start compiled. Runs BEFORE the
        # hardening strip; `|| true` keeps a flaky baseline from breaking build.
        install = """#!/bin/bash
set -e
git config --global --add safe.directory /home/__REPO__ || true
cd /home/__REPO__
go mod download || true
go build ./... >/dev/null 2>&1 || true
""".replace("__REPO__", repo)

        run_tests = """#!/bin/bash
set -uo pipefail
# Use embedded providers — never hit the network during tests (a catwalk
# provider auto-update in TestMain otherwise panics the whole package binary).
export CRUSH_DISABLE_PROVIDER_AUTO_UPDATE=1
cd /home/__REPO__
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### CRUSHPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        # base.sha is still checkout-able after the hardening strip because it is
        # HEAD (reachable, not pruned). Resetting + re-checking-out at the top of
        # each wrapper keeps the three eval runs independent and idempotent.
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

        # `git apply --3way` is all-or-nothing — if any single hunk fails the
        # 3way merge, none of the patch's NEW files get created either. Split
        # the patch by `diff --git` boundaries and apply each file's hunks
        # independently so unrelated files still land when an unrelated hunk
        # on a stale file (e.g. go.sum) can't merge.
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
""".replace("__REPO__", repo).replace("__SHA__", sha).replace("__APPLY_SPLIT__", apply_split)

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
""".replace("__REPO__", repo).replace("__SHA__", sha).replace("__APPLY_SPLIT__", apply_split)

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

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim — the clone + hardening below are kept as written
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

        # Anti-reward-hacking hardening — the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("charmbracelet", "crush")
class Crush(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CrushImageDefault(self.pr, self._config)

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
        #   --- PASS: TestParse (0.01s)
        #   --- FAIL: TestRender (0.02s)
        #   --- SKIP: TestRequiresAPIKey (0.00s)
        # Fenced by `### CRUSHPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### CRUSHPKG:\s+(\S+)\s+###")

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


# ---------------------------------------------------------------------------
# number_interval auto-population -- REGISTRY-SCOPED shim (no other file edited).
#
# This crush dataset is release-tag-range bundled: every instance's base.label is
# a "vLo..vHi" range and the fix_patch/test_patch are the split diff between the
# two release tags. The "PRs in the bundle" are exactly the PRs merged in that
# window. The output dataset's `number_interval` is written verbatim from the
# loaded PullRequest, but the bundle's PR list is dropped when the raw record is
# parsed, and the harness never derives it -- so number_interval ships empty.
#
# As this must live ONLY in the registry, we install two small, idempotent,
# charmbracelet/crush-scoped shims at import time (this file is the only one
# changed), mirroring the radareorg/radare2 precedent:
#
#   1. PullRequest.from_json -- for charmbracelet/crush records whose
#      number_interval is empty, fill it as "<p1>-<p2>-...-<pn>" (dash-joined,
#      the EXACT PRs in the bundle, ascending). Source order:
#        a. the raw line's `prs_in_bundle` list, if present (idiom-faithful), else
#        b. derived from base.label ("vLo..vHi") via `git log vLo..vHi` in a local
#           clone -- best-effort, fully guarded, leaves it empty if git/clone is
#           unavailable so generation never breaks.
#      That value then flows straight into the output dataset record.
#   2. Instance.create -- a non-empty number_interval makes routing look up
#      `charmbracelet/<that-list>`, which is not a registered key; fall back to
#      `charmbracelet/crush` so the build still routes. Other repos are
#      unaffected: shim 1 only fills charmbracelet/crush, and only EMPTY values.
# ---------------------------------------------------------------------------
import glob as _crush_glob
import json as _crush_json
import os as _crush_os
import subprocess as _crush_subprocess

from multi_swe_bench.harness.pull_request import PullRequest as _CrushPullRequest

_CRUSH_ORG = "charmbracelet"
_CRUSH_REPO = "crush"
_crush_repo_dir_cache: dict = {}
_crush_interval_cache: dict = {}


def _crush_join_prs(prs) -> str:
    """`[146, 150, 147]` -> `146-147-150` (dash-joined, ascending, unique)."""
    nums = sorted({int(p) for p in prs})
    return "-".join(str(n) for n in nums) if nums else ""


def _crush_find_repo_dir() -> str:
    """Locate a local charmbracelet/crush clone (best-effort, cached)."""
    if "dir" in _crush_repo_dir_cache:
        return _crush_repo_dir_cache["dir"]
    found = ""
    env = _crush_os.environ.get("CRUSH_REPO_DIR", "")
    candidates = [env] if env else []
    # Standard harness layout: <workdir>/data/<bundle>/repos/charmbracelet/crush.
    candidates += _crush_glob.glob(
        _crush_os.path.join(
            _crush_os.getcwd(), "**", "repos", _CRUSH_ORG, _CRUSH_REPO
        ),
        recursive=True,
    )
    for c in candidates:
        if c and _crush_os.path.isdir(_crush_os.path.join(c, ".git")):
            found = c
            break
    _crush_repo_dir_cache["dir"] = found
    return found


def _crush_prs_from_tag_range(label: str) -> str:
    """Derive `p1-p2-...` from a `vLo..vHi` release-tag range via git log."""
    if not label or ".." not in label:
        return ""
    if label in _crush_interval_cache:
        return _crush_interval_cache[label]
    interval = ""
    repo_dir = _crush_find_repo_dir()
    if repo_dir:
        try:
            lo, hi = label.split("..", 1)
            out = _crush_subprocess.run(
                ["git", "-C", repo_dir, "log", "--pretty=%s", f"{lo}..{hi}"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if out.returncode == 0:
                nums = re.findall(r"#(\d+)", out.stdout)
                interval = _crush_join_prs(nums)
        except Exception:
            interval = ""
    _crush_interval_cache[label] = interval
    return interval


if not getattr(_CrushPullRequest, "_crush_ni_shim", False):
    _crush_orig_from_json = _CrushPullRequest.from_json.__func__

    def _crush_from_json(cls, json_str):
        pr = _crush_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == _CRUSH_ORG
                and getattr(pr, "repo", "") == _CRUSH_REPO
                and not getattr(pr, "number_interval", "")
            ):
                raw = _crush_json.loads(json_str) or {}
                prs = raw.get("prs_in_bundle") or raw.get("pr_in_bundle") or []
                interval = (
                    _crush_join_prs(prs)
                    if prs
                    else _crush_prs_from_tag_range(getattr(pr.base, "label", ""))
                )
                if interval:
                    pr.number_interval = interval
        except Exception:
            pass
        return pr

    _CrushPullRequest.from_json = classmethod(_crush_from_json)
    _CrushPullRequest._crush_ni_shim = True


if not getattr(Instance, "_crush_route_shim", False):
    _crush_orig_create = Instance.create.__func__

    def _crush_create(cls, pr, config, *args, **kwargs):
        try:
            return _crush_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _CRUSH_ORG
                and getattr(pr, "repo", "") == _CRUSH_REPO
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_crush_create)
    Instance._crush_route_shim = True
