"""goadesign/goa — SINGLE registered dispatcher for all three eras.

Routing: the raw dataset carries an EMPTY (null) `number_interval` and no `tag`
for every one of the 101 rows, so instance.py's `Instance.create` collapses all
of them to the key `goadesign/goa` (the `goadesign/goa_v1` and `goadesign/goa_v2`
keys are unreachable — dead — and are NOT registered anymore). This single `GOA`
Instance therefore dispatches to the correct era at runtime by `pr.base.ref`
("v1" / "v2" / "v3"). base.ref is used (not pr.number) because the v2 and v3
number ranges OVERLAP (v2 2121..2683, v3 2114..3902) while base.ref is clean.

Eras (Image + script families live in the sibling modules):
  * v3 (`base.ref == "v3"`, 82 rows) — GoaImageBase/GoaImageDefault (this file).
    Module `goa.design/goa/v3`, real go.mod (go1.12..go1.24). golang:1.24 +
    GOTOOLCHAIN=auto builds the whole range.
  * v2 (`base.ref == "v2"`, 14 rows) — goa_v2.GoaV2ImageDefault. Pre-modules
    (module `goa.design/goa`), synthesize + tidy a go.mod. Flaky sampler/header
    tests demoted to SKIP in parse_log (see `_FLAKY_V2_RE`).
  * v1 (`base.ref == "v1"`, 5 rows)  — goa_v1.GoaV1ImageDefault. Oldest, GOPATH
    ginkgo/gomega, synthesize go.mod with 2018-era dep-rot -replace pins.

protoc + protoc-gen-go + protoc-gen-go-grpc are installed in every base image:
goa's gRPC codegen tests shell out to `protoc` (e.g. grpc/codegen tests).

Hardening (synced to harness/image.py): the three shared base images each carry
the `# syntax=docker/dockerfile:1.6` directive, which makes
DockerfileEnhancer.enhance() emit them VERBATIM — the enhancer does NOT rewrite
the clone into a `git checkout ${BASE_COMMIT}` + history-strip block. That is
deliberate: each base (tags `base` / `base-v1` / `base-v2`) is shared as the
FROM parent of every PR image in its era, so it must stay commit-agnostic;
hardening a shared base to whichever PR built it first would prune away every
other PR's base commit. The git-history hardening is instead applied PER-PR by
`_harden_block()`, AFTER prepare.sh checks out that PR's base commit — see the
`GoaImageDefault.dockerfile()` below and the identical use in the v1/v2 modules.
"""

import json as _json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for goadesign/goa.
#
# Every instance is a release-BUNDLE. The raw record carries `prs_in_bundle`
# (e.g. [2246, 2261, 2265, 2278]) but an EMPTY/null `number_interval`. The
# required output format is the dash-JOINED bundle list ("2246-2261-2265-2278")
# — NOT a "2246-2278" range, which would wrongly imply every PR in between.
#
# Two constraints force the approach below (identical to aquasecurity/tfsec):
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json schema
#     loader DROPS it — the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "goadesign/2246-2261-2265-2278"), which is not
#     registered → instance creation fails / the row is silently skipped.
#
# So, following the tfsec convention, we do two import-time monkeypatches
# SCOPED TO THIS REGISTRY (no edits to harness source):
#   1. PullRequest.from_json — re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_goa_number_interval` (routing key stays "").
#   2. Dataset.build — stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report builds every resolved-jsonl row via
#      Dataset.build(raw_dataset[id], report), so the output then carries it.
# The patches chain safely with tfsec's (each captures the current from_json /
# build, calls through, and only acts on its own org/repo).
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_goa_number_interval_patched", False):
    _goa_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _goa_from_json(cls, json_str):
        pr = _goa_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "goadesign"
                and raw.get("repo") == "goa"
                and raw.get("prs_in_bundle")
            ):
                # Stash only — do NOT set pr.number_interval (the routing key).
                pr._goa_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_goa_from_json)
    _pull_request.PullRequest._goa_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_goa_build_patched", False):
        _goa_orig_build = _Dataset.build.__func__

        def _goa_build(cls, pr, report):
            ds = _goa_orig_build(cls, pr, report)
            ni = getattr(pr, "_goa_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_goa_build)
        _Dataset._goa_build_patched = True
# ---------------------------------------------------------------------------


# Environmentally non-deterministic v2 tests, demoted to SKIP so cross-stage
# comparison ignores them (see goa_v2.py history for the full rationale):
#   * TestAdaptiveSampler / TestFixedSampler — goa's traffic samplers are
#     probabilistic; sampler_test.go asserts tight bounds on a random count, so
#     the SAME test flips PASS/FAIL across run/test/fix independent of the patch.
#   * TestHeader — v2 codegen golden checks iterate Go maps, so header order is
#     nondeterministic and flips across stages independent of the patch.
# Left un-demoted for v1/v3 (their validated configs never demoted these).
_FLAKY_V2_RE = re.compile(r"^(TestAdaptiveSampler|TestFixedSampler|TestHeader)(/|$)")


def _harden_block(repo: str) -> str:
    """Git-history hardening for the per-PR (agent) image, applied AFTER
    prepare.sh has checked out this PR's base commit — so the commit to KEEP is
    the current HEAD (NOT a ${BASE_COMMIT} build-arg, which is not passed to
    FROM-an-image builds). The shared base deliberately keeps full history +
    `origin` (so each PR can `git checkout` its own base.sha); this strips the
    remote and every ref/commit not reachable from HEAD, so the evaluated agent
    cannot recover the fix from git log/show/history. Mirrors image.py's
    Image._HARDENING_BLOCK, but anchored on HEAD instead of ${BASE_COMMIT}.

    v1/v2 note: their prepare.sh commits a synthesized go.mod on top of base.sha,
    so at harden time HEAD is that synth commit and base.sha is its parent —
    BOTH are kept (reachable from HEAD); only the branch/tag/remote refs that
    point at the fix are deleted and pruned. run/test/fix.sh's
    `git reset --hard HEAD~1` then returns to base.sha, which survives the prune.
    v3's prepare.sh does not commit, so HEAD is base.sha directly.
    """
    return f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""


def parse_goa_log(test_log: str, flaky_re: "Optional[re.Pattern]" = None) -> TestResult:
    """Unified `go test -v` parser for all three goa eras. When `flaky_re` is
    provided (v2), matching test names are demoted to SKIP regardless of the
    reported outcome so nondeterministic tests can't corrupt cross-stage f2p."""
    passed_tests: set = set()
    failed_tests: set = set()
    skipped_tests: set = set()

    # `go test` without a TTY is normally clean, but strip ANSI just in case a
    # dependency's test reporter injects color codes.
    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    # Standard `go test -v` lines (subtests appear indented; .strip() handles it):
    #   --- PASS: TestName (0.00s)
    #   --- FAIL: TestName (0.00s)
    #   --- SKIP: TestName (0.00s)
    re_pass = re.compile(r"--- PASS: (\S+)")
    re_fail = re.compile(r"--- FAIL: (\S+)")
    re_skip = re.compile(r"--- SKIP: (\S+)")

    for line in test_log.splitlines():
        line = line.strip()

        m = re_pass.match(line)
        if m:
            name = m.group(1)
            if flaky_re and flaky_re.match(name):
                passed_tests.discard(name)
                failed_tests.discard(name)
                skipped_tests.add(name)
                continue
            if name in failed_tests:
                continue
            skipped_tests.discard(name)
            passed_tests.add(name)
            continue

        m = re_fail.match(line)
        if m:
            name = m.group(1)
            if flaky_re and flaky_re.match(name):
                passed_tests.discard(name)
                failed_tests.discard(name)
                skipped_tests.add(name)
                continue
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
            continue

        m = re_skip.match(line)
        if m:
            name = m.group(1)
            if flaky_re and flaky_re.match(name):
                skipped_tests.add(name)
                continue
            if name in passed_tests or name in failed_tests:
                continue
            skipped_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class GoaImageBase(Image):
    """v3 SHARED base (tag `base`), built ONCE and reused as the FROM parent of
    every v3 per-PR image. Clones the full repo history + keeps `origin` so each
    per-PR image can `git checkout` its own base commit. Carries NO per-PR base
    commit — see the module docstring and _harden_block() for why the git-history
    hardening is applied per-PR instead of here."""

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
        # Go 1.24 + GOTOOLCHAIN=auto: builds the whole v3 range (go1.12..go1.24)
        # and auto-fetches a newer toolchain if a commit's go.mod requires it.
        # golang:1.24-bookworm is a multi-arch manifest (linux/amd64 + linux/arm64).
        return "golang:1.24-bookworm"

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

        # ${REPO_URL} is passed as a build-arg by build_dataset (dep is a str →
        # base image). We declare it as an ARG below so the clone resolves.
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # The leading `# syntax=docker/dockerfile:1.6` directive makes
        # DockerfileEnhancer.enhance() return this Dockerfile VERBATIM
        # (early-return when the directive is already present). That is
        # deliberate: it stops the enhancer from rewriting the clone into a
        # `git checkout ${BASE_COMMIT}` + history-strip block, which would pin
        # this SHARED base to whichever PR built it first and prune away every
        # other PR's commit ("reference is not a tree"). The git-history
        # hardening is applied per-PR in GoaImageDefault (see _harden_block).
        # Bypassing the enhancer also drops its proxy/MITM/cert injection (unused
        # here; the `ca-certificates` apt package below is the standard CA bundle
        # for HTTPS `git clone` / `go mod download`, not injected proxy config).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
# Later v3 commits (≈v3.18+) introduce a `go.work` file. With go.work present,
# `go` refuses the `-mod=mod` flag from GOFLAGS ("-mod may only be set to
# readonly or vendor when in workspace mode"), so `go test ./...` aborts before
# producing any test output (run/test/fix all = 0,0,0). Disabling workspace
# mode keeps `-mod=mod` valid; modules still resolve via the repo's go.mod.
ENV GOWORK=off
ENV CI=true
# Go's signal-based async preemption (Go 1.14+) crashes under QEMU user-mode
# emulation (SIGSEGV/SIGILL with a register dump) when building linux/amd64 on
# an arm64 host. Disabling it makes cross-arch (QEMU) multiarch builds robust;
# native-arch builds are unaffected. Inherited by prepare/run/test/fix stages.
ENV GODEBUG=asyncpreemptoff=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \\
    git make ca-certificates protobuf-compiler && rm -rf /var/lib/apt/lists/*

# goa's gRPC codegen tests invoke `protoc` with these plugins on PATH.
RUN GOTOOLCHAIN=auto go install google.golang.org/protobuf/cmd/protoc-gen-go@latest \\
 && GOTOOLCHAIN=auto go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest \\
 || true
ENV PATH="/go/bin:${{PATH}}"

{code}

CMD ["/bin/bash"]
"""


class GoaImageDefault(Image):
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
        return GoaImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo} 2>/dev/null || true
git config user.email "msb@build" >/dev/null
git config user.name "msb-build" >/dev/null

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# v3 ships a root go.mod (module goa.design/goa/v3). Pre-warm the module and
# build caches so the scored runs don't pay download/compile latency.
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod
go mod download || true
go build ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true
go test -mod=mod -vet=off -short -timeout 900s -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true

git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn -3 /home/test.patch || true
go mod download || true
go test -mod=mod -vet=off -short -timeout 900s -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true

git apply --whitespace=nowarn /home/test.patch /home/fix.patch \\
  || ( git apply --whitespace=nowarn /home/fix.patch && git apply --whitespace=nowarn /home/test.patch ) \\
  || git apply --whitespace=nowarn -3 /home/test.patch /home/fix.patch || true
go mod download || true
go test -mod=mod -vet=off -short -timeout 900s -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Per-PR git-history hardening, applied AFTER prepare.sh checks out this
        # PR's base commit (the shared base keeps full history + origin).
        harden = _harden_block(self.pr.repo)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{harden}

{self.clear_env}
"""


@Instance.register("goadesign", "goa")
class GOA(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def _era(self) -> str:
        return (self.pr.base.ref or "").strip()

    def dependency(self) -> Optional[Image]:
        # Dispatch to the correct era's per-PR image by base.ref. Lazy imports
        # keep this module free of a top-level dependency on its siblings (the
        # v1/v2 modules import `_harden_block` from here, so a top-level import
        # back would be circular).
        era = self._era()
        if era == "v1":
            from multi_swe_bench.harness.repos.golang.goadesign.goa_v1 import (
                GoaV1ImageDefault,
            )

            return GoaV1ImageDefault(self.pr, self._config)
        if era == "v2":
            from multi_swe_bench.harness.repos.golang.goadesign.goa_v2 import (
                GoaV2ImageDefault,
            )

            return GoaV2ImageDefault(self.pr, self._config)
        return GoaImageDefault(self.pr, self._config)

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
        flaky_re = _FLAKY_V2_RE if self._era() == "v2" else None
        return parse_goa_log(test_log, flaky_re)
