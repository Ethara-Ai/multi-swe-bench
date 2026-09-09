import re
from pathlib import Path
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_KIND = {
    2076: "rust",
    4756: "python",
    5827: "python",
    7495: "rust",
    9040: "python",
}

_RUST_TOOLCHAIN = {
    2076: "1.64.0",
    4756: "1.70.0",
    5827: "1.75.0",
    7495: "1.82.0",
    9040: "1.82.0",
}

_PY_VERSION = {
    4756: "3.10",
    5827: "3.10",
    9040: "3.12",
}

_RUST_TARGET = {
    2076: "-p i-slint-compiler",
    7495: "-p slint --features renderer-software,renderer-skia",
}

_PY_WORKDIR = {
    4756: "api/python",
    5827: "api/python",
    9040: "api/python/slint",
}

_RUSTFLAGS = (
    "-A dangerous-implicit-autorefs -A unsafe_op_in_unsafe_fn -A unknown-lints "
    "-A double-negations -A non-local-definitions"
)


_LOCK_DIR = Path(__file__).parent / "locks"


def _pinned_lock(pr) -> Optional[str]:
    """The Cargo.lock the ordinary build already resolved for this PR.

    Resolution against the local git index with MSRV fallback is the most expensive
    step in the image, and Cargo.lock is architecture-independent, so a multi-arch
    build recomputes a byte-identical file on the emulated leg. Measured 2026-09-07 on
    pr-2076: amd64 reached 681 crates in 1057s (whole prepare.sh 1112s) while arm64
    spent 4695s reaching the same 681 crates. Reusing the lock removes ~78 minutes per
    PR from the critical path.

    Returns None when no lock has been captured; prepare.sh then generates one, so the
    config still works on a machine that has never built this dataset.
    """
    f = _LOCK_DIR / f"pr-{int(pr.number)}.lock"
    if f.is_file():
        return f.read_text(encoding="utf-8")
    return None


def _rust_test_cmd(pr) -> str:
    n = int(pr.number)
    target = _RUST_TARGET[n]
    return (
        f'QT_QPA_PLATFORM=offscreen xvfb-run -a env RUSTFLAGS="{_RUSTFLAGS}" '
        f"cargo +stable test "
        f"{target} --no-fail-fast -- --test-threads=1"
    )


_PY_EXTRA_PKGS = {
    9040: "numpy pillow",
}

_PY_PYTEST_FLAGS = {
    4756: "--continue-on-collection-errors",
    9040: "--continue-on-collection-errors",
}


def _py_test_cmd(pr) -> str:
    n = int(pr.number)
    wd = _PY_WORKDIR[n]
    py = _PY_VERSION[n]
    editable = "'.[dev]'"
    _e = _PY_EXTRA_PKGS.get(n, "")
    _extra = (" " + _e) if _e else ""
    _f = _PY_PYTEST_FLAGS.get(n, "")
    _flags = (" " + _f) if _f else ""
    return (
        f"cd /home/{{repo}}/{wd} && "
        f"python{py} -m venv .venv && . .venv/bin/activate && "
        f"pip install --disable-pip-version-check -q maturin pytest{_extra} && "
        f'MATURIN_PEP517_ARGS="--profile=dev" RUSTFLAGS="{_RUSTFLAGS}" '
        f"pip install -q -e {editable} && "
        f"QT_QPA_PLATFORM=offscreen xvfb-run -a python -m pytest -v -rA -s{_flags}"
    )


_LOCK_PINS = {
    9040: [("pyo3-build-config", "0.25.1")],
}


def _test_cmd(pr) -> str:
    if _KIND[int(pr.number)] == "rust":
        return _rust_test_cmd(pr)
    return _py_test_cmd(pr).format(repo=pr.repo)


_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6
FROM __FROM__
__GLOBAL_ENV__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
ENV CARGO_HOME=/opt/cargo
ENV RUSTUP_HOME=/opt/rustup
ENV PATH=/opt/cargo/bin:${PATH}

WORKDIR /home/

# apt over the HTTPS aliyun mirror with a CA bootstrap - this network drops container
# HTTP:80 transfers (proven repeatedly on 2026-09-03/04); HTTPS is reliable. GPG still
# authenticates every package; only the one bootstrap call skips TLS peer verification.
RUN sed -i 's|http://archive.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g; s|http://security.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list 2>/dev/null || true; \\
    apt-get -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false -o Acquire::Retries=5 update && \\
    apt-get -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false -o Acquire::Retries=5 install -y ca-certificates

# Build + GUI + Python stack. slint's install-linux-dependencies action plus the pieces
# its python_test / rust test jobs add: Qt5 (qt backend), Xvfb (headless display),
# fontconfig, xkbcommon, and Skia's build prerequisites (clang, cmake, ninja, python3).
RUN apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\
    git curl wget ca-certificates build-essential pkg-config \\
    clang cmake ninja-build \\
    libavcodec-dev libavformat-dev libavutil-dev libavfilter-dev libavdevice-dev \\
    libasound2-dev \\
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libunwind-dev \\
    libfontconfig1-dev libfreetype6-dev libxcb1-dev libxkbcommon-dev \\
    libx11-dev libxext-dev libxrender-dev libgl1-mesa-dev \\
    qtbase5-dev qtdeclarative5-dev \\
    xvfb xauth \\
    python3 python3-pip python3-venv python3-dev \\
    software-properties-common \\
    && rm -rf /var/lib/apt/lists/*

# Python 3.10 and 3.12 from deadsnakes (the two interpreters the noxfiles pin). The PPA
# is added over HTTPS; if it is unreachable the base still builds - a python PR whose
# interpreter is missing then fails loudly at its prepare gate, not silently.
RUN add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true; \\
    apt-get -o Acquire::Retries=5 update 2>/dev/null || true; \\
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\
        python3.10 python3.10-venv python3.10-dev \\
        python3.12 python3.12-venv python3.12-dev 2>/dev/null || true; \\
    rm -rf /var/lib/apt/lists/*

# rustup, no default toolchain - each PR installs and selects its era toolchain in
# prepare.sh via `cargo +<version>`, so resolution stays era-correct.
# rustup with a MODERN stable toolchain installed alongside the per-PR era toolchains.
# Stable is NOT for compiling - it is for RESOLVING. MSRV-aware dependency resolution
# (which honours each crate's rust-version when picking versions) only exists in cargo
# >= 1.84. An era cargo like 1.75 has no such logic and resolves to the newest crate on
# the index, which on 2026-09-04 dragged wasm-bindgen 0.2.127 / chrono 0.4.45 into a
# 2024 tree and reproduced the `cyclic package dependency: once_cell` failure that
# parked this dataset. So: resolve the lockfile with stable + fallback, compile with the
# era toolchain. See prepare.sh.
RUN curl --retry 5 -fsSL https://sh.rustup.rs -o /tmp/rustup.sh && \\
    sh /tmp/rustup.sh -y --no-modify-path --default-toolchain stable --profile minimal && \\
    rm /tmp/rustup.sh && \\
    chmod -R a+w $CARGO_HOME $RUSTUP_HOME

# Un-yank the crates.io index. A crate yanked AFTER a PR merged blocks resolution of a
# tree that legitimately depended on it; flipping yanked flags in a local index mirror
# restores buildability without changing any crate's contents. Carried from the parked
# config where it was necessary. crates.io-index history is squashed, so this is the
# only lever - date-pinning the index is impossible.
RUN git clone --bare https://github.com/rust-lang/crates.io-index.git /tmp/idx.git && \\
    cd /tmp/idx.git && git clone -q . /opt/crates-index && cd /opt/crates-index && \\
    grep -rl '"yanked":true' . --include="*" | grep -v ".git/" | xargs -r sed -i 's/"yanked":true/"yanked":false/g' && \\
    git -c user.email=f@l -c user.name=f commit -qam f --allow-empty && \\
    rm -rf /tmp/idx.git && \\
    mkdir -p $CARGO_HOME && \\
    printf '[source.crates-io]\\nreplace-with = "filtered"\\n[source.filtered]\\nregistry = "file:///opt/crates-index"\\n[resolver]\\nincompatible-rust-versions = "fallback"\\n' > $CARGO_HOME/config.toml

# Break the once_cell <-> critical-section resolver cycle in the LOCAL index, by
# removing critical-section's `loom` dependency.
#
# The cycle cargo reports (measured 2026-09-04) is:
#   once_cell -> critical-section -> loom -> tracing -> tracing-core -> once_cell
# It fails `cargo generate-lockfile` for every slint PR and is what parked this dataset.
# Neither era-pinning the toolchain (cargo < 1.84 has no MSRV-aware resolution and just
# takes the newest crate) nor stable + `incompatible-rust-versions = "fallback"` avoids
# it, and date-pinning the index is impossible: crates.io-index is squashed, this clone
# holds ~6000 commits spanning ONE day.
#
# WHY loom AND NOT once_cell's critical-section edge: cutting the once_cell end was
# tried first and is WRONG - slint genuinely uses that feature. Proven by cargo:
#   "package `i-slint-core` depends on `once_cell` with feature `critical-section`
#    but `once_cell` does not have that feature"
# Removing it breaks slint's own build. loom is the safe cut, verified in the index:
#   critical-section 1.3.0 deps -> [(loom, normal, optional=False, target='cfg(loom)')]
#   critical-section 1.3.0 features2 -> {}          (NOTHING references loom)
#   critical-section 1.1.3 deps -> []               (older versions have no loom at all)
# loom is `cfg(loom)`-gated, so it is only ever compiled under `--cfg loom` - a
# concurrency-model-checking build slint never performs - and because no feature
# references it, deleting the dep leaves no dangling feature reference (which is exactly
# what made the once_cell attempt produce "index entry is invalid").
#
# Environment-only, like the un-yank above: no crate contents change, no repo file
# changes, applied identically to every stage of every PR. Separate RUN so iterating on
# it never redoes the expensive index clone.
RUN printf '%s\\n' \\
    'import json, io, os' \\
    'p = "/opt/crates-index/cr/it/critical-section"' \\
    'TARGET = "loom"' \\
    'out = []' \\
    'removed = 0' \\
    'for line in io.open(p, encoding="utf-8"):' \\
    '    line = line.strip()' \\
    '    if not line:' \\
    '        continue' \\
    '    d = json.loads(line)' \\
    '    before = len(d.get("deps", []))' \\
    '    d["deps"] = [x for x in d.get("deps", []) if x.get("name") != TARGET]' \\
    '    removed += before - len(d["deps"])' \\
    '    for key in ("features", "features2"):' \\
    '        feats = d.get(key)' \\
    '        if not feats:' \\
    '            continue' \\
    '        d[key] = {k: [v for v in vals if TARGET not in v]' \\
    '                  for k, vals in feats.items() if k != TARGET}' \\
    '    out.append(json.dumps(d, separators=(",", ":")))' \\
    'io.open(p, "w", encoding="utf-8").write(chr(10).join(out) + chr(10))' \\
    'print("critical-section entries:", len(out), "loom deps removed:", removed)' \\
    > /tmp/fix_cs.py && \\
    python3 /tmp/fix_cs.py && \\
    cd /opt/crates-index && \\
    git -c user.email=f@l -c user.name=f commit -qam "drop critical-section cfg(loom) dep" --allow-empty

RUN git clone "${REPO_URL}" /home/__REPO__

CMD ["/bin/bash"]
"""


class SlintImageBase(Image):
    """The ONE base image, tagged by flavour. dockerfile() IS overridden here (unlike a
    normal str-dependency base) because the shared clone must NOT be pinned to any one
    commit - the enhancer would check out BASE_COMMIT. Same reason glide hand-writes its
    base."""

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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-mixed"

    def workdir(self) -> str:
        return "base-mixed"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image if isinstance(image, str) else image.image_full_name()
        return (
            _BASE_DOCKERFILE.replace("__FROM__", name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__GLOBAL_ENV__", self.global_env)
        )


class SlintImageDefault(Image):
    """The thin per-PR layer: patches, scripts, and the prepare.sh fetch+build."""

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
        return SlintImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        n = int(self.pr.number)
        toolchain = _RUST_TOOLCHAIN[n]
        test_cmd = _test_cmd(self.pr)

        generate = """if ! cargo +stable generate-lockfile > /tmp/lock.log 2>&1; then
    echo "=== LOCKFILE RESOLUTION FAILED (stable cargo, MSRV fallback) ==="
    tr -cd '\\11\\12\\15\\40-\\176' < /tmp/lock.log | tail -40
    exit 1
fi"""

        if _pinned_lock(self.pr) is not None:
            gate = f"""if [ -f /home/Cargo.lock.pinned ]; then
    cp /home/Cargo.lock.pinned Cargo.lock
    if cargo +stable fetch --locked > /tmp/lockcheck.log 2>&1; then
        echo "lockfile REUSED (pinned)"
    else
        echo "pinned lockfile rejected by cargo, regenerating"
        tr -cd '\\11\\12\\15\\40-\\176' < /tmp/lockcheck.log | tail -20
        rm -f Cargo.lock
{generate}
    fi
else
{generate}
fi
        echo "lockfile OK ($(grep -c '^name = ' Cargo.lock) crates)\""""
        else:
            gate = f"""{generate}
        echo "lockfile OK ($(grep -c '^name = ' Cargo.lock) crates)\""""

        for pkg, ver in _LOCK_PINS.get(n, []):
            gate += f"""
_PVERS=$(grep -A1 'name = "{pkg}"' Cargo.lock | grep -oE 'version = "[^"]+"' | cut -d'"' -f2 | sort -u || true)
echo "lockfile has {pkg}:" $_PVERS
if [ -z "$_PVERS" ]; then
    echo "=== PIN FAILED: {pkg} absent from Cargo.lock ==="
    exit 1
fi
for _v in $_PVERS; do
    if [ "$_v" = "{ver}" ]; then continue; fi
    if ! cargo +stable update -p {pkg}@$_v --precise {ver} > /tmp/pin.log 2>&1; then
        echo "=== PIN FAILED: {pkg}@$_v -> {ver} ==="
        tr -cd '\\11\\12\\15\\40-\\176' < /tmp/pin.log | tail -20
        exit 1
    fi
    echo "pinned {pkg} $_v -> {ver}"
done
echo "pin step done for {pkg}\""""

        if _KIND[n] == "rust":
            warm = (
                f"{gate}\n"
                f"{test_cmd.replace(' -- --test-threads=1', ' --no-run')} "
                f"> /tmp/warm.log 2>&1 || "
                f"tr -cd '\\11\\12\\15\\40-\\176' < /tmp/warm.log | tail -30"
            )
        else:
            py = _PY_VERSION[n]
            wd = _PY_WORKDIR[n]
            editable = "'.[dev]'"
            warm = (
                f"python{py} --version\n"
                f"{gate}\n"
                f"cd /home/{self.pr.repo}/{wd}\n"
                f"python{py} -m venv .venv\n"
                f". .venv/bin/activate\n"
                f"pip install --disable-pip-version-check -q maturin pytest\n"
                f'if ! MATURIN_PEP517_ARGS="--profile=dev" RUSTFLAGS="{_RUSTFLAGS}" '
                f"pip install -q -e {editable} "
                f"> /tmp/maturin.log 2>&1; then\n"
                f'    echo "=== MATURIN BUILD FAILED ==="\n'
                f"    tr -cd '\\11\\12\\15\\40-\\176' < /tmp/maturin.log | tail -50\n"
                f"    exit 1\n"
                f"fi\n"
                f'echo "maturin build OK ($(python -c "import slint; print(slint.__file__)" 2>/dev/null || echo import-check-skipped))"\n'
                f"deactivate\n"
                f"cd /home/{self.pr.repo}"
            )

        prepare = f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh

# Shared base is one clone; this PR's commit is fetched on demand.
git remote add origin https://github.com/{self.pr.org}/{self.pr.repo}.git 2>/dev/null || true
git fetch --depth=1 origin {self.pr.base.sha} 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

printf 'target/\\n.venv/\\nCargo.lock\\n*.egg-info/\\n__pycache__/\\n*.so\\n*.pyd\\n.pytest_cache/\\nbuild/\\n' >> .git/info/exclude
{warm}

git checkout -- .
bash /home/check_git_changes.sh
"""

        run = f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
{test_cmd}
"""

        test_run = f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
"""

        fix_run = f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
"""

        check = """#!/bin/bash
set -e
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi
echo "check_git_changes: No uncommitted changes"
exit 0
"""

        out = [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

        lock = _pinned_lock(self.pr)
        if lock is not None:
            out.append(File(".", "Cargo.lock.pinned", lock))

        return out

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        copy = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {name}:{tag}

{self.global_env}

{copy}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("slint-ui", "slint")
class Slint(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SlintImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        """Handles BOTH cargo test and pytest output - this dataset mixes Rust and
        Python PRs, so the parser must recognise both formats from the same method."""
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        cargo_re = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)")
        py_summary_re = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)"
        )
        py_verbose_re = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )

        for raw in log.splitlines():
            line = raw.strip()

            m = cargo_re.match(line)
            if m:
                name, status = m.group(1), m.group(2)
                if status == "ok":
                    passed.add(name)
                elif status == "FAILED":
                    failed.add(name)
                else:
                    skipped.add(name)
                continue

            m = py_summary_re.match(line) or py_verbose_re.match(line)
            if m:
                if m.re is py_summary_re:
                    status, name = m.group(1), m.group(2)
                else:
                    name, status = m.group(1), m.group(2)
                if status in ("PASSED", "XPASS"):
                    passed.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed.add(name)
                else:
                    skipped.add(name)

        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
