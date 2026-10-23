import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# =============================================================================
# tauri-apps/tauri - QC-compliant two-stage layout, one shared base per
# number interval (tag = base-<interval>):
#
#   base-73_to_25       (tauri_73_to_25.py)       Go 1.13 + Node 12 + webkit 4.0
#   base-839_to_80      (tauri_839_to_80.py)      Node 12
#   base-1442_to_1442   (tauri_1442_to_1442.py)   rustup + Node 14 + webkit 4.0
#   base-15207_to_1729  (this file)               rustup 1.79/1.85/1.90 +
#                                                 Node 14 + webkit 4.0 & 4.1
#
#   Shared bases never pin / scrub a commit.  The per-PR layer (tag pr-<N>)
#   checks out --detach <sha>, installs deps and runs Image._HARDENING_BLOCK.
#
# This file also holds the helpers used by the three other interval files and
# the "tauri-apps/tauri" router for dataset rows that carry no
# `number_interval` (the router dispatches to the interval instance by number).
# =============================================================================

REPO_URL = "https://github.com/tauri-apps/tauri.git"
NODE_LEGACY_VERSION = "12.22.12"
NODE_MODERN_VERSION = "14.21.3"
GO_VERSION = "1.13.15"
YARN_VERSION = "1.22.22"
RUST_DEFAULT = "1.90.0"
RUST_TOOLCHAINS = ("1.79.0", "1.85.0", RUST_DEFAULT)

# The dataset base.sha of these PRs does not accept test.patch / fix.patch;
# the merge-base of base.sha and the PR head does (verified with git apply).
BASE_SHA_OVERRIDES = {
    25: "7bd6e0ddb1fc76c33f3bfa0f42b9cfb9fd2385bb",
    6173: "eaf0d71779707c69903aba6106322c8908107242",
    6443: "17bf7f1f0fd19207674a09106b2b10ae3952a63f",
    7729: "2acc474ccbf8d0e84e472f10fec4fbe6432c65d6",
    7743: "2acc474ccbf8d0e84e472f10fec4fbe6432c65d6",
    8056: "2acc474ccbf8d0e84e472f10fec4fbe6432c65d6",
    8291: "5f75ebbbbb9ae821d9f2e229ca0110e16f8f0316",
    10201: "e816a46b953092053d72c70feddf48a8c273c80d",
}

# Patches carry "GIT binary patch" hunks without full index for these assets.
BINARY_EXCLUDES = " ".join(
    f"--exclude='*.{ext}'"
    for ext in (
        "png", "jpg", "jpeg", "gif", "ico", "icns", "jar", "msm", "exe",
        "tar.gz", "woff", "woff2", "ttf", "svgz", "zip", "pdf",
    )
)


def tauri_jest(dirs, ignore=True, vm_modules=False, build=False) -> dict:
    return {
        "kind": "jest",
        "dirs": dirs,
        "ignore": ignore,
        "vm_modules": vm_modules,
        "build": build,
    }


def tauri_cargo(
    crates, tauri_features="", toolchain=RUST_DEFAULT, git_pins=(), vendor=()
) -> dict:
    return {
        "kind": "cargo",
        "crates": crates,
        "features": tauri_features,
        "toolchain": toolchain,
        "git_pins": git_pins,
        "vendor": vendor,
    }


# Every cargo_toml 0.11.x / 0.13.x release is yanked on crates.io, so a fresh
# resolve of these bases fails.  The .crate files are still served, so they are
# unpacked under /opt/cargo-vendor (outside the repo) and wired in through
# [patch] tables in $CARGO_HOME/config.toml.  Same for wry `branch = "dev"`
# (PR 5136), pinned to the dev commit at the PR's base date.
#   key -> (patch table, patch key, package, archive url, unpack dir)
VENDOR_SOURCES = {
    "cargo_toml-0.11": (
        "crates-io",
        "cargo_toml_0_11",
        "cargo_toml",
        "https://static.crates.io/crates/cargo_toml/cargo_toml-0.11.8.crate",
        "cargo_toml-0.11.8",
    ),
    "cargo_toml-0.13": (
        "crates-io",
        "cargo_toml_0_13",
        "cargo_toml",
        "https://static.crates.io/crates/cargo_toml/cargo_toml-0.13.3.crate",
        "cargo_toml-0.13.3",
    ),
    "wry-dev": (
        '"https://github.com/tauri-apps/wry"',
        "wry",
        "wry",
        "https://codeload.github.com/tauri-apps/wry/tar.gz/87216c7f01d5f65641422343dd0aa7f08ea61d0d",
        "wry-87216c7f01d5f65641422343dd0aa7f08ea61d0d",
    ),
    # PR 6443 fix.patch moves wry to `branch = "dev"`; HEAD dropped `dox`.
    "wry-dev-0.27": (
        '"https://github.com/tauri-apps/wry"',
        "wry",
        "wry",
        "https://codeload.github.com/tauri-apps/wry/tar.gz/db6618b560b9fbaed7e333bb6bb2463f5196ca17",
        "wry-db6618b560b9fbaed7e333bb6bb2463f5196ca17",
    ),
    # rfd 0.10.1 (only 0.10.x with `^0.10.1`) is yanked; needed by PR 6173 fix.
    "rfd-0.10": (
        "crates-io",
        "rfd_0_10",
        "rfd",
        "https://static.crates.io/crates/rfd/rfd-0.10.1.crate",
        "rfd-0.10.1",
    ),
}

CT11 = ("cargo_toml-0.11",)
CT11_13 = ("cargo_toml-0.11", "cargo_toml-0.13")


TAURI_GO = {"kind": "go"}

PR_PLANS = {
    # base-73_to_25
    25: TAURI_GO,
    55: tauri_jest(["."], ignore=False),
    73: tauri_jest(["cli/tauri.js", "."], ignore=False),
    # base-839_to_80
    80: tauri_jest(["cli/tauri.js"], ignore=False),
    530: tauri_jest(["cli/tauri.js"], build=True),
    703: tauri_jest(["cli/tauri.js"], build=True),
    # base-1442_to_1442
    1442: tauri_jest(["cli/tauri.js"], build=True),
    # base-15207_to_1729
    1925: tauri_jest(["tooling/cli.js"], build=True),
    2635: tauri_jest(["tooling/cli.js"], vm_modules=True, build=True),
    3217: tauri_jest(["tooling/cli.js"], vm_modules=True, build=True),
    3389: tauri_cargo(["tauri"], vendor=CT11),
    3486: tauri_cargo(["tauri"], vendor=CT11),
    4674: tauri_cargo(["tauri", "app-updater"], vendor=CT11_13),
    4862: tauri_cargo(["tauri"], vendor=CT11),
    5136: tauri_cargo(["tauri"], vendor=CT11_13 + ("wry-dev",)),
    6173: tauri_cargo(
        ["tauri", "app-updater", "restart"], vendor=CT11_13 + ("rfd-0.10",)
    ),
    6443: tauri_cargo(["tauri", "app-updater"], vendor=CT11 + ("wry-dev-0.27",)),
    7067: tauri_cargo(["tauri"]),
    7170: tauri_cargo(["tauri"], "test"),
    7214: tauri_cargo(["tauri", "app-updater", "restart"], "test", vendor=CT11),
    7535: tauri_cargo(["tauri"], "test"),
    7729: tauri_cargo(["tauri", "app-updater", "restart"], "test", vendor=CT11),
    7743: tauri_cargo(["tauri", "app-updater", "restart"], "test", vendor=CT11),
    8056: tauri_cargo(["tauri", "app-updater", "restart"], "test", vendor=CT11),
    8291: tauri_cargo(["tauri", "app-updater"], "test", vendor=CT11),
    8723: tauri_cargo(
        ["tauri"],
        "test",
        # schemars >= 0.8.19 removed _private::apply_metadata used by tauri-utils
        git_pins=(
            ("tao", "ae4b693dc0a5d4f556bb9e6dcdbbeb1cfbf8f862"),
            ("schemars@0.8", "0.8.16"),
        ),
    ),
    10201: tauri_cargo(
        ["tauri", "acl-tests", "app-updater", "restart"], "test", "1.79.0"
    ),
    15207: tauri_cargo(["tauri"], "test", "1.85.0"),
}


TAURI_CA_SYMLINKS = """RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"""

TAURI_HEADER_ARGS = f"""ARG TARGETARCH
ARG REPO_URL="{REPO_URL}"
ARG BASE_COMMIT
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt\""""

TAURI_ENV_COMMON = """ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH} \\
    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \\
    CI=true \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    npm_config_python=/usr/bin/python3 \\"""

TAURI_LABELS = """LABEL org.opencontainers.image.title="tauri-apps/tauri" \\
      org.opencontainers.image.description="tauri-apps/tauri Docker image" \\
      org.opencontainers.image.source="https://github.com/tauri-apps/tauri" \\
      org.opencontainers.image.authors="https://www.ethara.ai/\""""

_NODE_INSTALL = """RUN case "${{TARGETARCH}}" in \\
        amd64) NODE_ARCH=x64 ;; \\
        arm64) NODE_ARCH=arm64 ;; \\
        *) echo "unsupported TARGETARCH=${{TARGETARCH}}" && exit 1 ;; \\
    esac && \\
    curl -fsSL "https://nodejs.org/dist/v{node}/node-v{node}-linux-${{NODE_ARCH}}.tar.xz" -o /tmp/node.tar.xz && \\
    mkdir -p /usr/local/node && \\
    tar -xJf /tmp/node.tar.xz -C /usr/local/node --strip-components=1 && \\
    rm -f /tmp/node.tar.xz && \\
    curl -fsSL "https://github.com/yarnpkg/yarn/releases/download/v{yarn}/yarn-v{yarn}.tar.gz" -o /tmp/yarn.tgz && \\
    mkdir -p /usr/local/yarn && \\
    tar -xzf /tmp/yarn.tgz -C /usr/local/yarn --strip-components=1 && \\
    rm -f /tmp/yarn.tgz && \\
    ln -sf /usr/local/yarn/bin/yarn /usr/local/node/bin/yarn && \\
    ln -sf /usr/local/yarn/bin/yarnpkg /usr/local/node/bin/yarnpkg && \\
    node --version && yarn --version"""


def tauri_node_install(node_version: str) -> str:
    return _NODE_INSTALL.format(node=node_version, yarn=YARN_VERSION)


TAURI_GO_INSTALL = f"""RUN case "${{TARGETARCH}}" in \\
        amd64|arm64) GO_ARCH="${{TARGETARCH}}" ;; \\
        *) echo "unsupported TARGETARCH=${{TARGETARCH}}" && exit 1 ;; \\
    esac && \\
    curl -fsSL "https://dl.google.com/go/go{GO_VERSION}.linux-${{GO_ARCH}}.tar.gz" -o /tmp/go.tgz && \\
    tar -xzf /tmp/go.tgz -C /usr/local && \\
    rm -f /tmp/go.tgz && \\
    go version"""

TAURI_RUST_ENV = f"""    RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    RUSTUP_TOOLCHAIN={RUST_DEFAULT} \\
    CARGO_TERM_COLOR=never \\
    CARGO_INCREMENTAL=0 \\
    CARGO_NET_RETRY=10 \\
    CARGO_HTTP_TIMEOUT=600 \\
    CARGO_NET_GIT_FETCH_WITH_CLI=true \\
    PATH=/usr/local/cargo/bin:/usr/local/node/bin:${{PATH}}"""


def tauri_rust_install(toolchains=(RUST_DEFAULT,)) -> str:
    extra = " ".join(t for t in toolchains if t != RUST_DEFAULT)
    install_extra = (
        f" && \\\n    rustup toolchain install {extra} --profile minimal" if extra else ""
    )
    return f"""RUN case "${{TARGETARCH}}" in \\
        amd64) RUST_TRIPLE=x86_64-unknown-linux-gnu ;; \\
        arm64) RUST_TRIPLE=aarch64-unknown-linux-gnu ;; \\
        *) echo "unsupported TARGETARCH=${{TARGETARCH}}" && exit 1 ;; \\
    esac && \\
    curl -fsSL "https://static.rust-lang.org/rustup/dist/${{RUST_TRIPLE}}/rustup-init" -o /tmp/rustup-init && \\
    chmod +x /tmp/rustup-init && \\
    /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain {RUST_DEFAULT} && \\
    rm -f /tmp/rustup-init{install_extra} && \\
    rustc --version && cargo --version"""


class TauriIntervalBase(Image):
    """Shared toolchain base; subclasses set TAG and the toolchain pieces."""

    TAG = ""

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
        return self.TAG

    def workdir(self) -> str:
        return self.TAG

    def files(self) -> list[File]:
        return []

    def extra_env(self) -> str:
        raise NotImplementedError

    def toolchain(self) -> str:
        raise NotImplementedError

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}
{TAURI_HEADER_ARGS}
{TAURI_ENV_COMMON}
{self.extra_env()}
{TAURI_LABELS}
{self.global_env}
{TAURI_CA_SYMLINKS}
{self.toolchain()}
RUN git config --global --add safe.directory '*'
WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
{self.clear_env}
CMD ["/bin/bash"]
"""


class ImageBase15207to1729(TauriIntervalBase):
    """rustup (1.79 / 1.85 / 1.90) + Node 14 + webkit2gtk 4.0 & 4.1 + xvfb."""

    TAG = "base-15207_to_1729"

    def extra_env(self) -> str:
        return TAURI_RUST_ENV

    def toolchain(self) -> str:
        return f"""RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl xz-utils file build-essential pkg-config python3 \\
        libssl-dev libgtk-3-dev libwebkit2gtk-4.0-dev libwebkit2gtk-4.1-dev \\
        libayatana-appindicator3-dev librsvg2-dev patchelf libxdo-dev \\
        libvips-dev xvfb xauth \\
    && rm -rf /var/lib/apt/lists/*
{tauri_node_install(NODE_MODERN_VERSION)}
{tauri_rust_install(RUST_TOOLCHAINS)}"""


TAURI_CHECK_GIT_CHANGES = """#!/bin/bash
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

_YARN_INSTALL_FN = """js_install() {
  # optipng-bin's bundled libpng has no NEON objects on arm64
  export CFLAGS="-O2 -DPNG_ARM_NEON_OPT=0" CPPFLAGS="-DPNG_ARM_NEON_OPT=0"
  # Node 14 x64 JIT segfaults under qemu during multi-arch builds
  local saved_node_options="${NODE_OPTIONS:-}"
  if [ "$(uname -m)" = "x86_64" ]; then
    export NODE_OPTIONS="${saved_node_options} --jitless"
  fi
  local extra_flags=""
  for attempt in 1 2 3 4 5; do
    if yarn install --pure-lockfile --ignore-engines --non-interactive --network-timeout 600000 ${extra_flags}; then
      export NODE_OPTIONS="${saved_node_options}"
      return 0
    fi
    echo "yarn install failed (attempt ${attempt}/5)"
    # Native install scripts (sharp, optipng) still segfault under qemu
    # even with --jitless; skip them on x86_64 after the first failure.
    if [ "$(uname -m)" = "x86_64" ]; then
      extra_flags="--ignore-scripts"
    fi
    rm -rf node_modules
    sleep $((attempt * 15))
  done
  export NODE_OPTIONS="${saved_node_options}"
  return 1
}

js_dir() {
  for d in %s; do
    if [ -f "/home/%s/${d}/package.json" ]; then
      echo "/home/%s/${d}"
      return 0
    fi
  done
  echo "no package.json in any of: %s" >&2
  return 1
}
"""

# tauri.h #undefs WEBVIEW_IMPLEMENTATION, so branch before including it.
# tauri-gtk.c itself includes tauri-gtk-webview.h and tauri.h.
_GO_SHIM = """#ifdef WEBVIEW_IMPLEMENTATION
#include "tauri-gtk.c"
#else
#include "tauri-gtk-webview.h"
#include "tauri.h"
#endif
"""


class TauriIntervalImageDefault(Image):
    """Per-PR layer; subclasses set BASE_CLASS and DEFAULT_PLAN."""

    BASE_CLASS = TauriIntervalBase
    DEFAULT_PLAN: dict = tauri_cargo(["tauri"])

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    @property
    def plan(self) -> dict:
        return PR_PLANS.get(self.pr.number, self.DEFAULT_PLAN)

    @property
    def base_sha(self) -> str:
        return BASE_SHA_OVERRIDES.get(self.pr.number, self.pr.base.sha)

    def dependency(self) -> Image:
        return self.BASE_CLASS(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _install_block(self) -> str:
        plan = self.plan
        repo = self.pr.repo
        kind = plan["kind"]
        if kind == "go":
            return """go version
pkg-config --exists gtk+-3.0 webkit2gtk-4.0"""
        if kind == "jest":
            dirs = " ".join(plan["dirs"])
            fn = _YARN_INSTALL_FN % (dirs, repo, repo, dirs)
            return f"""{fn}
JS_DIR="$(js_dir)"
cd "${{JS_DIR}}"
js_install
test -x node_modules/.bin/jest
node_modules/.bin/jest --version"""
        pins = "\n".join(
            f"cargo update -p {name} --precise {rev}"
            for name, rev in plan["git_pins"]
        )
        if pins:
            pins += "\ncargo fetch"
        vendor = ""
        if plan["vendor"]:
            fetch_lines = []
            patch_tables: dict = {}
            for key in plan["vendor"]:
                table, patch_key, package, url, unpack = VENDOR_SOURCES[key]
                fetch_lines.append(
                    f'curl -fsSL --retry 5 "{url}" | tar -xz -C /opt/cargo-vendor'
                )
                patch_tables.setdefault(table, []).append(
                    f'{patch_key} = {{ path = "/opt/cargo-vendor/{unpack}", package = "{package}" }}'
                )
            config = "\n".join(
                f"[patch.{table}]\n" + "\n".join(entries)
                for table, entries in patch_tables.items()
            )
            vendor = (
                "mkdir -p /opt/cargo-vendor\n"
                + "\n".join(fetch_lines)
                + f"\ncat >> \"${{CARGO_HOME}}/config.toml\" <<'EOF'\n{config}\nEOF\n"
            )
        return f"""export RUSTUP_TOOLCHAIN={plan["toolchain"]}
cargo --version
{vendor}fetch_ok=0
for attempt in 1 2 3 4 5; do
  if [ ! -f Cargo.lock ]; then
    cargo generate-lockfile || {{ sleep $((attempt * 15)); continue; }}
  fi
  if cargo fetch; then
    fetch_ok=1
    break
  fi
  echo "cargo fetch failed (attempt ${{attempt}}/5)"
  sleep $((attempt * 15))
done
test "${{fetch_ok}}" = 1
{pins}
cargo metadata --format-version 1 --offline > /dev/null
# keep a tracked Cargo.lock pristine so test.patch / fix.patch still apply
if git ls-files --error-unmatch Cargo.lock > /dev/null 2>&1; then
  git checkout -- Cargo.lock
fi"""

    def _test_block(self) -> str:
        plan = self.plan
        repo = self.pr.repo
        kind = plan["kind"]
        if kind == "go":
            return f"""mkdir -p /tmp/cgo-shim
cat > /tmp/cgo-shim/webview.h <<'EOF'
{_GO_SHIM}EOF
if [ ! -d bindings/go ]; then
  echo "bindings/go does not exist at this revision"
  exit 1
fi
cd bindings/go
export GO111MODULE=off CGO_ENABLED=1
export CGO_CFLAGS="-I/tmp/cgo-shim -I/home/{repo}/ui"
xvfb-run -a go test -v ."""
        if kind == "jest":
            dirs = " ".join(plan["dirs"])
            fn = _YARN_INSTALL_FN % (dirs, repo, repo, dirs)
            args = "--ci --verbose --runInBand --no-cache --forceExit"
            if plan["ignore"]:
                args += ' --testPathIgnorePatterns="(build|dev)"'
            node_opts = "--max-old-space-size=4096"
            if plan["vm_modules"]:
                node_opts += " --experimental-vm-modules"
            build = ""
            if plan["build"]:
                build = """if node -e "process.exit(((require('./package.json').scripts)||{}).build ? 0 : 1)"; then
  yarn run build
fi
"""
            return f"""{fn}
JS_DIR="$(js_dir)"
cd "${{JS_DIR}}"
if [ -n "$(git status --porcelain -- package.json yarn.lock)" ]; then
  # a changed lockfile over old node_modules leaves stale babel files
  rm -rf node_modules
fi
if [ ! -d node_modules ]; then
  js_install
fi
{build}export NODE_OPTIONS="{node_opts}"
xvfb-run -a node_modules/.bin/jest {args}"""
        lines = [f"export RUSTUP_TOOLCHAIN={plan['toolchain']}", "rc=0"]
        for crate in plan["crates"]:
            feat = ""
            if crate == "tauri" and plan["features"]:
                feat = f" --features {plan['features']}"
            lines.append(
                f"xvfb-run -a cargo test -p {crate}{feat} --tests --no-fail-fast || rc=$?"
            )
        lines.append("exit ${rc}")
        return "\n".join(lines)

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.base_sha
        prepare = f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
bash /home/check_git_changes.sh

{self._install_block()}
"""
        test_sh = f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{self._test_block()}
"""
        run_sh = f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
bash /home/test.sh
"""
        test_run = f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn {BINARY_EXCLUDES} /home/test.patch
bash /home/test.sh
"""
        fix_run = f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn {BINARY_EXCLUDES} /home/test.patch /home/fix.patch
bash /home/test.sh
"""
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", TAURI_CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "test.sh", test_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return f"""FROM {dep.image_name()}:{dep.image_tag()}
{self.global_env}
ARG BASE_COMMIT="{self.base_sha}"
ENV BASE_COMMIT={self.base_sha}
{copy_commands}
RUN bash /home/prepare.sh
WORKDIR /home/{self.pr.repo}
{Image._HARDENING_BLOCK}
{self.clear_env}
"""


class TauriIntervalInstance(Instance):
    """Shared Instance behaviour; subclasses set IMAGE_DEFAULT."""

    IMAGE_DEFAULT = TauriIntervalImageDefault

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return self.IMAGE_DEFAULT(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # cargo test: "test path::name ... ok|FAILED|ignored[, reason]"
        cargo_re = re.compile(r"^test (\S.*?) \.\.\. (ok|FAILED|ignored)\b")
        # go test -v: "--- PASS: TestX (0.00s)" (subtests are indented)
        go_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")

        # jest --verbose
        suite_re = re.compile(
            r"^\s*(PASS|FAIL)\s+(\S+\.(?:js|jsx|ts|tsx|mjs))(?:\s+\(.*\))?\s*$"
        )
        test_re = re.compile(r"^(\s+)(\u2713|\u2714|\u2715|\u2716|\u00d7|\u25cb)\s+(.+?)\s*$")
        describe_re = re.compile(r"^( {2,})(\S.*?)\s*$")
        duration_re = re.compile(r"\s+\(\d+(?:\.\d+)?\s*m?s\)$")
        noise_re = re.compile(
            r"^(\u25cf|console\.|at |Test Suites:|Tests:|Snapshots:|Time:|Ran all|"
            r"expect|Expected|Received|[-+@<>{}\[\]])"
        )

        suite: Optional[str] = None
        stack: list[tuple[int, str]] = []
        seen: dict[str, int] = {}
        load_failed = False

        for raw in log.splitlines():
            line = raw.rstrip("\r")

            m = cargo_re.match(line)
            if m:
                name, status = m.group(1).strip(), m.group(2)
                if status == "ok":
                    passed_tests.add(name)
                elif status == "FAILED":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            m = go_re.match(line)
            if m:
                status, name = m.group(1), m.group(2)
                if status == "PASS":
                    passed_tests.add(name)
                elif status == "FAIL":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            m = suite_re.match(line)
            if m:
                suite = m.group(2)
                stack = []
                seen = {}
                load_failed = False
                continue
            if suite is None:
                continue
            stripped = line.strip()
            if stripped.startswith("Test Suites:"):
                suite = None
                continue
            if stripped.startswith("\u25cf Test suite failed to run"):
                load_failed = True
                failed_tests.add(suite)
                continue

            m = test_re.match(line)
            if m:
                indent = len(m.group(1))
                mark = m.group(2)
                title = duration_re.sub("", m.group(3)).strip()
                if mark == "\u25cb" and re.match(r"^skipped \d+ tests?$", title):
                    continue
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                name = " > ".join([suite] + [d for _, d in stack] + [title])
                count = seen.get(name, 0) + 1
                seen[name] = count
                if count > 1:
                    name = f"{name} #{count}"
                if mark in ("\u2713", "\u2714"):
                    passed_tests.add(name)
                elif mark == "\u25cb":
                    skipped_tests.add(name)
                else:
                    failed_tests.add(name)
                continue

            m = describe_re.match(line)
            if m and not load_failed and not noise_re.match(stripped):
                indent = len(m.group(1))
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, m.group(2)))

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


class ImageDefault15207to1729(TauriIntervalImageDefault):
    BASE_CLASS = ImageBase15207to1729
    DEFAULT_PLAN = tauri_cargo(["tauri"])


@Instance.register("tauri-apps", "tauri_15207_to_1729")
class Tauri15207to1729(TauriIntervalInstance):
    IMAGE_DEFAULT = ImageDefault15207to1729


def tauri_interval_for(number: int) -> str:
    if number <= 73:
        return "tauri_73_to_25"
    if number <= 839:
        return "tauri_839_to_80"
    if number < 1729:
        return "tauri_1442_to_1442"
    return "tauri_15207_to_1729"


@Instance.register("tauri-apps", "tauri")
def TauriRouter(pr: PullRequest, config: Config, *args, **kwargs) -> Instance:
    """Rows without `number_interval` -> the interval instance for pr.number."""
    name = f"tauri-apps/{tauri_interval_for(pr.number)}"
    return Instance._registry[name](pr, config, *args, **kwargs)
