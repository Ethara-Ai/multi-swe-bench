from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FrpImageBase(Image):
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
        return "golang:latest"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Level 1: shared golang toolchain, built once and reused by every PR in
        # this era (image_tag() is the constant "base").
        #
        # This image must NOT fetch the repo. dependency() is a *string*, so
        # DockerfileEnhancer rewrites any `git clone .../home/frp` (or
        # `COPY frp /home/frp`) here into clone + `git checkout ${BASE_COMMIT}`
        # + Image._HARDENING_BLOCK. On a per-PR image that is correct; on a
        # SHARED image it is fatal: the base gets force-pinned to whichever PR
        # happened to build it first, and the hardening then deletes every ref,
        # removes the remote, expires the reflog and runs `git gc --prune=now`.
        # Every other PR's checkout of its own base sha then fails, with no
        # remote left to refetch from. So the clone lives per-PR in
        # FrpImageDefault (an Image dependency, left verbatim by the enhancer)
        # and this layer only provides the toolchain.
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this file verbatim (image.py: `if cls.SYNTAX_DIRECTIVE in raw`),
        # so _infrastructure_block never runs against it. That is deliberate:
        # it suppresses the proxy build args (http_proxy/https_proxy/no_proxy),
        # the proxy + SSL_CERT_FILE/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE entries of
        # the shared ENV block, the CA-certificate symlink farm, and the MITM
        # certificate secret mount. No proxy or certificate configuration is
        # injected into this image. This is the only image in this registry that
        # was receiving any of it -- FrpImageDefault has an Image dependency, so
        # the enhancer already returned it verbatim.
        #
        # Everything still required is declared inline below: the TARGETARCH /
        # REPO_URL / BASE_COMMIT args (build_dataset passes REPO_URL and
        # BASE_COMMIT as --build-arg for string-dependency images, so they are
        # declared here to be consumed rather than warned about), the non-proxy
        # ENV settings, and the OCI labels.
        #
        # `ca-certificates` stays in the apt install: that is the distro trust
        # store plain HTTPS needs for `git clone` and Go module downloads, not
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

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates python3 \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

"""


class FrpImageDefault(Image):
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
        return FrpImageBase(self.pr, self.config)

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
                "apply_patches.sh",
                r"""#!/bin/bash
# apply_patches.sh <patch>... -- apply patches to the CWD repo, strictly.
#
# Two dataset-side quirks make a bare `git apply` fail on otherwise good
# records, so each is handled explicitly rather than papered over with
# `|| true` (a partial apply would score tests against a tree that never
# received the patch):
#
#   1. Binary files. The patches were generated with plain `git diff` rather
#      than `git diff --binary`, so a binary change is recorded as a
#      contentless placeholder with an abbreviated index line:
#
#          diff --git a/assets/static/favicon.ico b/assets/static/favicon.ico
#          index 1234abc..5678def 100644
#          Binary files a/... and b/... differ
#
#      There is no blob data to apply, so git refuses the WHOLE patch with
#      "cannot apply binary patch ... without full index line". In this repo
#      those are dashboard assets (.ico/.ttf/.png) and doc screenshots, which
#      no Go test loads, so they are dropped and everything else applies.
#
#   2. CRLF / whitespace-only context drift: `--ignore-whitespace` matches
#      context ignoring line-ending differences. Note `--whitespace=nowarn`
#      does NOT help here -- it governs whitespace errors on *added* lines,
#      not context matching.
#
# Any other failure is a real failure and must abort.
set -eo pipefail

stripped=()
for p in "$@"; do
  s="${p%.patch}.stripped.patch"
  awk '
    /^diff --git /             { if (n && !skip) printf "%s", buf; buf=""; skip=0; n=1 }
    /^Binary files .* differ$/ { skip=1 }
                               { buf = buf $0 "\n" }
    END                        { if (n && !skip) printf "%s", buf }
  ' "$p" > "$s"

  dropped=$(grep -c '^Binary files .* differ$' "$p" || true)
  if [ "${dropped:-0}" -gt 0 ]; then
    echo "apply_patches: $p: dropped $dropped contentless binary section(s):" >&2
    grep '^Binary files .* differ$' "$p" | sed 's/^/apply_patches:   /' >&2
  fi
  stripped+=("$s")
done

git apply --ignore-whitespace "${stripped[@]}"
""",
            ),
            File(
                ".",
                "build_bin.sh",
                """#!/bin/bash
# Build the frps/frpc binaries into bin/.
#
# REQUIRED for correct scoring, not a convenience. frp's integration suites
# execute the built binaries rather than calling library code:
#
#   * tests/ci/*.go  (mid era) spawn ../../../bin/frps and fail with
#     "fork/exec ../../../bin/frps: no such file or directory" without them --
#     the panic aborts the whole package, so none of its tests report at all.
#   * test/e2e (modern era) defaults -frps-path to ../../bin/frps
#     (test/e2e/framework/test_context.go), so building into bin/ is enough to
#     make it run with no extra flags.
#
# 26 of the 64 records in this dataset carry test patches that touch exactly
# those suites, so without this step their new tests never execute and the run
# reports "no test cases transitioned from failed to passed" even when the fix
# is correct.
#
# Must be re-run after EVERY patch application: the binaries are compiled from
# the source the patches modify, so a stale binary would test the pre-patch code
# while the suite believes it is testing the patched tree.
#
# bin/ is listed in .gitignore in every era, so the artefacts never dirty
# `git status` and do not disturb the hardening block's assertions.
#
# Best-effort: a build failure is reported but does not abort, so that unit
# tests (which need no binary) still run and can still yield a transition.
set -uo pipefail

cd /home/{pr.repo}

# Build with the SAME module mode the tests use. Without -mod=vendor the build
# resolves modules from the network and dies on dependency rot -- e.g.
#   go: github.com/fatedier/kcp-go@v0.0.0-...: unknown revision cd167d2f15f4
# because that upstream revision no longer exists. The vendored copy in the
# repo still has it, which is why `go test -mod=vendor` succeeds against the
# very same tree. Mirroring the mode keeps build and test consistent and
# offline-capable.
MODFLAG=""
if [ -f /home/.gomod_mode ] && [ "$(cat /home/.gomod_mode)" = "vendor" ]; then
    MODFLAG="-mod=vendor"
elif [ -d vendor ]; then
    MODFLAG="-mod=vendor"
fi

for base in ./cmd ./src/cmd; do
    if [ -d "$base/frps" ]; then
        go build $MODFLAG -o bin/frps "$base/frps" \\
            || echo "build_bin: WARNING frps build failed" >&2
        go build $MODFLAG -o bin/frpc "$base/frpc" \\
            || echo "build_bin: WARNING frpc build failed" >&2
        break
    fi
done
exit 0

""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# The repo is already cloned and checked out at ${{BASE_COMMIT}} by the
# Dockerfile, so this script performs no git checkout of its own -- doing one
# here would fight the hardening pass that runs after it. It only detects the
# module layout and warms the build/module cache while the network is still
# available, so the eval runs are reproducible.
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

# Detect whether -mod=vendor works for this checkout
if go list -mod=vendor ./... > /dev/null 2>&1; then
    echo "vendor" > /home/.gomod_mode
else
    echo "normal" > /home/.gomod_mode
fi

# Build the binaries the integration suites exec (see build_bin.sh).
bash /home/build_bin.sh

# Warm-up test run
if [ "$(cat /home/.gomod_mode)" = "vendor" ]; then
    go test -v -count=1 -p=1 -vet=off -mod=vendor ./... || true
else
    go test -v -count=1 -p=1 -vet=off ./... || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/build_bin.sh
if [ "$(cat /home/.gomod_mode)" = "vendor" ]; then
    go test -v -count=1 -p=1 -vet=off -mod=vendor ./...
else
    go test -v -count=1 -p=1 -vet=off ./...
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
# Test-only stage: the test patch MUST apply cleanly.
#
# The previous `git apply || git apply --reject ... || true` + `rm *.rej` let a
# failed or partial apply fall through and run the PRE-patch suite anyway, so
# the harness scored tests that never existed and fail_to_pass was computed from
# a tree that never received the patch. Applying strictly means a broken record
# fails loudly instead of scoring a stale tree.
set -eo pipefail

cd /home/{pr.repo}
bash /home/apply_patches.sh /home/test.patch
bash /home/build_bin.sh
if [ "$(cat /home/.gomod_mode)" = "vendor" ]; then
    go test -v -count=1 -p=1 -vet=off -mod=vendor ./...
else
    go test -v -count=1 -p=1 -vet=off ./...
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
# Gold stage: test patch + fix patch applied together in one strict, atomic
# `git apply` so a half-applied tree can never be scored (see test-run.sh).
set -eo pipefail

cd /home/{pr.repo}
bash /home/apply_patches.sh /home/test.patch /home/fix.patch
bash /home/build_bin.sh
if [ "$(cat /home/.gomod_mode)" = "vendor" ]; then
    go test -v -count=1 -p=1 -vet=off -mod=vendor ./...
else
    go test -v -count=1 -p=1 -vet=off ./...
fi

""".format(pr=self.pr),
            ),
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
        # it, while the network and full git history are still available; the
        # Go build/module cache it warms lives outside the repo tree, and the
        # generated /home/.gomod_mode is untracked, so the history strip that
        # follows leaves both in place for the offline eval runs.
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


@Instance.register("fatedier", "frp_1107_to_99999")
class FRP_1107_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FrpImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval auto-population -- REGISTRY-SCOPED shim.
#
# Placed at the end of this era file (imported last by the package __init__.py)
# so __init__.py can stay imports-only. It works from here because it installs
# patched classmethods that resolve targets via `cls._registry[key]` at CALL
# time (Instance.create runtime), not at import time -- by then both era keys
# are registered, so it does not need to sit where both classes are visible.
#
# The output dataset jsonl's `number_interval` is written from the loaded
# PullRequest (Dataset.build -> number_interval=pr.number_interval), but the
# bundle's PR list (`prs_in_bundle`) is dropped when the raw record is parsed
# into a PullRequest, and the harness never derives it. The fatedier/frp dataset
# ships `prs_in_bundle` on every record but carries no `number_interval` field
# at all, so without this it would stay "" in the resolved jsonl.
#
# The interval is the EXACT PRs in the bundle joined with "-", NOT a first-last
# range: prs_in_bundle [146, 147, 150, 155, 157] -> "146-147-150-155-157".
# A "146-157" range would wrongly imply every PR in between is included; the frp
# bundles are sparse (median 7 PRs), so a range would over-claim by hundreds.
#
# Two idempotent, fatedier/frp-scoped shims are installed at import time:
#
#   1. PullRequest.from_json -- for fatedier/frp records whose number_interval
#      is empty, fill it from the raw line's prs_in_bundle.
#   2. Instance.create -- routing. There is NO plain `fatedier/frp` registration,
#      only the two era keys. So `fatedier/frp` fails whether number_interval is
#      empty (key becomes `fatedier/frp`) or filled with the PR list (key becomes
#      `fatedier/<dash-list>`). The fallback dispatches on PR NUMBER to the
#      correct era class. Other repos are unaffected: shim 1 only fills
#      fatedier/frp, and the fallback re-raises for any other org/repo.
# ---------------------------------------------------------------------------
import json as _frp_json  # noqa: E402

from multi_swe_bench.harness.instance import Instance as _FrpInstance  # noqa: E402
from multi_swe_bench.harness.pull_request import (  # noqa: E402
    PullRequest as _FrpPullRequest,
)

_FRP_ORG = "fatedier"
_FRP_REPO = "frp"

# Era boundary. The two registries are named frp_0_to_1060 and
# frp_1107_to_99999; the 1061-1106 span between them is unnamed and no record in
# this dataset falls in it, so it is routed to the modern class rather than left
# to raise.
_FRP_LEGACY_MAX_PR = 1060
_FRP_LEGACY_KEY = f"{_FRP_ORG}/frp_0_to_1060"
_FRP_MODERN_KEY = f"{_FRP_ORG}/frp_1107_to_99999"


def _frp_interval_from_raw(json_str: str) -> str:
    """Return the dash-joined prs_in_bundle for a raw record, or "" if absent.

    Bundle order is preserved as delivered (the dataset ships them ascending);
    values are emitted verbatim so the string round-trips the source list.
    """
    try:
        prs = (_frp_json.loads(json_str) or {}).get("prs_in_bundle") or []
    except Exception:
        return ""
    return "-".join(str(p) for p in prs)


if not getattr(_FrpPullRequest, "_frp_ni_shim", False):
    _frp_orig_from_json = _FrpPullRequest.from_json.__func__

    def _frp_from_json(cls, json_str):
        pr = _frp_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == _FRP_ORG
                and getattr(pr, "repo", "") == _FRP_REPO
                and not getattr(pr, "number_interval", "")
            ):
                interval = _frp_interval_from_raw(json_str)
                if interval:
                    pr.number_interval = interval
        except Exception:
            pass
        return pr

    _FrpPullRequest.from_json = classmethod(_frp_from_json)
    _FrpPullRequest._frp_ni_shim = True

if not getattr(_FrpInstance, "_frp_route_shim", False):
    _frp_orig_create = _FrpInstance.create.__func__

    def _frp_create(cls, pr, config, *args, **kwargs):
        try:
            return _frp_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _FRP_ORG
                and getattr(pr, "repo", "") == _FRP_REPO
            ):
                key = (
                    _FRP_LEGACY_KEY
                    if pr.number <= _FRP_LEGACY_MAX_PR
                    else _FRP_MODERN_KEY
                )
                if key in cls._registry:
                    return cls._registry[key](pr, config, *args, **kwargs)
            raise

    _FrpInstance.create = classmethod(_frp_create)
    _FrpInstance._frp_route_shim = True
