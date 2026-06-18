import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# telepresenceio/telepresence — a local-development tool for Kubernetes services.
#
# Discovery (dataset analysis):
#  - 72-PR Go range #1864..#4094, almost all on `release/v2` branch
#    (a few on release/v2.x tags). base.sha is the start of a version-range
#    window (e.g. v2.3.7..v2.4.0); each instance bundles 2..48 merged PRs.
#  - Multi-module repo: ./go.mod, ./rpc/go.mod, ./cmd/cobraparser/go.mod,
#    ./cmd/teleroute/go.mod. Per-package `go test` must run from the nearest
#    ancestor go.mod, so the runner walks up from each test pkg path.
#  - Go directive climbs across the range (early v2.3 ~go1.16, latest moved to
#    `go 1.26.0`), so GOTOOLCHAIN=auto is required to honour each base.sha's
#    own go.mod/toolchain directive.
#  - At least one file uses cgo (pkg/client/cli/env/syntax_test.go) — keep
#    CGO_ENABLED=1 + a C toolchain (build-essential + pkg-config).
#  - Test files in cmd/ (49 PRs), pkg/ (55 PRs), integration_test/ (62 PRs).
#    integration_test/ needs a real k8s cluster — those tests fail/skip without
#    one and aren't the recoverable signal. cmd/ and pkg/ unit tests are the
#    recoverable bulk (8 PRs touch ONLY integration_test/ → no unit signal).
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs from each pkg's nearest go.mod ancestor.
#    Runs are fenced with `### TLPKG ###` markers so test ids stay unique
#    across packages.
#
# Conformed to the hardened image.py contract:
#  - dependency() returns a STRING base image so the shared Image.dockerfile()
#    owns the build: it clones "${REPO_URL}", checks out "${BASE_COMMIT}", runs
#    extra_setup(), and appends the _HARDENING_BLOCK that strips every other
#    ref/commit so the fix can't be read out of git history. DockerfileEnhancer
#    then injects the proxy/cert infra + final sanitize pass. None of that fires
#    when dockerfile() is overridden, which is why the old two-stage build (its
#    own base-image class + custom dockerfile) bypassed the anti-cheat hardening.


# Era map: the go.mod `go` directive at each instance's base.sha, read by
# `git show <base.sha>:go.mod` across all 72 records. The toolchain climbs
# 1.15 -> 1.26 over the range. GOTOOLCHAIN=auto can only UPGRADE (and can only
# auto-download >= go1.21), so it cannot rescue old-era code from a too-new
# toolchain: e.g. at v2.3.7 the dep dlib@v1.2.4 re-declares
# `context.WithoutCancel`, which entered the stdlib in go1.21 -> build fails on
# any toolchain >= 1.21. So each instance must build on a base image matching
# its era's go minor. Keyed by PR number (the instance id) rather than a number
# interval because #2548 is a backport: low number, but release_line 2.19 / go
# 1.22 (its base.sha is on the v2.19 line), so number ranges would misroute it.
_GO_MINOR_BY_PR = {
    # go 1.15
    1864: "1.15", 1876: "1.15",
    # go 1.16
    1951: "1.16", 1998: "1.16", 2024: "1.16",
    # go 1.17
    2067: "1.17", 2079: "1.17", 2137: "1.17", 2141: "1.17", 2159: "1.17",
    2186: "1.17", 2370: "1.17", 2424: "1.17", 2432: "1.17", 2483: "1.17",
    2488: "1.17", 2531: "1.17", 2538: "1.17",
    # go 1.18
    2576: "1.18", 2577: "1.18", 2600: "1.18", 2633: "1.18", 2636: "1.18",
    2644: "1.18", 2648: "1.18", 2709: "1.18", 2711: "1.18", 2754: "1.18",
    2767: "1.18", 2806: "1.18", 2807: "1.18",
    # go 1.19
    2862: "1.19", 2877: "1.19", 2897: "1.19", 2912: "1.19", 2991: "1.19",
    3045: "1.19", 3070: "1.19", 3079: "1.19", 3129: "1.19", 3140: "1.19",
    3172: "1.19", 3225: "1.19", 3245: "1.19", 3312: "1.19", 3354: "1.19",
    3359: "1.19",
    # go 1.21 (toolchain go1.21.3)
    3385: "1.21", 3507: "1.21",
    # go 1.22 (toolchain go1.22.2) — note 2548 is a v2.19 backport
    2548: "1.22", 3626: "1.22",
    # go 1.23
    3694: "1.23", 3707: "1.23", 3746: "1.23", 3749: "1.23",
    # go 1.24
    3825: "1.24", 3837: "1.24", 3890: "1.24", 3896: "1.24", 3899: "1.24",
    3904: "1.24", 3909: "1.24", 3953: "1.24", 3961: "1.24",
    # go 1.25
    3991: "1.25", 3994: "1.25", 4016: "1.25",
    # go 1.26
    4038: "1.26", 4069: "1.26", 4083: "1.26", 4091: "1.26", 4094: "1.26",
}

# golang base images that ship on Debian buster/bullseye (<= go1.20). Their apt
# mirrors have moved to archive.debian.org, so the default apt-get update 404s.
_OLD_DEBIAN_GO = ("1.15", "1.16", "1.17", "1.18", "1.19", "1.20")


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


class TelepresenceImageDefault(Image):
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
        # Era-matched base image (see _GO_MINOR_BY_PR). For >= 1.21 eras the
        # go.mod also carries a `toolchain` directive; GOTOOLCHAIN=auto (set in
        # extra_setup) then upgrades to that exact patch (e.g. golang:1.21 ->
        # go1.21.3) on top of the matching minor. Unknown PRs fall back to a
        # recent toolchain. Returning a string hands the build to the shared
        # Image.dockerfile() (clone + checkout ${BASE_COMMIT} + _HARDENING_BLOCK).
        minor = _GO_MINOR_BY_PR.get(self.pr.number, "1.24")
        return f"golang:{minor}"

    def _get_apt_update_command(self, packages_str: str, base_img: str) -> str:
        # golang:1.15..1.20 are built on Debian buster/bullseye, whose apt repos
        # have moved to archive.debian.org. The base image name doesn't match
        # DEPRECATED_DEBIAN_IMAGES, so the default apt-get update would 404.
        # Force the archive-rewrite branch (hand the parent a buster tag), and
        # make the whole step non-fatal: the golang image already ships git,
        # gcc, curl and ca-certificates, so a failed extra-package install still
        # leaves a working build/test toolchain.
        dep = self.dependency()
        if any(dep.startswith(f"golang:{v}") for v in _OLD_DEBIAN_GO):
            cmd = super()._get_apt_update_command(packages_str, "debian:buster")
            return cmd + " || true"
        return super()._get_apt_update_command(packages_str, base_img)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        # build-essential is already in the default package set; pkg-config is
        # needed by the cgo test package (pkg/client/cli/env/syntax_test.go).
        return ["pkg-config"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Sets persistent Go env, stages the runtime helper scripts +
        # patches into /home/, and warms the module cache via prepare.sh. The
        # copied files live outside /home/{repo}, so the hardening pass (which
        # only operates inside the git tree) leaves them untouched.
        #   - GOTOOLCHAIN=auto: honour each base.sha's go/toolchain directive.
        #   - CGO_ENABLED=1: the syntax_test.go cgo package.
        #   - GOWORK=off: keep each sub-module's own go.mod authoritative
        #     (avoids -mod conflicts if a future release adds go.work).
        return (
            "ENV GOTOOLCHAIN=auto\n"
            "ENV CGO_ENABLED=1\n"
            "ENV GOWORK=off\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run_tests.sh /home/run_tests.sh\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

    def files(self) -> list[File]:
        repo = self.pr.repo
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        prepare = """#!/bin/bash
# Repo is already cloned + checked out at ${BASE_COMMIT} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# only warms the Go module/build caches so the eval runs don't need network.
set -e
cd /home/__REPO__
git reset --hard || true

export GOWORK=off
go mod download 2>&1 || true
go build ./... 2>&1 || true
""".replace("__REPO__", repo)

        # Multi-module aware runner: walk up from each test pkg dir to find its
        # nearest go.mod ancestor; run `go test` from there with the relative
        # path. GOWORK=off keeps each sub-module's own go.mod authoritative.
        # GOEXPERIMENT=jsonv2 is enabled per-modroot ONLY when the resolved
        # toolchain accepts it — newest PRs (go 1.26+) import encoding/json/v2,
        # but an older toolchain (pinned by a `toolchain` directive under
        # GOTOOLCHAIN=auto) would hard-error on an unknown experiment name.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__

export GOWORK=off
unset GOFLAGS 2>/dev/null || true
export GOTOOLCHAIN=auto
export CGO_ENABLED=1

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  # Find nearest ancestor with go.mod
  d="$pkg"
  while [ -n "$d" ] && [ "$d" != "." ] && [ ! -f "$d/go.mod" ]; do
    parent=$(dirname "$d")
    [ "$parent" = "$d" ] && break
    d="$parent"
  done
  if [ -f "$d/go.mod" ]; then
    modroot="$d"
  else
    modroot="."
  fi
  # Compute relative path inside the module's root.
  if [ "$modroot" = "$pkg" ]; then
    rel="."
  elif [ "$modroot" = "." ]; then
    rel="$pkg"
  else
    rel="${pkg#$modroot/}"
  fi
  echo "### TLPKG: $pkg ###"
  (
    cd "$modroot"
    EXP=""
    # `go env` validates GOEXPERIMENT, so this enables jsonv2 only on a
    # toolchain that actually recognises it (go1.25+) and silently skips it on
    # older eras (where the name is unknown and would hard-error every command).
    if GOEXPERIMENT=jsonv2 go env GOVERSION >/dev/null 2>&1; then
      EXP="jsonv2"
    fi
    GOEXPERIMENT="$EXP" go mod download 2>/dev/null || true
    GOEXPERIMENT="$EXP" go test -v -count=1 -vet=off -timeout=20m "./$rel/" 2>&1
  ) || true
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

        # Per-file split-apply: `git apply --3way` is all-or-nothing across the
        # whole patch — if any single hunk fails the 3way merge, none of the
        # patch's NEW files get created either. Split by `diff --git` boundaries
        # and apply each file's hunks independently. Bundle patches are large
        # (some fix.patch > 400 KB) so this materially improves apply coverage.
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
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


@Instance.register("telepresenceio", "telepresence")
class Telepresence(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TelepresenceImageDefault(self.pr, self._config)

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
        #   --- FAIL: TestConnect (0.02s)
        #   --- SKIP: TestIntegration (0.00s)
        # Fenced by `### TLPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### TLPKG:\s+(\S+)\s+###")

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
