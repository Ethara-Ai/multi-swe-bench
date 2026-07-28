"""v2ray-core harness for the pre-go-modules era (PRs 0-1499).

These bases predate go.mod entirely, so the module manifest is supplied by the
registry (see ``pinned_go.mod``) rather than taken from the tree.

Test command: go test -v -count=1 -timeout 15m -skip <hanging> -vet=off -tags json ./...
"""

import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# === number_interval: dash-joined prs_in_bundle ===
#
# FORMAT: the explicit dash-joined member list, NEVER a range. The bundles here
# are sparse ([758, 786, 861, 863] must serialise as "758-786-861-863"), so a
# range form would be wrong for most records.
#
# WHY A PATCH IS NEEDED: the raw jsonl carries `prs_in_bundle` but NO
# `number_interval` and NO `tag`, and PullRequest.from_json goes through
# dataclass_json, which DROPS unknown keys. With both fields empty,
# Instance.create builds the key f"{org}/{repo}" == "v2ray/v2ray-core", which is
# NOT registered (only the two era classes are), so every one of the 22 records
# raises "Instance 'v2ray/v2ray-core' is not registered" before a single image
# is built. Deriving the value at load time is the only place the bundle is
# still visible.
#
# Like restic/restic and unlike MHSanaei/3x-ui, v2ray-core DOES set
# pr.number_interval, because for this repo it is also the era routing key:
# instance.py routes on f"{org}/{number_interval}", and the two era classes
# (this file and v2ray_core_1500_to_99999.py) split on whether the base commit
# predates go modules. It is only set when the resulting key is actually
# registered (every bundle in this dataset is -- see the tables at the bottom of
# this file and of the 1500+ file); an unregistered bundle falls back to "" so
# Instance.create still resolves rather than raising, and the Dataset.build
# patch below still stamps the value onto the output row.
#
# Two import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to harness
# source), following the restic, aquasecurity/tfsec and MHSanaei/3x-ui
# convention. Installed here rather than in the 1500+ file because __init__.py
# imports this module first and the org filter covers both eras.
import multi_swe_bench.harness.pull_request as _pull_request  # noqa: E402


def _v2ray_number_interval(raw: dict) -> str:
    """Dash-joined explicit member list of prs_in_bundle ("" if unavailable)."""
    bundle = raw.get("prs_in_bundle")
    if not bundle:
        return ""
    return "-".join(str(p) for p in bundle)


if not getattr(_pull_request.PullRequest, "_v2ray_number_interval_patched", False):
    _v2ray_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _v2ray_from_json(cls, json_str):
        pr = _v2ray_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if raw.get("org") == "v2ray" and raw.get("repo") == "v2ray-core":
                ni = _v2ray_number_interval(raw)
                if ni:
                    # Stash unconditionally so the output row can be stamped
                    # even when the bundle is not a registered routing key.
                    pr._v2ray_ni = ni
                    if f"v2ray/{ni}" in Instance._registry:
                        pr.number_interval = ni
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_v2ray_from_json)
    _pull_request.PullRequest._v2ray_number_interval_patched = True

    # Stamp number_interval onto the resolved-jsonl row. Redundant when the
    # routing key was set above (Dataset.build already copies it), and the
    # actual fix for the unregistered-bundle fallback.
    #
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_v2ray_build_patched", False):
        _v2ray_orig_build = _Dataset.build.__func__

        def _v2ray_build(cls, pr, report):
            ds = _v2ray_orig_build(cls, pr, report)
            if not ds.number_interval:
                ds.number_interval = getattr(pr, "_v2ray_ni", "")
            return ds

        _Dataset.build = classmethod(_v2ray_build)
        _Dataset._v2ray_build_patched = True
# ---------------------------------------------------------------------------


# === phantom-N2P correction (SCOPED TO v2ray/v2ray-core) ===
#
# THE BUG. report.py buckets a test as N2P when run == NONE, test == NONE and
# fix == PASS, reading "no result at the test stage" as "the test did not exist
# yet". For Go that inference is unsound, and measurably so: on the 18 gradeable
# instances of this dataset the run produced 96 N2P against only 11 F2P, and
# EVERY ONE of the 96 traced to its package failing at the test stage (76
# `[build failed]`, 20 `[setup failed]`) rather than to a newly added test.
#
# WHY IT IS UNSOUND. `go test` reports per-test lines only for packages that
# COMPILE. If a package fails to build, it prints one `FAIL <pkg> [build failed]`
# and no `--- PASS/FAIL` lines at all, so every test in it -- new or decades old
# -- parses as NONE. The common shape here is a gold test patch that exercises an
# API the gold FIX patch introduces:
#
#     pr-651  common/buf/writer_test.go:54:  undefined: DiscardBytes
#     pr-2091 infra/conf/v2ray_test.go:443:  tt.orig.Override undefined
#     pr-2002 app/dns/dnscommon_test.go:59:  too many values in struct literal
#     pr-1019 testing/scenarios/tls_test.go: no required module provides package
#                                            v2ray.com/core/common/protocol/tls/cert
#
# In each case the test literally cannot compile until the fix lands, so it can
# never emit `--- FAIL` at the test stage, and it silently lands in N2P instead
# of F2P.
#
# THE RULE. A test function that the gold test patch ADDS (`+func TestX`) is
# present in the tree at the test stage by construction. If it still produced no
# result there, it was SUPPRESSED -- in Go there is no other way for a present
# test to report nothing. Suppressed is not passing, so `fix == PASS` makes it a
# genuine fail-to-pass. Move it to F2P.
#
# DELIBERATELY CONSERVATIVE, in two ways:
#
#  1. Only tests the test patch demonstrably adds are moved. PRE-EXISTING tests
#     caught in the same broken package (28 of the 96 -- 12 in pr-2002 behind a
#     Go-1.22 `misplaced +build comment` in 2020-era code, 16 in pr-1019 behind a
#     timed-out testing/scenarios package) are NOT touched. Their baseline status
#     is genuinely unknown, and asserting a run-stage PASS would be fabricating
#     evidence. The timeout 16 are addressed at the source instead, by the
#     HANGING_TESTS skip above, which lets the package finish so those tests
#     report normally at the run stage and classify as P2P on their own.
#     The pr-2002 12 remain a documented limitation: the era's code is not
#     buildable under the Go version its go.mod requires.
#
#  2. It runs AFTER the original check(), so it cannot change any validity
#     verdict -- `valid` is decided in steps 1-5 and bucketing is step 6. In
#     particular the anomalous-pattern guard still fires for pr-758
#     (`TestChinaSites`: run=PASS, test=NONE, fix=FAIL), which is a real
#     fix-stage regression that must stay unresolved rather than be masked.
#     Reclassifying N2P -> F2P never flips an instance from unresolved to
#     resolved; N2P and F2P both already satisfy the "fix something" test.
#
# Same scoped-monkeypatch convention as the number_interval patch above.
_V2RAY_ADDED_TEST_RE = re.compile(r"^\+func (Test\w+)", re.M)


def _v2ray_test_funcs_added_by(test_patch: str) -> set[str]:
    """Test functions the gold test patch introduces (`+func TestX`)."""
    if not test_patch:
        return set()
    text = test_patch.replace("\r\n", "\n").replace("\r", "\n")
    return set(_V2RAY_ADDED_TEST_RE.findall(text))


from multi_swe_bench.harness.report import Report as _Report  # noqa: E402

if not _Report.__dict__.get("_v2ray_n2p_patched", False):
    _v2ray_orig_check = _Report.check

    def _v2ray_check(self, force: bool = False):
        valid, msg = _v2ray_orig_check(self, force)
        if getattr(self, "org", "") != "v2ray" or getattr(self, "repo", "") != "v2ray-core":
            return valid, msg
        if not self.n2p_tests:
            return valid, msg
        added = _v2ray_test_funcs_added_by(getattr(self, "test_patch", "") or "")
        if not added:
            return valid, msg
        # Subtests report as "TestRoot/case"; the root is what the patch declares.
        for name in list(self.n2p_tests):
            if name.split("/")[0] in added:
                self.f2p_tests[name] = self.n2p_tests.pop(name)
        return valid, msg

    _Report.check = _v2ray_check
    _Report._v2ray_n2p_patched = True
# ---------------------------------------------------------------------------


# Paths excluded from every `git apply`. This is what makes STRICT patch
# application possible, and the same set is used for both eras and all three
# graded stages so no stage-dependent or era-dependent flag can skew the reward
# buckets.
#
# 1. BINARY BLOBS (`*.dat`, `*.exe`, `.dev/*`). The delivered jsonl stores
#    patches as JSON strings, which mangles the binary hunks for the geo
#    databases (release/config/geoip.dat, geosite.dat and the older
#    tools/release/config/ copies) and for the vendored protoc toolchain
#    (.dev/protoc/{linux,macos,windows}/protoc[.exe] -- the macOS/Linux ones
#    carry no extension, hence the directory glob). Measured across all 22
#    records: with no exclusion, 9 of 22 fix patches fail to apply.
#    Excluding them costs nothing: the geo .dat files are supplied by the base
#    image instead (see GEO_ASSET_DIR below), which is where the tests read them
#    from anyway, and .dev/protoc/* is developer tooling for regenerating .pb.go
#    files that `go test` never touches.
#
# 2. `vendor/*`. Required by THIS era: prepare.sh deletes the vendor/ tree
#    wholesale (no 0_to_1499 base ships a complete one, so vendor mode is
#    unusable) and resolves dependencies through the pinned go.mod instead. Two
#    legacy fix patches -- pr-1301 (vendor/h12.me/socks) and pr-1019
#    (vendor/github.com/shadowsocks/go-shadowsocks2) -- delete a vendored
#    gitlink, and a strict apply of that hunk fails with "No such file or
#    directory" once prepare.sh has removed the path. Both are pure
#    vendored-submodule removals with no effect on the pinned build.
#    NOTE these are DELETIONS (`deleted file mode 160000`, `+++ /dev/null`), so
#    they are invisible to any patch scan that only reads `+++ b/` lines.
#    Verified a no-op for the 1500+ era: no module-era patch touches vendor/.
#
# With all four globs excluded, all 22 test patches AND all 22 fix patches apply
# strictly and cleanly, in the post-prepare.sh tree state each stage actually
# sees.
#
# This is the anti-reward-hacking half of the change. The previous scripts ran
#     git apply X || git apply --reject --allow-empty X || true
#     find . -name '*.rej' -delete
# which turned "the patch did not apply" into a SILENT no-op: the fix stage
# would run the unfixed tree, the .rej evidence was deleted, and the stage was
# graded on whatever the old tests happened to report. A model could be credited
# for a fix that was never applied. Application is now strict under `set -e`, so
# a patch that does not apply fails the stage loudly.
PATCH_EXCLUDES = (
    "--exclude='*.dat' --exclude='*.exe' --exclude='.dev/*' --exclude='vendor/*'"
)

# Integration tests in testing/scenarios that DEADLOCK in this container, skipped
# identically at all three graded stages so the reward buckets stay symmetric.
#
# These do not merely run slowly -- they hang forever. The goroutine dump at the
# timeout panic shows the test binary parked on
#
#     goroutine 1 [chan receive, 28 minutes]:
#     sync.(*WaitGroup).Wait(...)
#
# i.e. main is waiting on workers that never finish. They spin up real proxy
# servers and clients over loopback, and in this sandbox one side never comes up.
#
# MEASURED, not assumed: a first attempt simply raised the limit from Go's
# default 10m to 30m. Every affected instance still timed out, just later --
# `FAIL v2ray.com/core/testing/scenarios 1980.0s` with
# `panic: test timed out after 30m0s` on all 7 of them. The only effect was
# tripling the wall clock of the run stage, which is why that pipeline had to be
# abandoned mid-flight. Raising the ceiling cannot fix a deadlock; the hanging
# tests have to be excluded.
#
# COST OF NOT SKIPPING: the panic aborts the WHOLE package, so every other test
# in testing/scenarios reports nothing. That is what put 16 pre-existing pr-1019
# tests into the phantom-N2P bucket -- they were never observable at the run
# stage. Skipping the deadlockers lets the package finish (~123s, as it already
# does at the fix stage) so those tests report normally and classify as P2P.
#
# Applied to run/test/fix alike: a skipped test is absent from all three stages,
# so it is simply not graded, rather than shifting between buckets.
HANGING_TESTS = "TestShadowsocksChacha|TestCommanderAddRemoveUser"

# `-skip` requires Go >= 1.21; both eras pin 1.21 (legacy) / 1.22 (module).
GO_TEST_SKIP = f"-skip '{HANGING_TESTS}'"

# Geo databases are staged into the BASE image at build time and copied in at
# test time. They used to be curl'd inside run.sh / test-run.sh / fix-run.sh,
# i.e. during the graded stages, which made grading depend on the network and
# gave graded containers a live path to github.com. Staging them in the base
# keeps the "a patch that adds the file wins" behaviour (the copy is skipped
# when the file already exists) while making all three graded stages hermetic.
GEO_ASSET_DIR = "/home/geoassets"

STAGE_GEO_ASSETS = f"""mkdir -p release/config
for asset in geoip.dat geosite.dat; do
    if [ ! -f release/config/$asset ] && [ -f {GEO_ASSET_DIR}/$asset ]; then
        cp {GEO_ASSET_DIR}/$asset release/config/$asset
    fi
done"""


class V2rayCore0To1499ImageBase(Image):
    """Toolchain + full-history checkout, shared by every PR in this era.

    ``image_tag()`` is the constant ``"base-legacy"``, so ONE image serves all 16
    PRs in this era while the records carry 16 different ``base.sha`` values.
    That is why this Dockerfile declares its own ``# syntax`` directive: it makes
    ``DockerfileEnhancer.enhance()`` return the content verbatim, which is the
    only way to stop the enhancer's ``_standardize_repo_fetch`` from rewriting
    the clone below into ``git clone`` + ``git checkout ${BASE_COMMIT}`` +
    ``Image._HARDENING_BLOCK``.

    That rewrite is what the harness now does by default, and on a shared base
    tag it is fatal: the hardening block detaches at ``${BASE_COMMIT}``, deletes
    every ref and ``gc --prune``s the repository down to a single commit's
    history. Since the pipeline only builds this tag ONCE (images are deduped by
    full name, and BASE_COMMIT is whichever PR was scheduled first), the other 15
    PRs would then fail ``git checkout <their sha>`` with "reference is not a
    tree" -- and could not recover, because the same block removes ``origin``.

    So the base keeps FULL history (every era member's base.sha stays reachable)
    and takes only the hardening that is safe to share: the network remote is
    dropped, so no later layer -- and no agent -- can re-fetch upstream history.
    The strict per-PR hardening runs one tier up, in V2rayCore0To1499ImageDefault,
    where pinning to a single base.sha is correct.

    Opting out of the enhancer means the ARG/ENV/LABEL block it would have
    injected is no longer free, so the parts still wanted are spelled out inline.
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
        # Pre-go-modules v2ray-core (PRs 0-1499). Go 1.22 removed `go get` in
        # GOPATH mode, so we pin to 1.21 which still supports the workflow our
        # prepare.sh needs (module mode + writable module cache + curl).
        return "golang:1.21-bookworm"

    def image_tag(self) -> str:
        return "base-legacy"

    def workdir(self) -> str:
        return "base-legacy"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Validated before interpolation into the clone URL / WORKDIR paths.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git \\
    && rm -rf /var/lib/apt/lists/*

# Geo databases staged once here so the graded stages never touch the network.
RUN mkdir -p {GEO_ASSET_DIR} && \\
    curl -fsSL -o {GEO_ASSET_DIR}/geoip.dat \\
        https://github.com/v2fly/geoip/releases/latest/download/geoip.dat || true; \\
    curl -fsSL -o {GEO_ASSET_DIR}/geosite.dat \\
        https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat || true

{fetch}

# Drop the network remote from the shared base. Full history is deliberately
# retained here (see the class docstring); the per-PR image prunes it.
WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class V2rayCore0To1499ImageDefault(Image):
    """Per-PR grading image -- this is the tier that carries the hardening.

    ``Image._HARDENING_BLOCK`` runs BEFORE ``prepare.sh`` (unlike restic, where
    it runs after). prepare.sh in this era deliberately dirties the worktree --
    it deletes the tracked ``vendor/`` tree and drops in the pinned ``go.mod`` --
    and the hardening block ends with ``git status``-sensitive assertions plus a
    ``git checkout --detach``. Running it first means it sees a pristine tree,
    and prepare.sh then operates on an already-pinned, already-pruned checkout.
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
        return V2rayCore0To1499ImageBase(self.pr, self.config)

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
                "pinned_go.mod",
                """module v2ray.com/core

go 1.13

require (
\tgithub.com/golang/glog v0.0.0-20160126235308-23def4e6c14b
\tgithub.com/golang/protobuf v1.2.0
\tgithub.com/gorilla/websocket v1.2.0
\tgithub.com/miekg/dns v1.0.8
\tgolang.org/x/crypto v0.0.0-20190208162236-193df9c0f06f
\tgolang.org/x/net v0.0.0-20190206173232-65e2d4e15006
\tgolang.org/x/sync v0.0.0-20180314180146-1d60e4601c6f
\tgolang.org/x/sys v0.0.0-20180830151530-49385e6e1522
\tgolang.org/x/text v0.3.0
\tgoogle.golang.org/genproto v0.0.0-20180817151627-c66870c02cf8
\tgoogle.golang.org/grpc v1.18.0
\tv2ray.com/ext v0.0.0-20171226163434-694045b342ba
\th12.me/socks v0.0.0-20180505162055-cd352f5a4693
)

replace v2ray.com/ext => /home/v2ray-ext-stub
replace h12.me/socks => github.com/h12w/socks v0.0.0-20180505162055-cd352f5a4693
""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

# Pre-go-modules PRs lack a working dep manifest. Pin 2018-era versions and
# use replace directives for import paths whose hosting no longer resolves
# (v2ray.com/ext, h12.me/socks). GOTOOLCHAIN=local prevents Go from
# auto-upgrading when transitive deps request a newer toolchain.
export GOTOOLCHAIN=local

cd /home/{pr.repo}

# HEAD is already detached at {pr.base.sha} and the history has already been
# pruned to it by the hardening block in the Dockerfile, which runs before this
# script. These two lines are a cheap re-assertion of that pin; there is no
# `git fetch origin` fallback because the base image has no remote (verified:
# all 22 base SHAs in this dataset are reachable from a plain clone, so the old
# fallback was dead code even before the remote was dropped).
git reset --hard
bash /home/check_git_changes.sh

# Install the pinned manifest. Strip any partial vendor/ tree (none of the
# 0_to_1499 PRs ship a complete vendor, so vendor mode is unusable here).
rm -rf vendor go.mod go.sum
cp /home/pinned_go.mod ./go.mod

# Build a local v2ray.com/ext stub: clone the Dec-2017 snapshot and remove
# the tools/conf/*.go siblings that import v2ray.com/core packages absent
# from older bases (e.g., app/policy and common/log didn't exist in 2017).
# A local-directory `replace` bypasses module-cache hashing, so the strip works.
if [ ! -d /home/v2ray-ext-stub ]; then
    git clone https://github.com/v2ray/ext /home/v2ray-ext-stub
    (cd /home/v2ray-ext-stub && git checkout 694045b342ba0aee86b6601649c51e1fc51914b1)
    # Drop the stub's git metadata entirely: it is a build-time scaffold, not
    # graded source, and leaving a live remote inside the image would reopen the
    # network path the hardening block exists to close.
    rm -rf /home/v2ray-ext-stub/.git
    rm -f /home/v2ray-ext-stub/tools/conf/*.go
    # serial/loader.go imports tools/conf (parent) and uses conf.Config.Build().
    # Write a minimal stub satisfying that interface so the package compiles
    # without pulling in the original siblings that reference unavailable
    # v2ray.com/core subpackages.
    cat > /home/v2ray-ext-stub/tools/conf/stub.go <<'EOF'
package conf

import "v2ray.com/core"

type Config struct{{}}

func (c *Config) Build() (*core.Config, error) {{
    return &core.Config{{}}, nil
}}
EOF
    # The cloned tree predates go modules -- add a minimal go.mod for the replace target.
    cat > /home/v2ray-ext-stub/go.mod <<'EOF'
module v2ray.com/ext

go 1.13
EOF
fi

# Some v2ray-core tests load geoip.dat / geosite.dat via a GOPATH-style path
# (/go/src/v2ray.com/core/release/config/*.dat) regardless of module mode.
mkdir -p /go/src/v2ray.com
ln -sfn /home/{pr.repo} /go/src/v2ray.com/core

# tidy populates go.sum + adds missing transitive deps; -e keeps going on errors.
# Skip the warm-up `go test ./...` -- it compiles every package in parallel and
# can OOM-kill the build container; the actual run.sh / test-run.sh / fix-run.sh
# do the test compilation at execution time instead.
go mod tidy -e -compat=1.13 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export GOTOOLCHAIN=local

cd /home/{pr.repo}

{stage_geo}

go test -v -count=1 -timeout 15m {skip} -vet=off -tags json ./...

""".format(pr=self.pr, skip=GO_TEST_SKIP, stage_geo=STAGE_GEO_ASSETS),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export GOTOOLCHAIN=local

cd /home/{pr.repo}

# STRICT: no `|| true`, no --reject, no .rej deletion. A test patch that does
# not apply must fail this stage loudly rather than silently grade the unpatched
# tree. Verified: all 22 test patches in this dataset apply cleanly at their
# base sha with these excludes.
git apply --whitespace=nowarn {excludes} /home/test.patch

# Re-tidy after applying patches: test.patch / fix.patch may add code that
# imports packages not in the unpatched source's import graph (e.g. grpc).
# The unpatched prepare.sh tidy can't anticipate these, so retidy here with
# the pinned go.mod as the baseline. -compat=1.13 keeps pin discipline.
cp /home/pinned_go.mod ./go.mod
rm -f go.sum
go mod tidy -e -compat=1.13 || true

{stage_geo}

go test -v -count=1 -timeout 15m {skip} -vet=off -tags json ./...

""".format(pr=self.pr, skip=GO_TEST_SKIP, excludes=PATCH_EXCLUDES, stage_geo=STAGE_GEO_ASSETS),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export GOTOOLCHAIN=local

cd /home/{pr.repo}

# STRICT, same rationale as test-run.sh. Verified: all 22 fix patches apply
# cleanly on top of their test patch with these excludes.
git apply --whitespace=nowarn {excludes} /home/test.patch
git apply --whitespace=nowarn {excludes} /home/fix.patch

# Re-tidy after applying patches (see test-run.sh comment).
cp /home/pinned_go.mod ./go.mod
rm -f go.sum
go mod tidy -e -compat=1.13 || true

{stage_geo}

go test -v -count=1 -timeout 15m {skip} -vet=off -tags json ./...

""".format(pr=self.pr, skip=GO_TEST_SKIP, excludes=PATCH_EXCLUDES, stage_geo=STAGE_GEO_ASSETS),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # This image's dependency() is an Image, so DockerfileEnhancer returns
        # the content verbatim and injects nothing -- the hardening has to be
        # emitted here explicitly. ${BASE_COMMIT} is substituted with the literal
        # sha because the pipeline only passes REPO_URL/BASE_COMMIT build args to
        # string-dependency (base) images. Concatenating the block through
        # .replace rather than an f-string keeps its %(refname) tokens literal.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

WORKDIR /home/{repo}

{hardening}

WORKDIR /home/

{prepare_commands}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("v2ray", "v2ray-core_0_to_1499")
class V2RAY_CORE_0_TO_1499(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return V2rayCore0To1499ImageDefault(self.pr, self._config)

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
        re_fail_tests = [re.compile(r"--- FAIL: (\S+)")]
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
                    if test_name in failed_tests:
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# instance.py routes on f"{org}/{number_interval}" whenever number_interval is
# set, so every dash-joined bundle value a record in THIS era can carry must
# resolve to a class. Without these keys the from_json patch above leaves
# number_interval empty and Instance.create falls back to "v2ray/v2ray-core",
# which is deliberately not registered (the era split makes a single bare key
# ambiguous) and raises.
#
# Explicit dash-joined member lists, never ranges -- the bundles are sparse.
# These are the 16 bundles whose base commit predates go modules.
_BUNDLE_NIS_V2RAY_LEGACY = [
    "651-685",  # pr-651 (2 PRs)
    "700-716",  # pr-700 (2 PRs)
    "758-786-861-863",  # pr-758 (4 PRs)
    "883-884",  # pr-883 (2 PRs)
    "927-928-929-931-934",  # pr-927 (5 PRs)
    "946-962",  # pr-946 (2 PRs)
    "968-981-982-985-992",  # pr-968 (5 PRs)
    "1008-1013",  # pr-1008 (2 PRs)
    "1019-1024-1035-1037-1038-1039-1041-1045",  # pr-1019 (8 PRs)
    "1053-1054-1055-1056-1057",  # pr-1053 (5 PRs)
    "1269-1270",  # pr-1269 (2 PRs)
    "1291-1292-1293",  # pr-1291 (3 PRs)
    "1301-1302",  # pr-1301 (2 PRs)
    "1314-1324",  # pr-1314 (2 PRs)
    "1350-1352",  # pr-1350 (2 PRs)
    "1435-1470",  # pr-1435 (2 PRs)
]

for _ni in _BUNDLE_NIS_V2RAY_LEGACY:
    Instance.register("v2ray", _ni)(V2RAY_CORE_0_TO_1499)
