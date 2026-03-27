import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------

_DEFAULT_GO_IMAGE: dict[str, str] = {
    "influxdb_gdm": "golang:1.11",
    "influxdb_gomod": "golang:1.13",
    "influxdb_gomod_rust": "golang:1.19",
    "influxdb_v2": "golang:1.20",
}

_GO_IMAGE_MAP: dict[tuple[str, str], str] = {
    ("influxdb_gdm", "1.1"): "golang:1.7.3",
    ("influxdb_gdm", "1.2"): "golang:1.7.4",
    ("influxdb_gdm", "1.3"): "golang:1.8.3",
    ("influxdb_gdm", "1.4"): "golang:1.9.2",
    ("influxdb_gdm", "1.5"): "golang:1.9.2",
    ("influxdb_gdm", "1.6"): "golang:1.10",
    ("influxdb_gdm", "1.7"): "golang:1.11",
    ("influxdb_gomod", "1.8"): "golang:1.13",
    ("influxdb_gomod_rust", "1.9"): "golang:1.15",
    ("influxdb_gomod_rust", "1.10"): "golang:1.18",
    ("influxdb_gomod_rust", "1.11"): "golang:1.23",
    ("influxdb_gomod_rust", "1.12"): "golang:1.23",
    ("influxdb_gomod_rust", "master-1.x"): "golang:1.23",
    ("influxdb_v2", "2.0"): "golang:1.15",
    ("influxdb_v2", "2.7"): "golang:1.23",
}


def _resolve_strategy(pr: "PullRequest") -> str:
    """Determine build strategy from base ref, with heuristic fallback for
    edge cases (late v1.7 PRs, heterogeneous master PRs)."""
    ref = pr.base.ref if pr.base else ""
    strategy = _REF_TO_STRATEGY.get(ref)
    if strategy:
        # Edge case: late v1.7 PRs with go.mod (PR#15609, #16382)
        if ref == "1.7" and pr.number >= 15609:
            return "influxdb_gomod"
        return strategy
    # NOTE: "master" is intentionally EXCLUDED from _REF_TO_STRATEGY
    # because master PRs are heterogeneous (some are gdm-era, some are v2-era).
    # Old master PRs (#8878, #9509, #9649) are gdm-era (v1.x)
    # New master PRs (#23270) are v2-era
    if ref == "master" or ref == "main":
        if pr.number >= 20000:
            return "influxdb_v2"
        return "influxdb_gdm"
    # Ultimate fallback for unknown refs
    return "influxdb_gomod"


def _resolve_go_version(pr: "PullRequest", strategy: str) -> str:
    ref = pr.base.ref if pr.base else ""
    key = (strategy, ref)
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

_GO_ENV_MODULE = """\
export GOTOOLCHAIN=auto
export GONOSUMCHECK=*
export GONOSUMDB=*
export GOPROXY=https://proxy.golang.org"""

# Robust `patch` installation that works on both current and archived Debian repos.
# golang:1.7–1.10 are based on Debian stretch (archived), 1.12 on buster (archived),
# newer images use bullseye/bookworm (current).  The fallback handles both.
_INSTALL_PATCH_CMD = (
    "RUN apt-get update 2>/dev/null && apt-get install -y --no-install-recommends patch && rm -rf /var/lib/apt/lists/* || "
    "{ CODENAME=$(sed -n 's/.*(\\(.*\\)).*/\\1/p' /etc/os-release | head -1) && "
    "echo \"deb [trusted=yes] http://archive.debian.org/debian $CODENAME main\" > /etc/apt/sources.list && "
    "apt-get -o Acquire::Check-Valid-Until=false update && "
    "apt-get install -y --no-install-recommends --allow-unauthenticated patch && "
    "rm -rf /var/lib/apt/lists/*; }"
)

# Rust toolchain for gomod_rust and v2 strategies.
# Installed in Dockerfile; scripts just source cargo env.
_RUST_INSTALL_CMD = (
    "RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | "
    "sh -s -- --default-toolchain stable -y"
)

# Extra dependencies for v2 builds (bzr, clang, protobuf).
_V2_EXTRA_DEPS_CMD = (
    "RUN apt-get update 2>/dev/null && apt-get install -y --no-install-recommends bzr clang protobuf-compiler && rm -rf /var/lib/apt/lists/* || "
    "{ CODENAME=$(sed -n 's/.*(\\(.*\\)).*/\\1/p' /etc/os-release | head -1) && "
    "echo \"deb [trusted=yes] http://archive.debian.org/debian $CODENAME main\" > /etc/apt/sources.list && "
    "apt-get -o Acquire::Check-Valid-Until=false update && "
    "apt-get install -y --no-install-recommends --allow-unauthenticated bzr clang protobuf-compiler && "
    "rm -rf /var/lib/apt/lists/*; }"
)


# ---------------------------------------------------------------------------
# GDM strategy scripts  (v1.1 – v1.7 + old master)
# ---------------------------------------------------------------------------

_GOPATH_DIR = "/go/src/github.com/influxdata/influxdb"


def _gdm_prepare(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd {_GOPATH_DIR}\n"
        f"git config core.filemode false\n"
        f"git reset --hard\n"
        f"git checkout -- .\n"
        f"git clean -fd\n"
        f"bash /home/check_git_changes.sh\n"
        f"git checkout {pr.base.sha}\n"
        f"git checkout -- .\n"
        f"git clean -fd\n"
        f"bash /home/check_git_changes.sh\n"
        f"\n"
        f"{_GO_ENV_GOPATH}\n"
        f"\n"
        f"go get github.com/sparrc/gdm\n"
        f"gdm restore -v\n"
        f"go test -v -count=1 ./... || true\n"
    )


def _gdm_run(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd {_GOPATH_DIR}\n"
        f"{_GO_ENV_GOPATH}\n"
        f"\n"
        f"go test -v -count=1 ./...\n"
    )


def _gdm_test_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd {_GOPATH_DIR}\n"
        f"{patch_test}\n"
        f"\n"
        f"{_GO_ENV_GOPATH}\n"
        f"\n"
        f"go test -v -count=1 ./...\n"
    )


def _gdm_fix_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    patch_fix = _apply_patch_cmd(["/home/fix.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd {_GOPATH_DIR}\n"
        f"{patch_test}\n"
        f"{patch_fix}\n"
        f"\n"
        f"{_GO_ENV_GOPATH}\n"
        f"\n"
        f"go test -v -count=1 ./...\n"
    )


# ---------------------------------------------------------------------------
# Go-module strategy scripts  (v1.8, no Rust)
# ---------------------------------------------------------------------------

def _gomod_prepare(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"git config core.filemode false\n"
        f"git reset --hard\n"
        f"git checkout -- .\n"
        f"git clean -fd\n"
        f"bash /home/check_git_changes.sh\n"
        f"git checkout {pr.base.sha}\n"
        f"git checkout -- .\n"
        f"git clean -fd\n"
        f"bash /home/check_git_changes.sh\n"
        f"\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod vendor || true\n"
        f"go mod download || true\n"
        f"go mod tidy || true\n"
        f"go test -v -count=1 ./... || true\n"
    )


def _gomod_run(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"{_GO_ENV_MODULE}\n"
        f"go test -v -count=1 ./...\n"
    )


def _gomod_test_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"{patch_test}\n"
        f"\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod vendor || true\n"
        f"go mod download || true\n"
        f"go mod tidy || true\n"
        f"go test -v -count=1 ./...\n"
    )


def _gomod_fix_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    patch_fix = _apply_patch_cmd(["/home/fix.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"{patch_test}\n"
        f"{patch_fix}\n"
        f"\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod vendor || true\n"
        f"go mod download || true\n"
        f"go mod tidy || true\n"
        f"go test -v -count=1 ./...\n"
    )


# ---------------------------------------------------------------------------
# Go-module + Rust strategy scripts  (v1.9 – v1.12, master-1.x)
# ---------------------------------------------------------------------------

_CARGO_ENV = ". $HOME/.cargo/env"


def _gomod_rust_prepare(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"git config core.filemode false\n"
        f"git reset --hard\n"
        f"git checkout -- .\n"
        f"git clean -fd\n"
        f"bash /home/check_git_changes.sh\n"
        f"git checkout {pr.base.sha}\n"
        f"git checkout -- .\n"
        f"git clean -fd\n"
        f"bash /home/check_git_changes.sh\n"
        f"\n"
        f"{_CARGO_ENV}\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod vendor || true\n"
        f"go mod download || true\n"
        f"go mod tidy || true\n"
        f"go test -v -count=1 ./... || true\n"
    )


def _gomod_rust_run(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"{_CARGO_ENV}\n"
        f"{_GO_ENV_MODULE}\n"
        f"go test -v -count=1 ./...\n"
    )


def _gomod_rust_test_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"{patch_test}\n"
        f"\n"
        f"{_CARGO_ENV}\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod vendor || true\n"
        f"go mod download || true\n"
        f"go mod tidy || true\n"
        f"go test -v -count=1 ./...\n"
    )


def _gomod_rust_fix_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    patch_fix = _apply_patch_cmd(["/home/fix.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"{patch_test}\n"
        f"{patch_fix}\n"
        f"\n"
        f"{_CARGO_ENV}\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod vendor || true\n"
        f"go mod download || true\n"
        f"go mod tidy || true\n"
        f"go test -v -count=1 ./...\n"
    )


# ---------------------------------------------------------------------------
# v2 strategy scripts  (v2.0, v2.7 + new master)
# ---------------------------------------------------------------------------

def _v2_prepare(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"git config core.filemode false\n"
        f"git reset --hard\n"
        f"git checkout -- .\n"
        f"git clean -fd\n"
        f"bash /home/check_git_changes.sh\n"
        f"git checkout {pr.base.sha}\n"
        f"git checkout -- .\n"
        f"git clean -fd\n"
        f"bash /home/check_git_changes.sh\n"
        f"\n"
        f"{_CARGO_ENV}\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod vendor || true\n"
        f"go mod download || true\n"
        f"go mod tidy || true\n"
        f"go test -v -count=1 ./... || true\n"
    )


def _v2_run(pr: PullRequest) -> str:
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"{_CARGO_ENV}\n"
        f"{_GO_ENV_MODULE}\n"
        f"go test -v -count=1 ./...\n"
    )


def _v2_test_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"{patch_test}\n"
        f"\n"
        f"{_CARGO_ENV}\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod vendor || true\n"
        f"go mod download || true\n"
        f"go mod tidy || true\n"
        f"go test -v -count=1 ./...\n"
    )


def _v2_fix_run(pr: PullRequest) -> str:
    patch_test = _apply_patch_cmd(["/home/test.patch"])
    patch_fix = _apply_patch_cmd(["/home/fix.patch"])
    return (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"\n"
        f"cd /home/influxdb\n"
        f"{patch_test}\n"
        f"{patch_fix}\n"
        f"\n"
        f"{_CARGO_ENV}\n"
        f"{_GO_ENV_MODULE}\n"
        f"\n"
        f"go mod vendor || true\n"
        f"go mod download || true\n"
        f"go mod tidy || true\n"
        f"go test -v -count=1 ./...\n"
    )


# ---------------------------------------------------------------------------
# Script function dispatch tables
# ---------------------------------------------------------------------------

_PREPARE_FN = {
    "influxdb_gdm": _gdm_prepare,
    "influxdb_gomod": _gomod_prepare,
    "influxdb_gomod_rust": _gomod_rust_prepare,
    "influxdb_v2": _v2_prepare,
}

_RUN_FN = {
    "influxdb_gdm": _gdm_run,
    "influxdb_gomod": _gomod_run,
    "influxdb_gomod_rust": _gomod_rust_run,
    "influxdb_v2": _v2_run,
}

_TEST_RUN_FN = {
    "influxdb_gdm": _gdm_test_run,
    "influxdb_gomod": _gomod_test_run,
    "influxdb_gomod_rust": _gomod_rust_test_run,
    "influxdb_v2": _v2_test_run,
}

_FIX_RUN_FN = {
    "influxdb_gdm": _gdm_fix_run,
    "influxdb_gomod": _gomod_fix_run,
    "influxdb_gomod_rust": _gomod_rust_fix_run,
    "influxdb_v2": _v2_fix_run,
}


# ---------------------------------------------------------------------------
# Image classes
# ---------------------------------------------------------------------------

class InfluxDBImageBase(Image):

    def __init__(self, pr: PullRequest, config: Config, go_version: str, strategy: str):
        self._pr = pr
        self._config = config
        self._go_version = go_version
        self._strategy = strategy

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return f"golang:{self._go_version}"

    def image_tag(self) -> str:
        # Include strategy in tag to avoid collision when different strategies
        # share the same Go version (e.g. golang:1.15 for gomod_rust AND v2).
        strategy_suffix = self._strategy.replace("influxdb_", "")
        return f"base-{strategy_suffix}-go{self._go_version.replace('.', '_')}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        go_image = self.dependency()
        is_gdm = self._strategy == "influxdb_gdm"
        needs_rust = self._strategy in ("influxdb_gomod_rust", "influxdb_v2")
        is_v2 = self._strategy == "influxdb_v2"

        if is_gdm:
            # GOPATH layout: clone into /go/src/github.com/influxdata/influxdb
            gopath_dir = _GOPATH_DIR
            if self.config.need_clone:
                clone_code = (
                    f"RUN mkdir -p /go/src/github.com/influxdata && \\\n"
                    f"    git clone https://github.com/{self.pr.org}/{self.pr.repo}.git {gopath_dir} && \\\n"
                    f"    ln -sf {gopath_dir} /home/{self.pr.repo}"
                )
            else:
                clone_code = (
                    f"COPY {self.pr.repo} {gopath_dir}\n"
                    f"RUN ln -sf {gopath_dir} /home/{self.pr.repo}"
                )
        else:
            # Module layout: clone into /home/influxdb (NOT under GOPATH)
            if self.config.need_clone:
                clone_code = (
                    f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
                )
            else:
                clone_code = (
                    f"COPY {self.pr.repo} /home/{self.pr.repo}"
                )

        # gdm depends on golang.org/x/tools/go/vcs, which was removed from x/tools master.
        # Pre-clone a compatible branch so `go get github.com/sparrc/gdm` succeeds.
        gdm_deps_block = (
            "\nRUN mkdir -p /go/src/golang.org/x && "
            "git clone --depth=1 -b release-branch.go1.9 "
            "https://go.googlesource.com/tools /go/src/golang.org/x/tools\n"
        ) if is_gdm else ""

        rust_block = f"\n{_RUST_INSTALL_CMD}\n" if needs_rust else ""
        v2_deps_block = f"\n{_V2_EXTRA_DEPS_CMD}\n" if is_v2 else ""

        env_block = ""
        if is_gdm:
            env_block = (
                "ENV GOPATH=/go\n"
                "ENV GO111MODULE=off\n"
            )
        env_block += (
            "ENV GOTOOLCHAIN=auto\n"
            "ENV GONOSUMCHECK=*\n"
            "ENV GONOSUMDB=*\n"
            "ENV GOPROXY=https://proxy.golang.org\n"
        )

        return f"""FROM {go_image}

{self.global_env}

{_INSTALL_PATCH_CMD}

WORKDIR /home/

{clone_code}
{gdm_deps_block}{rust_block}{v2_deps_block}
{self.clear_env}

{env_block}"""


class InfluxDBImageDefault(Image):

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
        return InfluxDBImageBase(self.pr, self.config, go_version, self._strategy)

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
# Instance classes
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


class _InfluxDBInstanceBase(Instance):

    STRATEGY: str = ""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return InfluxDBImageDefault(self.pr, self._config, self.STRATEGY)

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


# ---------------------------------------------------------------------------
# Backward-compatible instance for PRs without a tag.
# Determines strategy dynamically from base.ref when no tag is provided.
# ---------------------------------------------------------------------------

# NOTE: "master" is intentionally EXCLUDED from this dict because master PRs
# are heterogeneous (some are gdm-era, some are v2-era). Master dispatch is
# handled by _resolve_strategy() heuristic.
_REF_TO_STRATEGY: dict[str, str] = {
    "1.1": "influxdb_gdm",
    "1.2": "influxdb_gdm",
    "1.3": "influxdb_gdm",
    "1.4": "influxdb_gdm",
    "1.5": "influxdb_gdm",
    "1.6": "influxdb_gdm",
    "1.7": "influxdb_gdm",
    "1.8": "influxdb_gomod",
    "1.9": "influxdb_gomod_rust",
    "1.10": "influxdb_gomod_rust",
    "1.11": "influxdb_gomod_rust",
    "1.12": "influxdb_gomod_rust",
    "master-1.x": "influxdb_gomod_rust",
    "2.0": "influxdb_v2",
    "2.7": "influxdb_v2",
}


@Instance.register("influxdata", "influxdb")
class InfluxDB(_InfluxDBInstanceBase):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__(pr, config, *args, **kwargs)
        # Determine strategy from base.ref with edge-case heuristics
        self._dynamic_strategy = _resolve_strategy(pr)

    def dependency(self) -> Image:
        return InfluxDBImageDefault(self.pr, self._config, self._dynamic_strategy)
