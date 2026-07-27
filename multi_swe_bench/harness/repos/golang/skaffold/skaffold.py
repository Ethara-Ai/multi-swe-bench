import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for
# GoogleContainerTools/skaffold.
#
# Each instance is a release-delta BUNDLE. The raw record carries
# `prs_in_bundle` (e.g. [1060, 1338, 1340, 1342, ...]) but an EMPTY / null
# `number_interval`. The required output format is the dash-JOINED bundle list
# ("1060-1338-1340-1342-..."), NOT a "1060-1414" RANGE -- a range would wrongly
# imply every PR between 1060 and 1414 belongs to the bundle, which is untrue.
#
# Two constraints force the approach below:
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json schema
#     loader DROPS it -- the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "GoogleContainerTools/1060-1338-..."), which is
#     not registered -> instance creation fails.
#
# So, following the grafana/mimir + aquasecurity/tfsec convention, we do two
# import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to harness source):
#   1. PullRequest.from_json -- re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_skaffold_number_interval` (routing key stays "").
#   2. Dataset.build -- stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report builds every resolved-jsonl row via
#      Dataset.build(raw_dataset[id], report), so the output then carries it.
#
# Both wrappers chain safely with the identical grafana/mimir patches (each
# guards on its OWN flag, captures the current from_json/build as its `orig`,
# and only acts on its own org/repo) regardless of registry import order.
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_skaffold_number_interval_patched", False):
    _skaffold_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _skaffold_from_json(cls, json_str):
        pr = _skaffold_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "GoogleContainerTools"
                and raw.get("repo") == "skaffold"
                and raw.get("prs_in_bundle")
            ):
                # Stash only -- do NOT set pr.number_interval (the routing key).
                pr._skaffold_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_skaffold_from_json)
    _pull_request.PullRequest._skaffold_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_skaffold_build_patched", False):
        _skaffold_orig_build = _Dataset.build.__func__

        def _skaffold_build(cls, pr, report):
            ds = _skaffold_orig_build(cls, pr, report)
            ni = getattr(pr, "_skaffold_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_skaffold_build)
        _Dataset._skaffold_build_patched = True
# ---------------------------------------------------------------------------


# GoogleContainerTools/skaffold — Kubernetes-native CI/CD tool (Go).
#
# Discovery (dataset analysis):
#  - 77-PR Go range #388..#9778, mostly master/main with a few release
#    branches mixed in.
#  - Module path migrated:
#    * pre-modules (~<2200): github.com/GoogleContainerTools/skaffold
#      (committed vendor/, dep/Gopkg.toml era)
#    * modules era (~2200-7000): same path, go.mod present, vendor/ often
#      committed
#    * v2 era (~>=7300): github.com/GoogleContainerTools/skaffold/v2
#  - vendor/ is broadly present across eras — even modern PRs commit it,
#    so even old PRs can usually compile under GOPATH-mode fallback.
#  - Auto-detect at runtime: go.mod present → modules; else GOPATH at
#    github.com/GoogleContainerTools/skaffold + vendor/.


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


class SkaffoldImageBase(Image):
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

        org = self.pr.org
        repo = self.pr.repo

        # The leading `# syntax=docker/dockerfile:1.6` directive makes
        # DockerfileEnhancer.enhance() return this Dockerfile VERBATIM (it
        # early-returns when the directive is present). That deliberately
        # suppresses the enhancer's proxy / MITM / CA-cert injection AND its
        # `_standardize_repo_fetch` rewrite -- the latter would otherwise splice
        # a `git checkout ${{BASE_COMMIT}}` + history-strip block into this SHARED
        # base, whose build never receives a BASE_COMMIT, breaking the base build
        # outright. The `ca-certificates` apt package below is unrelated -- it is
        # the standard CA bundle for HTTPS `git clone` / `go mod download`.
        #
        # TOOLCHAIN-ONLY base (NO persistent clone), following the grafana/mimir
        # (cloudwego/eino) model: the repo clone + `${{BASE_COMMIT}}` checkout live
        # in the PER-PR image (SkaffoldImageDefault), so this ONE shared base is
        # reusable by every PR and each PR pins its own base commit. We still warm
        # the SHARED Go module cache (/go/pkg/mod) here from a THROWAWAY shallow
        # clone so common deps download once instead of for all 77 PRs -- then
        # remove it so no /home/{repo} with history is baked into the shared base
        # (the per-PR image clones fresh and strips history via the hardening
        # block, closing the git-history reward-hacking vector).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8 \\
    GOTOOLCHAIN=auto \\
    GOFLAGS=-mod=mod \\
    CGO_ENABLED=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN ( git clone --depth 1 "${{REPO_URL}}" /tmp/{repo}-warm \\
      && cd /tmp/{repo}-warm && go mod download ) || true; \\
    rm -rf /tmp/{repo}-warm

RUN git config --global --add safe.directory '*'

{self.clear_env}

CMD ["/bin/bash"]
"""


class SkaffoldImageDefault(Image):
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
        return SkaffoldImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        # The per-PR image clones + checks out ${BASE_COMMIT} INLINE in the
        # Dockerfile (grafana/mimir + netbird model, see dockerfile()) and then
        # strips git history via the canonical Image._HARDENING_BLOCK. install.sh
        # warms the go module + build cache at this SHA so the three eval runs
        # start compiled. Named + wired exactly like the netbird reference image
        # (ARG TARGETARCH/BUILDARCH passed through to this script).
        install = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__ || true

go mod download 2>/dev/null || true

# Compile-warm the build cache ONLY in the modules era AND ONLY on the native
# build arch. Under multi-arch buildx the non-native arch runs under QEMU
# (~10-20x slower) and its image is never graded on this host (the run phase
# uses the native-arch image), so warming it there is pure waste.
# TARGETARCH/BUILDARCH are buildx auto-args (empty under the classic single-arch
# builder -> that build IS native). Pre-modules/GOPATH source needs the runtime
# GOPATH symlink (set up in run_tests.sh), so the build-warm is skipped there;
# `go mod download` above is a harmless no-op in that era.
if [ -f go.mod ] && { [ -z "${TARGETARCH:-}" ] || [ "${TARGETARCH:-}" = "${BUILDARCH:-}" ]; }; then
  export GOFLAGS=-mod=mod
  go build ./... >/dev/null 2>&1 || true
fi
""".replace("__REPO__", repo)

        # Two eras: modules (go.mod present) and pre-modules (dep/glide era,
        # vendor/ committed, module path was
        # github.com/GoogleContainerTools/skaffold).
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
export GOWORK=off

if [ -f go.mod ]; then
  # Reconcile go.sum for this bundle's go.mod. The cumulative fix.patch bumps
  # go.mod to a newer dependency set; under the default read-only mode `go test`
  # aborts every affected package with "missing go.sum entry" (compile knockout,
  # e.g. pr-7056/6655/6133). Keeping the base image's -mod=mod (instead of the
  # old `unset GOFLAGS`) lets `go` add the missing sums as it compiles; the
  # explicit `download` primes go.sum for the direct requirements first.
  export GOFLAGS=-mod=mod
  go mod download 2>/dev/null || true
  go mod download all 2>/dev/null || true
  for pkg in __PKGS__; do
    [ -d "$pkg" ] || continue
    echo "### SKAFFPKG: $pkg ###"
    go test -v -count=1 -vet=off -timeout=20m "./$pkg/..." 2>&1 || true
  done
else
  export GOPATH=/go
  export GO111MODULE=off
  export PATH="$GOPATH/bin:$PATH"
  MODPATH="$GOPATH/src/github.com/GoogleContainerTools/skaffold"
  mkdir -p "$(dirname $MODPATH)"
  [ ! -e "$MODPATH" ] && ln -sf /home/__REPO__ "$MODPATH"
  for pkg in __PKGS__; do
    [ -d "$pkg" ] || continue
    echo "### SKAFFPKG: $pkg ###"
    (cd "$MODPATH/$pkg" && \\
      go test -v -count=1 -vet=off -timeout=20m ./... 2>&1) || true
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
        org = self.pr.org
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_files = " ".join(f.name for f in self.files())

        # Per-PR image (grafana/mimir + netbird / cloudwego/eino model): clone
        # FULL history, pin ${BASE_COMMIT} inline, COPY scripts, warm the build
        # cache (install.sh, arch-gated), then the CANONICAL Image._HARDENING_BLOCK
        # -- detach at
        # ${BASE_COMMIT}, remove origin, delete all refs, reflog-expire,
        # gc/repack, drop alternates, plus the HEAD==BASE_COMMIT / empty-refs /
        # rev-list asserts, then a recursive submodule strip. dependency() returns
        # an Image, so DockerfileEnhancer returns this Dockerfile VERBATIM -- the
        # per-PR clone/pin/harden below are kept as written (pinning here is
        # correct: it is per-PR, NOT the shared base). The hardening block is
        # concatenated RAW (not via an f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal. This closes the git-history reward-hacking vector:
        # after the strip, the fix commit and every ref/remote is unreachable.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

ARG TARGETARCH
ARG BUILDARCH
RUN TARGETARCH="${{TARGETARCH}}" BUILDARCH="${{BUILDARCH}}" bash /home/install.sh || true

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("GoogleContainerTools", "skaffold")
class Skaffold(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SkaffoldImageDefault(self.pr, self._config)

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

        # `go test -v` per-test result lines (possibly indented for subtests).
        # Fenced by `### SKAFFPKG: <pkg> ###` so ids stay unique across pkgs.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### SKAFFPKG:\s+(\S+)\s+###")

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
