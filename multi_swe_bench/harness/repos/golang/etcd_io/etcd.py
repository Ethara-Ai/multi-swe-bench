import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------

_DEFAULT_GO_IMAGE: dict[str, str] = {
    "etcd_gopath": "golang:1.12",
    "etcd_single_module": "golang:1.22",
    "etcd_multi_module": "golang:1.22",
}

_GO_IMAGE_MAP: dict[tuple[str, str], str] = {
    ("etcd_gopath", "3.1"): "golang:1.8",
    ("etcd_gopath", "3.2"): "golang:1.8",
    ("etcd_gopath", "3.3"): "golang:1.12",

    ("etcd_multi_module", "3.5"): "golang:1.24",
    ("etcd_multi_module", "3.6"): "golang:1.24",
    ("etcd_single_module", "3.4"): "golang:1.24",
}


def _get_release_line(pr: "PullRequest") -> str:
    ref = pr.base.ref if pr.base else ""
    if ref.startswith("release-"):
        return ref.replace("release-", "")
    return ref


def _parse_go_image_from_tag(tag: str) -> Optional[str]:
    """Extract Go Docker image from a versioned tag.

    Tags follow the format ``{strategy}_go{major}.{minor}`` (e.g.
    ``gopath_go1.8``, ``single_module_go1.12``).  Returns a Docker image
    string like ``"golang:1.8"`` or *None* when the tag does not encode a
    Go version.
    """
    m = re.search(r"_go(\d+\.\d+)$", tag)
    if m:
        return f"golang:{m.group(1)}"
    return None


def _resolve_go_version(pr: "PullRequest", strategy: str) -> str:
    go_image = _parse_go_image_from_tag(pr.tag)
    if go_image:
        return go_image.replace("golang:", "")
    rl = _get_release_line(pr)
    key = (strategy, rl)
    if key in _GO_IMAGE_MAP:
        return _GO_IMAGE_MAP[key].replace("golang:", "")
    return _DEFAULT_GO_IMAGE[strategy].replace("golang:", "")


# ---------------------------------------------------------------------------
# Helper: patch application with fallback chain
# ---------------------------------------------------------------------------

def _apply_patch_cmd(patch_files: list[str]) -> str:
    cmds = []
    for pf in patch_files:
        cmds.append(
            f'if [ -s "{pf}" ]; then\n'
            f'    git apply --whitespace=nowarn "{pf}" || \\\n'
            f'    git apply --whitespace=nowarn --3way "{pf}" || \\\n'
            f'    (git apply --whitespace=nowarn --reject "{pf}" && find . -name "*.rej" -delete) || \\\n'
            f'    patch --batch --fuzz=5 -p1 < "{pf}" || \\\n'
            f'    {{ echo "ERROR: Failed to apply {pf}"; exit 1; }}\n'
            f"fi"
        )
    return "\n".join(cmds)


# ---------------------------------------------------------------------------
# Shared script fragments
# ---------------------------------------------------------------------------

_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
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

_GO_ENV_GOPATH = """\
export GO111MODULE=off"""

# Set up GOPATH so that cmd/vendor is the source for third-party deps.
# etcd 3.1-3.3 ships deps in cmd/vendor/ with a symlink back to the repo
# root at cmd/vendor/github.com/coreos/etcd -> ../../../../
# This mirrors what the upstream "build" script does via etcd_setup_gopath().
_GOPATH_VENDOR_SETUP = """\
cd /go/src/github.com/coreos/etcd
rm -rf gopath/src
mkdir -p gopath
ln -sf "$(pwd)/cmd/vendor" "$(pwd)/gopath/src"
export GOPATH="$(pwd)/gopath:/go"
export GO111MODULE=off"""

# NOTE on -mod=mod: several etcd release lines (notably 3.4) ship an
# *inconsistent* committed vendor/ tree (incomplete vendor/modules.txt) at the
# tested base SHAs.  Whenever a vendor/ dir is present, Go >=1.14 auto-selects
# -mod=vendor and then fails to compile ("cannot find package .../vendor/...").
# Running `go mod vendor`/`go mod tidy` to repair it instead rewrites go.mod and
# yields the opposite error ("no required module provides package").  Either way
# nothing compiles and every test reports (0,0,0) -- a pure build/env failure,
# not a real fix signal.  Forcing -mod=mod makes Go ignore the broken vendor dir
# and resolve modules from GOPROXY, which compiles cleanly.  GOSUMDB=off avoids
# checksum-db lookups (the legacy GONOSUMCHECK/GONOSUMDB vars are no-ops on
# modern Go).
_GO_ENV_MODULE = """\
export GO111MODULE=on
export GOTOOLCHAIN=auto
export GOSUMDB=off
export GONOSUMCHECK=*
export GONOSUMDB=*
export GOPROXY=https://proxy.golang.org
export GOFLAGS="-mod=mod -p=1"
# etcd refuses to start on non-amd64 arches unless this is set; without it every
# e2e test that spawns the real etcd/etcdctl binary fails on arm64 hosts.
export ETCD_UNSUPPORTED_ARCH="$(go env GOARCH)\""""

# Build the etcd/etcdctl binaries before running tests.  The e2e suites
# (tests/e2e/...) spawn the real binaries as subprocesses; without them the
# e2e tests fail with "binary not found" -- an *environmental* failure rather
# than a genuine fix-to-pass signal.  Building here makes those f2p/n2p tests
# genuine.  Guarded with `|| true` so a build hiccup never aborts the test run
# (the unit/integration tests still execute and report).
_BUILD_BINARIES = """\
# --- build etcd/etcdctl so e2e tests can spawn real binaries ---
export BINDIR="$(pwd)/bin"
mkdir -p "$BINDIR"
( make build ) || ( ./build.sh ) || ( ./build ) || true
export PATH="$BINDIR:$PATH"
export ETCD_BIN_DIR="$BINDIR"
export ETCD_BIN_PATH="$BINDIR/etcd"
export ETCDCTL_BIN_PATH="$BINDIR/etcdctl"
# ----------------------------------------------------------------"""

_MULTI_MOD_FIND = (
    "find . -name 'go.mod' -not -path './vendor/*' -not -path './.git/*' | sort"
)

# Robust `patch` installation that works on both current and archived Debian repos.
# golang:1.8–1.10 are based on Debian stretch (archived), 1.12 on buster (archived),
# newer images use bullseye/bookworm (current).  The fallback handles both.
_INSTALL_PATCH_CMD = (
    "RUN { apt-get update 2>/dev/null || "
    "{ sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && "
    "sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && "
    "sed -i '/-updates/d' /etc/apt/sources.list && "
    "apt-get update; }; } && "
    "apt-get install -y --no-install-recommends patch && "
    "rm -rf /var/lib/apt/lists/*"
)


# ---------------------------------------------------------------------------
# GOPATH strategy scripts  (release-3.1 / 3.2 / 3.3 / old master)
# ---------------------------------------------------------------------------

def _gopath_prepare(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"git reset --hard\n"
        f"bash /home/check_git_changes.sh\n"
        f"git checkout {pr.base.sha}\n"
        f"bash /home/check_git_changes.sh\n"
        f"\n"
        f"{_GOPATH_VENDOR_SETUP}\n"
        f"\n"
        f"go test -v -count=1 -vet=off ./... || true\n"
    )


def _gopath_run(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"{_GOPATH_VENDOR_SETUP}\n"
        f"\n"
        f"go test -v -count=1 -vet=off ./...\n"
    )


def _gopath_test_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"{patch_test}\n"
        f"\n"
        f"{_GOPATH_VENDOR_SETUP}\n"
        f"\n"
        f"go test -v -count=1 -vet=off ./...\n"
    )


def _gopath_fix_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    patch_fix = _apply_patch_cmd(["/home/fix.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"{patch_test}\n"
        f"{patch_fix}\n"
        f"\n"
        f"{_GOPATH_VENDOR_SETUP}\n"
        f"\n"
        f"go test -v -count=1 -vet=off ./...\n"
    )


# ---------------------------------------------------------------------------
# Single-module strategy scripts  (release-3.4)
# ---------------------------------------------------------------------------

def _single_module_prepare(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"git reset --hard\n"
        f"bash /home/check_git_changes.sh\n"
        f"git checkout {pr.base.sha}\n"
        f"bash /home/check_git_changes.sh\n"
        f"\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod download || true\n"
        f"go test -v -count=1 ./... || true\n"
    )


def _single_module_run(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod download || true\n"
        f"{_BUILD_BINARIES}\n"
        f"\n"
        f"go test -v -count=1 ./...\n"
    )


def _single_module_test_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"{patch_test}\n"
        f"\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod download || true\n"
        f"{_BUILD_BINARIES}\n"
        f"\n"
        f"go test -v -count=1 ./...\n"
    )


def _single_module_fix_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    patch_fix = _apply_patch_cmd(["/home/fix.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"{patch_test}\n"
        f"{patch_fix}\n"
        f"\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod download || true\n"
        f"{_BUILD_BINARIES}\n"
        f"\n"
        f"go test -v -count=1 ./...\n"
    )


# ---------------------------------------------------------------------------
# Multi-module strategy scripts  (release-3.5 / 3.6 / main)
# ---------------------------------------------------------------------------

def _multi_module_prepare(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"git reset --hard\n"
        f"bash /home/check_git_changes.sh\n"
        f"git checkout {pr.base.sha}\n"
        f"bash /home/check_git_changes.sh\n"
        f"\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"{_MULTI_MOD_FIND} | while read modfile; do\n"
        f'  dir=$(dirname "$modfile")\n'
        f'  echo "=== Preparing module: $dir ==="\n'
        f'  (cd "$dir" && go mod download) || true\n'
        f'  (cd "$dir" && go test -v -count=1 ./...) || true\n'
        f"done\n"
    )


def _multi_module_run(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"{_BUILD_BINARIES}\n"
        f"\n"
        f"{_MULTI_MOD_FIND} | while read modfile; do\n"
        f'  dir=$(dirname "$modfile")\n'
        f'  echo "=== Testing module: $dir ==="\n'
        f'  (cd "$dir" && go test -v -count=1 ./...) || true\n'
        f"done\n"
    )


def _multi_module_test_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"{patch_test}\n"
        f"\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"{_MULTI_MOD_FIND} | while read modfile; do\n"
        f'  dir=$(dirname "$modfile")\n'
        f'  (cd "$dir" && go mod download) || true\n'
        f"done\n"
        f"\n"
        f"{_BUILD_BINARIES}\n"
        f"\n"
        f"{_MULTI_MOD_FIND} | while read modfile; do\n"
        f'  dir=$(dirname "$modfile")\n'
        f'  echo "=== Testing module: $dir ==="\n'
        f'  (cd "$dir" && go test -v -count=1 ./...) || true\n'
        f"done\n"
    )


def _multi_module_fix_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    patch_fix = _apply_patch_cmd(["/home/fix.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/etcd\n"
        f"{patch_test}\n"
        f"{patch_fix}\n"
        f"\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"{_MULTI_MOD_FIND} | while read modfile; do\n"
        f'  dir=$(dirname "$modfile")\n'
        f'  (cd "$dir" && go mod download) || true\n'
        f"done\n"
        f"\n"
        f"{_BUILD_BINARIES}\n"
        f"\n"
        f"{_MULTI_MOD_FIND} | while read modfile; do\n"
        f'  dir=$(dirname "$modfile")\n'
        f'  echo "=== Testing module: $dir ==="\n'
        f'  (cd "$dir" && go test -v -count=1 ./...) || true\n'
        f"done\n"
    )


# ---------------------------------------------------------------------------
# Script function dispatch tables
# ---------------------------------------------------------------------------

_PREPARE_FN = {
    "etcd_gopath": _gopath_prepare,
    "etcd_single_module": _single_module_prepare,
    "etcd_multi_module": _multi_module_prepare,
}

_RUN_FN = {
    "etcd_gopath": _gopath_run,
    "etcd_single_module": _single_module_run,
    "etcd_multi_module": _multi_module_run,
}

_TEST_RUN_FN = {
    "etcd_gopath": _gopath_test_run,
    "etcd_single_module": _single_module_test_run,
    "etcd_multi_module": _multi_module_test_run,
}

_FIX_RUN_FN = {
    "etcd_gopath": _gopath_fix_run,
    "etcd_single_module": _single_module_fix_run,
    "etcd_multi_module": _multi_module_fix_run,
}


# ---------------------------------------------------------------------------
# Image classes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# THREE-LEVEL REGISTRY
#
# The pipeline's DockerfileEnhancer (multi_swe_bench/harness/image.py) appends a
# "clone-hardening" block to ANY image whose ``dependency()`` returns a *string*
# (a raw Docker tag).  That block runs ``git checkout --detach ${BASE_COMMIT}``,
# removes ``origin``, deletes every ref, and ``git gc --prune``s unreachable
# objects.  If the image that gets hardened is *shared* across PRs (one image per
# Go version), it is pinned to whichever PR built first and stripped of the other
# PRs' commits -> their ``git checkout <base.sha>`` fails -> 0 resolved.
#
# To survive this, only the per-PR leaf may depend on a string.  We therefore use
# three levels:
#
#   1. EtcdImageBase  (toolchain)  FROM golang:X.Y  -- dependency() -> str.
#        The enhancer runs but, because this image performs NO ``git clone``,
#        its final-sanitize step is a no-op: only the harmless proxy/cert infra
#        block is injected, never the hardening block.  Shared per Go version.
#   2. EtcdImageRepo  (full clone) FROM base-goX_Y  -- dependency() -> Image.
#        The enhancer returns this dockerfile verbatim (no hardening), so the
#        clone keeps ``origin`` + every ref + full history.  Shared per Go
#        version; never pinned to a base.sha.
#   3. EtcdImageDefault (per PR)   FROM repo-goX_Y  -- dependency() -> Image.
#        Also returned verbatim.  prepare.sh checks out this PR's base.sha from
#        the full history present in level 2.
# ---------------------------------------------------------------------------


class EtcdImageBase(Image):
    """Level 1: shared Go toolchain image.  No source clone (keeps the
    enhancer's hardening block from ever being appended)."""

    def __init__(self, pr: PullRequest, config: Config, go_version: str):
        self._pr = pr
        self._config = config
        self._go_version = go_version

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return f"golang:{self._go_version}"

    def image_tag(self) -> str:
        return f"base-go{self._go_version.replace('.', '_')}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        go_image = self.dependency()

        return f"""FROM {go_image}

{self.global_env}

{_INSTALL_PATCH_CMD}

WORKDIR /home/

{self.clear_env}

ENV GOPATH=/go
ENV GO111MODULE=off
ENV GOTOOLCHAIN=auto
ENV GONOSUMCHECK=*
ENV GONOSUMDB=*
ENV GOPROXY=https://proxy.golang.org
"""


class EtcdImageRepo(Image):
    """Level 2: shared full clone of the repo on top of the toolchain.  Because
    ``dependency()`` returns an Image, the enhancer leaves this dockerfile
    untouched -- the clone retains ``origin`` and the complete history, so any
    PR's base.sha remains checkout-able from level 3."""

    def __init__(self, pr: PullRequest, config: Config, go_version: str):
        self._pr = pr
        self._config = config
        self._go_version = go_version

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return EtcdImageBase(self.pr, self.config, self._go_version)

    def image_tag(self) -> str:
        return f"repo-go{self._go_version.replace('.', '_')}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_image = self.dependency()
        name = base_image.image_name()
        tag = base_image.image_tag()
        gopath_dir = f"/go/src/github.com/coreos/{self.pr.repo}"

        clone_code = (
            f"RUN mkdir -p /go/src/github.com/coreos /go/src/go.etcd.io && \\\n"
            f"    git clone https://github.com/{self.pr.org}/{self.pr.repo}.git {gopath_dir} && \\\n"
            f"    ln -sf {gopath_dir} /home/{self.pr.repo} && \\\n"
            f"    ln -sf {gopath_dir} /go/src/go.etcd.io/{self.pr.repo}"
        )

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/

{clone_code}

{self.clear_env}
"""


class EtcdImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config, strategy: str):
        self._pr = pr
        self._config = config
        self._strategy = strategy

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        go_version = _resolve_go_version(self.pr, self._strategy)
        return EtcdImageRepo(self.pr, self.config, go_version)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare_fn = _PREPARE_FN[self._strategy]
        run_fn = _RUN_FN[self._strategy]
        test_run_fn = _TEST_RUN_FN[self._strategy]
        fix_run_fn = _FIX_RUN_FN[self._strategy]

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare_fn(self.pr)),
            File(".", "run.sh", run_fn(self.pr)),
            File(".", "test-run.sh", test_run_fn(self.pr)),
            File(".", "fix-run.sh", fix_run_fn(self.pr)),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency()
        if isinstance(base_image, Image):
            name = base_image.image_name()
            tag = base_image.image_tag()
        else:
            name = base_image
            tag = "latest"

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


# ---------------------------------------------------------------------------
# Instance classes  (one per strategy, registered for tag-based dispatch)
# ---------------------------------------------------------------------------

def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

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
                base_name = get_base_name(pass_match.group(1))
                if base_name in failed_tests:
                    continue
                if base_name in skipped_tests:
                    skipped_tests.remove(base_name)
                passed_tests.add(base_name)

        for re_fail_test in re_fail_tests:
            fail_match = re_fail_test.match(line)
            if fail_match:
                base_name = get_base_name(fail_match.group(1))
                if base_name in passed_tests:
                    passed_tests.remove(base_name)
                if base_name in skipped_tests:
                    skipped_tests.remove(base_name)
                failed_tests.add(base_name)

        for re_skip_test in re_skip_tests:
            skip_match = re_skip_test.match(line)
            if skip_match:
                base_name = get_base_name(skip_match.group(1))
                if base_name in passed_tests:
                    continue
                failed_tests.discard(base_name)
                skipped_tests.add(base_name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class _EtcdInstanceBase(Instance):

    STRATEGY: str = ""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return EtcdImageDefault(self.pr, self._config, self.STRATEGY)

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
        return _parse_go_test_log(test_log)


@Instance.register("etcd-io", "etcd_gopath")
class EtcdGopath(_EtcdInstanceBase):
    STRATEGY = "etcd_gopath"


@Instance.register("etcd-io", "etcd_single_module")
class EtcdSingleModule(_EtcdInstanceBase):
    STRATEGY = "etcd_single_module"


@Instance.register("etcd-io", "etcd_multi_module")
class EtcdMultiModule(_EtcdInstanceBase):
    STRATEGY = "etcd_multi_module"


# ---------------------------------------------------------------------------
# Dynamic registration: versioned tags encode both strategy AND Go version.
#
# JSONL tag format: ``{strategy}_go{major}.{minor}``
#   e.g.  "gopath_go1.8"  →  Instance.register key "etcd_gopath_go1_8"
#         "single_module_go1.14" → "etcd_single_module_go1_14"
#
# Instance.create applies tag.replace('.', '_'), so the register key is
# f"etcd_{tag_with_underscores}".
#
# Only combos that exist in the dataset are registered.
# ---------------------------------------------------------------------------

_VERSIONED_TAGS: list[tuple[str, str, list[str]]] = [
    ("gopath", "etcd_gopath", ["1.8", "1.12"]),
    ("single_module", "etcd_single_module", [
        "1.12", "1.14", "1.16", "1.17", "1.19",
        "1.20", "1.21", "1.22", "1.23", "1.24",
    ]),
    ("multi_module", "etcd_multi_module", [
        "1.16", "1.19", "1.20", "1.21", "1.23", "1.24",
    ]),
]

for _sn, _sk, _versions in _VERSIONED_TAGS:
    for _gv in _versions:
        _tag_raw = f"{_sn}_go{_gv}"
        _reg_key = f"etcd_{_tag_raw.replace('.', '_')}"
        _cls = type(
            f"_Etcd_{_reg_key}",
            (_EtcdInstanceBase,),
            {"STRATEGY": _sk},
        )
        Instance.register("etcd-io", _reg_key)(_cls)


# ---------------------------------------------------------------------------
# Bundle number_interval registrations.
#
# Release-bundled instances carry number_interval="etcd_<first>_to_<last>"
# (first/last PR number of the bundle).  Instance.create() routes on
# f"{org}/{number_interval}" *before* falling back to the tag, so every bundle
# interval that appears in the dataset must be registered here.  The strategy
# is fixed per bundle; the Go version is still resolved from pr.tag (which the
# records retain), so routing stays identical to the tag-based path.
# ---------------------------------------------------------------------------

_NUMBER_INTERVALS: list[tuple[str, str]] = [
    ("etcd_14410_to_14442", "etcd_single_module"),
    ("etcd_14530_to_14675", "etcd_single_module"),
    ("etcd_15038_to_15280", "etcd_single_module"),
    ("etcd_15774_to_15860", "etcd_multi_module"),
    ("etcd_15788_to_15861", "etcd_single_module"),
]

for _interval, _strategy in _NUMBER_INTERVALS:
    _cls = type(
        f"_Etcd_{_interval}",
        (_EtcdInstanceBase,),
        {"STRATEGY": _strategy},
    )
    Instance.register("etcd-io", _interval)(_cls)


# ---------------------------------------------------------------------------
# Backward-compatible instance for PRs without a tag (batch-1 format).
# Determines strategy dynamically from base.ref when no tag is provided.
# ---------------------------------------------------------------------------

_REF_TO_STRATEGY = {
    "release-3.1": "etcd_gopath",
    "release-3.2": "etcd_gopath",
    "release-3.3": "etcd_gopath",
    "release-3.4": "etcd_single_module",
    "release-3.5": "etcd_multi_module",
    "release-3.6": "etcd_multi_module",
    "main": "etcd_multi_module",
    "master": "etcd_multi_module",
}


@Instance.register("etcd-io", "etcd")
class Etcd(_EtcdInstanceBase):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__(pr, config, *args, **kwargs)
        # Determine strategy from base.ref when no tag is set
        ref = pr.base.ref if pr.base else ""
        self._dynamic_strategy = _REF_TO_STRATEGY.get(ref, "etcd_single_module")

    def dependency(self) -> Image:
        return EtcdImageDefault(self.pr, self._config, self._dynamic_strategy)
