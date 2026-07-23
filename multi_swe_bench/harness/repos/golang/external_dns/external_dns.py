import json as _edns_json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# kubernetes-sigs/external-dns — DNS record management for K8s services (Go).
#
# Discovery (dataset analysis):
#  - 65-PR Go range #112..#5843, all on `master`.
#  - Spans three dep-management eras:
#    * 23 PRs <#800: glide + vendor/ committed (works in GOPATH mode)
#    * 9 PRs #800-#1280: dep (Gopkg.toml), no vendor — mostly unrecoverable
#    * 33 PRs >=#1280: modules era (go.mod), standard tooling
#  - Module path changed mid-era: github.com/kubernetes-incubator/external-dns
#    → sigs.k8s.io/external-dns (post-modules).
#  - Auto-detect mode at runtime: prefer go.mod, fall back to GOPATH +
#    vendor/ when go.mod is missing.


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


class ExternalDnsImageBase(Image):
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
        # Level 1: shared toolchain, built once and reused by every PR
        # (image_tag() is the constant "base").
        #
        # This image must NOT fetch the repo. dependency() is a *string*, so
        # DockerfileEnhancer rewrites any `git clone .../home/external-dns` (or
        # `COPY external-dns /home/external-dns`) here into clone +
        # `git checkout ${BASE_COMMIT}` + Image._HARDENING_BLOCK. On a per-PR
        # image that is correct; on a SHARED image it is fatal: the base gets
        # force-pinned to whichever PR happened to build it first, and the
        # hardening then deletes every ref, removes the remote, expires the
        # reflog and runs `git gc --prune=now`. Every other PR's checkout of its
        # own base sha then fails, with no remote left to refetch from. So the
        # clone lives per-PR in ExternalDnsImageDefault (an Image dependency,
        # left verbatim by the enhancer) and this layer only provides the
        # toolchain.
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this file verbatim (image.py: `if cls.SYNTAX_DIRECTIVE in raw`),
        # so _infrastructure_block never runs against it. That is deliberate:
        # it suppresses the proxy build args (http_proxy/https_proxy/no_proxy),
        # the proxy + SSL_CERT_FILE/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE entries of
        # the shared ENV block, the CA-certificate symlink farm, and the MITM
        # certificate secret mount. No proxy or certificate configuration is
        # injected into this image. This is the only image in this registry that
        # was receiving any of it -- ExternalDnsImageDefault has an Image
        # dependency, so the enhancer already returned it verbatim.
        #
        # Everything still required is declared inline below: the TARGETARCH /
        # REPO_URL / BASE_COMMIT args (build_dataset passes REPO_URL and
        # BASE_COMMIT as --build-arg for string-dependency images, so they are
        # declared here to be consumed rather than warned about), the non-proxy
        # ENV settings, and the OCI labels.
        #
        # `ca-certificates` stays in the apt install: that is the distro trust
        # store plain HTTPS needs for `git clone` and `go mod download`, not
        # proxy/MITM interception config. Removing the symlink farm does not
        # remove the need for it.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV TZ=UTC
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
ENV CGO_ENABLED=1
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

"""


class ExternalDnsImageDefault(Image):
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
        return ExternalDnsImageBase(self.pr, self._config)

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

        # The repo is already cloned and checked out at ${BASE_COMMIT} by the
        # Dockerfile, so this script performs no git checkout of its own --
        # doing one here would fight the hardening pass that runs after it, and
        # the previous hardcoded `git checkout <sha>` also bypassed the
        # BASE_COMMIT build arg entirely. It now only warms the module cache
        # while the network is still available, so the eval runs are
        # reproducible.
        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh

go mod download 2>/dev/null || true
""".replace("__REPO__", repo)

        # Two eras: modules (go.mod present, current module path is
        # sigs.k8s.io/external-dns) and pre-modules (glide/dep era, module
        # path was github.com/kubernetes-incubator/external-dns and vendor/
        # was sometimes committed). Detect and switch modes.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
export GOWORK=off

if [ -f go.mod ]; then
  unset GOFLAGS
  # go.sum is a GENERATED lockfile, not source. Several fix patches change
  # dependencies via go.mod but their go.sum hunk does not apply cleanly against
  # this tree (split_apply drops it -- see apply_patches excludes for go.sum),
  # which would otherwise leave a go.mod/go.sum pair that `go test` rejects with
  # "missing go.sum entry". Regenerate it from the (patched) go.mod so the module
  # graph is consistent before testing. `go mod tidy` also backfills any require
  # lines the patch added to imports but not to go.mod.
  go mod download 2>/dev/null || true
  go mod tidy 2>/dev/null || true
  go mod download 2>/dev/null || true
  for pkg in __PKGS__; do
    [ -d "$pkg" ] || continue
    echo "### EDNSPKG: $pkg ###"
    go test -v -count=1 -vet=off -timeout=20m "./$pkg/..." 2>&1 || true
  done
else
  # GOPATH layout: place under $GOPATH/src/github.com/kubernetes-incubator/external-dns
  # (the pre-modules import path). Use vendor/ if present, otherwise tests
  # likely won't compile and will be marked failed.
  export GOPATH=/go
  export GO111MODULE=off
  export PATH="$GOPATH/bin:$PATH"
  MODPATH="$GOPATH/src/github.com/kubernetes-incubator/external-dns"
  mkdir -p "$(dirname $MODPATH)"
  [ ! -e "$MODPATH" ] && ln -sf /home/__REPO__ "$MODPATH"
  for pkg in __PKGS__; do
    [ -d "$pkg" ] || continue
    echo "### EDNSPKG: $pkg ###"
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
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin "
            # go.sum: a generated lockfile. Its hunks routinely fail to apply on
            # top of a differently-resolved tree; dropping the hunk and letting
            # run_tests.sh regenerate go.sum (go mod tidy) from the patched
            # go.mod keeps the module graph consistent without a spurious
            # split_apply failure.
            "--exclude=go.sum "
            # Vendored third-party test FIXTURES (e.g.
            # vendor/github.com/prometheus/procfs/fixtures/...): data files inside
            # a dependency, never external-dns's own code or tests. A patch that
            # touches them should not gate the record -- same rationale as the
            # binary excludes above.
            "--exclude=vendor/**/fixtures/** --exclude=vendor/**/testdata/**"
        )

        # split_apply: apply a patch file-section by file-section, then FAIL if
        # any section did not apply.
        #
        # Splitting is deliberate and stays: the excludes below drop binary
        # assets (.png/.ico/.gz/...) whose sections are contentless placeholders
        # -- the patches were generated with plain `git diff` rather than
        # `git diff --binary`, so git refuses the whole patch with "cannot apply
        # binary patch ... without full index line". Per-section application
        # lets the real code hunks land while those are skipped.
        #
        # What changed: the previous version COUNTED failures and returned
        # success regardless, so a patch that failed entirely still fell through
        # to run_tests.sh. The harness then scored the PRE-patch tree while
        # believing it had the patched one -- fail_to_pass computed from a tree
        # that never received the patch. It now returns non-zero on any failed
        # section, and callers run under `set -eo pipefail`, so a broken record
        # aborts loudly instead of being silently mis-scored.
        apply_split = """split_apply() {
  local pf="$1"
  local td
  td=$(mktemp -d)
  awk 'BEGIN{i=0} /^diff --git /{i++; f=sprintf("%s/p%04d.patch","'"$td"'",i)} f{print > f}' "$pf"
  local applied=0 failed=0
  local failed_files=""
  for f in "$td"/p*.patch; do
    [ -s "$f" ] || continue
    if git apply --3way --whitespace=nowarn __EXCLUDES__ "$f" >/dev/null 2>&1; then
      applied=$((applied+1))
    elif git apply --whitespace=nowarn __EXCLUDES__ "$f" >/dev/null 2>&1; then
      applied=$((applied+1))
    else
      failed=$((failed+1))
      failed_files="$failed_files $(sed -n 's|^diff --git a/\\([^ ]*\\).*|\\1|p' "$f" | head -1)"
    fi
  done
  rm -rf "$td"
  echo "split_apply $pf: applied=$applied failed=$failed"
  if [ "$failed" -gt 0 ]; then
    echo "split_apply: ERROR $failed section(s) did not apply:$failed_files" >&2
    return 1
  fi
  return 0
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
        # Level 2: per-PR image FROM the shared toolchain base. dependency() is
        # an *Image*, so DockerfileEnhancer.enhance() returns this verbatim --
        # nothing is injected and nothing is rewritten, so the clone, the
        # ${BASE_COMMIT} checkout and Image._HARDENING_BLOCK below are kept
        # exactly as written. Pinning BASE_COMMIT here is correct precisely
        # because this image is per-PR, unlike the shared base.
        #
        # build_dataset only passes --build-arg REPO_URL/BASE_COMMIT for
        # *string*-dependency images, so BASE_COMMIT is defaulted to this PR's
        # sha here rather than relying on the build arg.
        #
        # _HARDENING_BLOCK is concatenated raw (not through the f-string) so its
        # ${BASE_COMMIT} / %(refname) tokens stay literal. prepare.sh runs before
        # it, while the network and full git history are still available; the Go
        # module cache it warms lives outside the repo tree, so the history strip
        # that follows leaves it in place for the offline eval runs.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("kubernetes-sigs", "external-dns")
class ExternalDns(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ExternalDnsImageDefault(self.pr, self._config)

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
        #   --- PASS: TestController (0.01s)
        #   --- FAIL: TestProvider (0.02s)
        #   --- SKIP: TestIntegration (0.00s)
        # Fenced by `### EDNSPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### EDNSPKG:\s+(\S+)\s+###")

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
# The output dataset jsonl's `number_interval` is written from the loaded
# PullRequest (Dataset.build -> number_interval=pr.number_interval), but the
# bundle's PR list (`prs_in_bundle`) is dropped when the raw record is parsed
# into a PullRequest, and the harness never derives it. The external-dns dataset
# ships `prs_in_bundle` on every record but carries no `number_interval` field
# at all, so without this it would stay "" in the resolved jsonl.
#
# The interval is the EXACT PRs in the bundle joined with "-", NOT a first-last
# range: prs_in_bundle [146, 147, 150, 155, 157] -> "146-147-150-155-157".
# A "146-157" range would wrongly imply every PR in between is included. These
# bundles are extremely sparse -- e.g. pr-1209 bundles 36 PRs spanning 1209 to
# beyond 3300 -- so a range would over-claim by thousands of PRs.
#
# Two idempotent, kubernetes-sigs/external-dns-scoped shims are installed at
# import time (this file is the only one changed):
#
#   1. PullRequest.from_json -- for external-dns records whose number_interval
#      is empty, fill it from the raw line's prs_in_bundle. That value then
#      flows straight into the output dataset record.
#   2. Instance.create -- a non-empty number_interval makes routing look up
#      `kubernetes-sigs/<that-list>`, which is not a registered key; fall back
#      to `kubernetes-sigs/external-dns` so the build still routes. Other repos
#      are unaffected: shim 1 only fills external-dns, and era-keyed datasets
#      keep their pre-set number_interval (only EMPTY values are filled) whose
#      `org/<era>` key is registered, so the fallback never triggers for them.
#      Shims installed by other registries chain safely: each re-raises when the
#      org/repo is not its own.
# ---------------------------------------------------------------------------

_EDNS_ORG = "kubernetes-sigs"
_EDNS_REPO = "external-dns"


def _edns_interval_from_raw(json_str: str) -> str:
    """Return the dash-joined prs_in_bundle for a raw record, or "" if absent.

    Bundle order is preserved as delivered (the dataset ships them ascending);
    values are emitted verbatim so the string round-trips the source list.
    """
    try:
        prs = (_edns_json.loads(json_str) or {}).get("prs_in_bundle") or []
    except Exception:
        return ""
    return "-".join(str(p) for p in prs)


if not getattr(PullRequest, "_edns_ni_shim", False):
    _edns_orig_from_json = PullRequest.from_json.__func__

    def _edns_from_json(cls, json_str):
        pr = _edns_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == _EDNS_ORG
                and getattr(pr, "repo", "") == _EDNS_REPO
                and not getattr(pr, "number_interval", "")
            ):
                interval = _edns_interval_from_raw(json_str)
                if interval:
                    pr.number_interval = interval
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_edns_from_json)
    PullRequest._edns_ni_shim = True

if not getattr(Instance, "_edns_route_shim", False):
    _edns_orig_create = Instance.create.__func__

    def _edns_create(cls, pr, config, *args, **kwargs):
        try:
            return _edns_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _EDNS_ORG
                and getattr(pr, "repo", "") == _EDNS_REPO
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_edns_create)
    Instance._edns_route_shim = True
