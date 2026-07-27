from __future__ import annotations

import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for netbirdio/netbird.
#
# Each instance is a release-delta BUNDLE. The raw record carries
# `prs_in_bundle` (e.g. [1005, 1044, 1047, 1049, 1051, 1052, 1053]) but an EMPTY
# `number_interval`. The required output format is the dash-JOINED bundle list
# ("1005-1044-1047-1049-1051-1052-1053") -- NOT a "1005-1053" range, which would
# wrongly imply every PR in between is part of the bundle.
#
# Two constraints force the approach below:
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json schema
#     loader DROPS it -- the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "netbirdio/1005-1044-..."), which is not
#     registered -> instance creation fails.
#
# So, following the aquasecurity/tfsec convention, we do two import-time
# monkeypatches SCOPED TO THIS REGISTRY (no edits to harness source):
#   1. PullRequest.from_json -- re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_netbird_number_interval` (routing key stays "").
#   2. Dataset.build -- stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report.run_dataset builds every resolved-jsonl row
#      via Dataset.build(raw_dataset[id], report), so the output then carries it.
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_netbird_number_interval_patched", False):
    _netbird_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _netbird_from_json(cls, json_str):
        pr = _netbird_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "netbirdio"
                and raw.get("repo") == "netbird"
                and raw.get("prs_in_bundle")
            ):
                # Stash only -- do NOT set pr.number_interval (the routing key).
                pr._netbird_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_netbird_from_json)
    _pull_request.PullRequest._netbird_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_netbird_build_patched", False):
        _netbird_orig_build = _Dataset.build.__func__

        def _netbird_build(cls, pr, report):
            ds = _netbird_orig_build(cls, pr, report)
            ni = getattr(pr, "_netbird_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_netbird_build)
        _Dataset._netbird_build_patched = True
# ---------------------------------------------------------------------------


# ERA-SPLIT Go toolchain. netbird go.mod goes `go 1.16` (PR 152 / v0.2) ->
# `go 1.21` (PR ~1732) -> `go 1.23.0` (PR 2712 / v0.30) -> `go 1.25.5` (PR 5202+).
#
# Go 1.23 tightened `//go:linkname`: the linker now REJECTS a linkname to an
# undeclared runtime symbol. Old `golang.org/x/net/internal/socket` linknames
# `syscall.recvmsg`, so under go1.25 the TEST binaries of iface/peer/internal/cmd
# fail to link (`FAIL ... [build failed]: invalid reference to syscall.recvmsg`)
# for every pre-1.23 bundle -- which is why those instances came back unresolved.
# GOTOOLCHAIN=auto CANNOT fix this (it only upgrades), so old-era PRs need a
# pre-1.23 toolchain. go1.22 is the last strict-linkname-free release that still
# builds go1.16-1.22 modules; the go1.23+ bundles bumped x/net, so go1.25 is fine
# for them. Boundary = go.mod's `go 1.21 -> 1.23` jump at PR 2712.
#
# CGO + libpcap-dev: gopacket/pcap. X11/mesa/appindicator: client/ui (glfw) and
# client/cmd (systray) won't compile without them, knocking out those test pkgs.
def _netbird_era(number: int) -> str:
    return "old" if number < 2712 else "new"


_ERA_GO_IMAGE = {"old": "golang:1.22-bookworm", "new": "golang:1.25-bookworm"}
_ERA_GOTOOLCHAIN = {"old": "local", "new": "auto"}


# ---------------------------------------------------------------------------
# Build-context scripts (COPY'd into the image, run at build/eval time).
# ---------------------------------------------------------------------------

# Warms the go module + build cache at base.sha so the three eval runs start from
# a compiled state. Runs BEFORE the hardening strip, so it may still see the full
# clone; `|| true` keeps a flaky/partial baseline (e.g. the UI packages that need
# X11 headers) from breaking the build.
_INSTALL_SH = """#!/bin/bash
set -e

git config --global --add safe.directory /home/netbird || true
cd /home/netbird

go mod download || true

# Compile-warm the go build/test cache ONLY on the native build arch. Under
# multi-arch buildx the non-native arch runs under QEMU (~10-20x slower) and its
# image is never graded on this host (the run phase uses the native-arch image),
# so the expensive `go build`/`go test` warm there is pure waste -- skip it.
# TARGETARCH/BUILDARCH are buildx auto-args (passed in via the Dockerfile RUN);
# empty under the classic single-arch builder, in which case we always warm
# (that build IS native).
if [ -z "${TARGETARCH:-}" ] || [ "${TARGETARCH:-}" = "${BUILDARCH:-}" ]; then
    go build ./... >/dev/null 2>&1 || true
    go test -count=1 ./... >/dev/null 2>&1 || true
fi
"""

# Patch excludes shared by the three run scripts: binary/asset blobs a text
# `git apply` would choke on. Kept identical to netbird's scored dataset so the
# regraded results stay comparable.
_APPLY_EXCLUDES = (
    "--exclude='*.o' --exclude='*.png' --exclude='*.ico' --exclude='*.sqlite' "
    "--exclude='*.db' --exclude='*.mmdb' --exclude='*.gif' --exclude='*.ttf' "
    "--exclude='*.syso'"
)

# Baseline: clean base.sha, no patches. Full `./...` matches the golden p2p set.
# base.sha stays checkout-able after the hardening strip because it IS HEAD
# (reachable, not pruned).
_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/netbird
git reset --hard
git checkout {pr.base.sha}

go test -v -count=1 ./...
"""

# Test patch only: the new tests exercise behaviour the fix has not introduced
# yet, so they fail (or their package fails to compile) -- genuine f2p / n2p.
_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/netbird
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn {excludes} /home/test.patch

go test -v -count=1 ./...
"""

# Test + fix patches: production fix present, the suite passes.
_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/netbird
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn {excludes} /home/test.patch /home/fix.patch

go test -v -count=1 ./...
"""


class NetbirdImageBase(Image):
    """Level 1: toolchain-only base image (shared by all PRs).

    Does NOT clone the repo -- the clone lives in NetbirdImageDefault, done
    per-PR (mirrors the cloudwego/eino pattern). A shared string-dependency image
    that performs a `git clone` would be force-pinned to a single ${BASE_COMMIT}
    and history-stripped, breaking `git checkout` for every other PR sharing the
    base; keeping the clone per-PR avoids that.

    The leading `# syntax=docker/dockerfile:1.6` directive makes
    DockerfileEnhancer.enhance() return this Dockerfile VERBATIM (early-return),
    so the enhancer's infrastructure block is NOT injected. That is deliberate:
    netbird is kept proxy/cert-free at the REGISTRY level (no proxy build-args,
    no proxy/SSL ENVs, no CA-cert symlinks, no MITM secret mount). The plain
    `ca-certificates` apt package is unrelated -- it is the standard CA bundle
    required for HTTPS `git clone` / `go mod download`, not injected proxy config.
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
        return _ERA_GO_IMAGE[_netbird_era(self.pr.number)]

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_netbird_era(self.pr.number)}"

    def workdir(self) -> str:
        return f"base-{_netbird_era(self.pr.number)}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        gotoolchain = _ERA_GOTOOLCHAIN[_netbird_era(self.pr.number)]

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8 \\
    CGO_ENABLED=1 \\
    GOTOOLCHAIN={gotoolchain} \\
    GOFLAGS="-buildvcs=false -mod=mod"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

# libpcap-dev: gopacket/pcap (cgo). libx11-dev/libgl1-mesa-dev: client/ui glfw.
# libayatana-appindicator3-dev + libgtk-3-dev: client/cmd systray. Without these
# those test packages fail to compile and their tests never run (false unresolved).
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    build-essential \\
    pkg-config \\
    libpcap-dev \\
    libx11-dev \\
    libgl1-mesa-dev \\
    libgtk-3-dev \\
    libayatana-appindicator3-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

CMD ["/bin/bash"]
"""


class NetbirdImageDefault(Image):
    """Level 2: per-PR image (built on the shared toolchain base).

    dependency() returns NetbirdImageBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile VERBATIM -- no pin, no history
    strip injected by the pipeline. The clone therefore lives here, per-PR: the
    image clones full history, checks out ${BASE_COMMIT} inline, COPYs the
    scripts, warms the build cache (install.sh), then the canonical
    Image._HARDENING_BLOCK strips origin/refs/future history (with the four
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
        return NetbirdImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", _INSTALL_SH),
            File(".", "run.sh", _RUN_SH.format(pr=self.pr)),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH.format(pr=self.pr, excludes=_APPLY_EXCLUDES),
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH.format(pr=self.pr, excludes=_APPLY_EXCLUDES),
            ),
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

ARG TARGETARCH
ARG BUILDARCH
RUN TARGETARCH="${{TARGETARCH}}" BUILDARCH="${{BUILDARCH}}" bash /home/install.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts incl. HEAD==BASE_COMMIT,
        # then submodule strip). Concatenated raw (not via f-string) so its
        # ${BASE_COMMIT} / %(refname) tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("netbirdio", "netbird")
class Netbird(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NetbirdImageDefault(self.pr, self._config)

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

        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

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

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
