"""servo/servo — era `servo_10189_to_6188`: PRs #6188-#10189 (2015-05 - 2016-04).

Kept apart from `servo_13860_to_12637` for two reasons. No aarch64 nightly exists
for the rustc revisions these commits pin — 2015-12-09 and 2016-04-06 both 404 for
aarch64-unknown-linux-gnu on static.rust-lang.org — so this era is amd64 only and
must be run with PLATFORM=linux/amd64. And #6188 still uses the retired
`rust-snapshot-hash` pin scheme rather than `rust-nightly-date`.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "servo_10189_to_6188"

# `mach test-unit` of this era runs `cargo test -p <crate>` from this directory:
# it is the crate that dev-depends on every tests/unit/* crate. Running cargo
# from the repo root does not work — tests/unit/* are not workspace members.
_CARGO_DIR = "components/servo"

# servo pins an exact rustc per commit, in-tree, so prepare.sh reads the pin
# after checkout. This map covers only the commits whose pinned artifact is no
# longer distributed anywhere.
_TOOLCHAIN_FALLBACK = {
    # #6188 pins `rust-snapshot-hash` 474c6e0a/rustc-1.2.0-dev, a servo-hosted
    # snapshot whose bucket now 404s; nightly-2015-06-01 is the upstream nightly
    # from the same window.
    6188: "nightly-2015-06-01",
}
_TOOLCHAIN_DEFAULT = "nightly-2016-04-06"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

_FREEZE_INDEX_PY = r'''#!/usr/bin/env python3
"""Build a crates.io index containing only the versions this commit locks.

cargo 0.13 cannot parse today's index: entries such as `log` now declare
`serde` as both a feature and an optional dependency, which this cargo rejects
with "Features and dependencies cannot have the same name". The versions the
lockfile pins still carry their original 2016 metadata, so an index holding
only those parses cleanly. git's insteadOf rewrite points cargo at it while
keeping the source id as crates.io, so Cargo.lock stays valid.
"""
import concurrent.futures, json, os, re, shutil, subprocess, sys, time, urllib.request

REPO = sys.argv[1] if len(sys.argv) > 1 else "/home/servo"
OUT = "/opt/crates-index"
CRATES_IO = "https://github.com/rust-lang/crates.io-index"

want = {}
for root, dirs, files in os.walk(REPO):
    dirs[:] = [d for d in dirs if d not in (".git", "target")]
    if "Cargo.lock" not in files:
        continue
    for blk in open(os.path.join(root, "Cargo.lock"), encoding="utf-8", errors="replace").read().split("[[package]]"):
        n = re.search(r'^\s*name\s*=\s*"([^"]+)"', blk, re.M)
        v = re.search(r'^\s*version\s*=\s*"([^"]+)"', blk, re.M)
        src = re.search(r'^\s*source\s*=\s*"([^"]+)"', blk, re.M)
        if n and v and src and src.group(1).startswith("registry+"):
            want.setdefault(n.group(1), set()).add(v.group(1))
# The test/fix patches can edit Cargo.lock and pin versions the base lock never
# had; without them the fix stage dies with "version required: = X / versions
# found: Y" because the frozen index only carries the base commit's versions.
# Two shapes matter. A bumped [[package]] shows `name` as an unchanged context
# line with only `+version = "..."` added, so track the enclosing name as we go.
# Dependency entries carry both at once: `+ "crate 1.2.3 (registry+...)"`.
for _patch in ("/home/test.patch", "/home/fix.patch"):
    try:
        _text = open(_patch, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    _name = None
    for _line in _text.splitlines():
        _body = _line[1:] if _line[:1] in "+- " else _line
        _m = re.match(r'\s*name = "([^"]+)"', _body)
        if _m:
            _name = _m.group(1)
            continue
        if not _line.startswith("+"):
            continue
        _m = re.match(r'\s*version = "([^"]+)"', _body)
        if _m and _name:
            want.setdefault(_name, set()).add(_m.group(1))
            continue
        _m = re.match(r'\s*"([A-Za-z0-9_.\-]+) ([^ "]+) \(registry\+', _body)
        if _m:
            want.setdefault(_m.group(1), set()).add(_m.group(2))

print("locked registry crates: %d" % len(want))


def shard(name):
    n = name.lower()
    if len(n) == 1:
        return "1/" + n
    if len(n) == 2:
        return "2/" + n
    if len(n) == 3:
        return "3/%s/%s" % (n[0], n)
    return "%s/%s/%s" % (n[:2], n[2:4], n)


def fetch(name):
    for attempt in range(5):
        try:
            req = urllib.request.Request("https://index.crates.io/" + shard(name),
                                         headers={"User-Agent": "cargo-index-freeze"})
            return name, urllib.request.urlopen(req, timeout=60).read().decode()
        except Exception:
            if attempt == 4:
                return name, None
            time.sleep(2 * (attempt + 1))
    return name, None


shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)
with open(os.path.join(OUT, "config.json"), "w") as f:
    json.dump({"dl": "https://static.crates.io/crates", "api": "https://crates.io"}, f)
    f.write("\n")

kept, missing = 0, []
with concurrent.futures.ThreadPoolExecutor(6) as ex:
    for name, body in ex.map(fetch, sorted(want)):
        if not body:
            missing.append(name)
            continue
        lines = [ln for ln in body.splitlines()
                 if ln.strip() and json.loads(ln).get("vers") in want[name]]
        if not lines:
            missing.append(name)
            continue
        path = os.path.join(OUT, shard(name))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        kept += len(lines)
print("index entries written: %d; missing: %s" % (kept, missing))
# An empty or badly incomplete index means every fetch failed (crates.io rate
# limiting is the usual cause). Carrying on would hand cargo an index that can
# resolve nothing, so fail loudly instead of poisoning the image.
if kept == 0 or len(missing) > max(5, len(want) // 10):
    raise SystemExit(
        "freeze_index: only %d of %d crates resolved (%d missing) -- refusing to "
        "write a useless index" % (len(want) - len(missing), len(want), len(missing))
    )

subprocess.check_call(["git", "init", "-q", OUT])
subprocess.check_call(["git", "-C", OUT, "add", "-A"])
subprocess.check_call(["git", "-C", OUT, "-c", "user.email=build@local",
                       "-c", "user.name=build", "commit", "-qm", "frozen index"])
# cargo asks libgit2 for the crates.io URL; send it here instead.
subprocess.check_call(["git", "config", "--global",
                       "url.file://%s.insteadOf" % OUT, CRATES_IO])
print("index frozen at %s and rewired from %s" % (OUT, CRATES_IO))
'''

_PREPARE_SH = """#!/bin/bash
set -e

REPO_DIR=/home/{pr.repo}
BASE_SHA={pr.base.sha}

cd "$REPO_DIR"

# The era base image is shared by every PR in this interval, so its clone sits at
# whatever HEAD was current. Recover this PR's base commit if the clone predates
# it (or upstream deleted the branch it lived on: GitHub still serves such a
# commit by SHA and via refs/pull/<N>/head), then check it out. The Dockerfile's
# hardening block runs after this and asserts HEAD is exactly this commit.
if ! git cat-file -e "$BASE_SHA^{{commit}}" 2>/dev/null; then
    git fetch --no-tags --depth=2147483647 origin "$BASE_SHA" \
        || git fetch --no-tags origin "+refs/pull/{pr.number}/head:refs/remotes/origin/pr-{pr.number}"
fi

git reset --hard
git checkout --detach "$BASE_SHA"
test "$(git rev-parse HEAD)" = "$BASE_SHA"
bash /home/check_git_changes.sh

# 2015-era servo carries path dependencies as git submodules (support/android-rs-glue).
# The Dockerfile's `submodule foreach` only visits already-initialised submodules, so
# without this cargo dies with "Could not find `Cargo.toml` in .../glue".
if [ -f .gitmodules ]; then
    git submodule update --init --recursive || true
fi

# Resolve the rustc this commit pins. `rust-nightly-date` is authoritative when
# present; commits that instead pin `rust-commit-hash` name a rustc CI build no
# longer hosted, and fall back to the nightly cut from that same revision.
if [ -f rust-toolchain.toml ] || [ -f rust-toolchain ]; then
    TOOLCHAIN=""
elif [ -f rust-nightly-date ]; then
    TOOLCHAIN="nightly-$(tr -d '[:space:]' < rust-nightly-date)"
else
    TOOLCHAIN="{toolchain}"
fi

if [ -n "$TOOLCHAIN" ]; then
    rustup toolchain install "$TOOLCHAIN" --profile minimal || true
    rustup default "$TOOLCHAIN" || true
else
    rustup show || true
fi

# Nightlies cut between roughly 2016-11 and 2017-01-19 list their cargo component
# on s3.amazonaws.com/rust-lang-ci, a bucket that has since been pruned: rustup
# installs rustc, 404s on cargo, and leaves an unusable toolchain. The combined
# standalone tarball for the very same date is still served and carries both, so
# fall back to it rather than drifting to a different rustc.
# Two independent ways the pinned nightly can fail to yield a usable cargo:
#  1. Nightlies cut between roughly 2016-11 and 2017-01-19 list their cargo
#     component on s3.amazonaws.com/rust-lang-ci, a bucket since pruned, so
#     rustup installs rustc, 404s on cargo, and leaves the toolchain unusable.
#     The combined standalone tarball for the same date still carries both.
#  2. Some 2017 nightlies ship a cargo that panics in cargo::version() on any
#     registry access ("called `Option::unwrap()` on a `None` value").
# Try the exact pin first, then the standalone tarball, then walk forward a few
# days -- rustc drift of a day or two is far less damaging than having no cargo.
if [ -n "$TOOLCHAIN" ] && ! cargo --version >/dev/null 2>&1; then
    tc_date="${{TOOLCHAIN#nightly-}}"
    tc_url="https://static.rust-lang.org/dist/$tc_date/rust-nightly-$(uname -m)-unknown-linux-gnu.tar.gz"
    echo "prepare.sh: no usable cargo for $TOOLCHAIN; trying standalone $tc_url"
    if curl -fsSL "$tc_url" -o /tmp/rust.tgz; then
        mkdir -p /tmp/rustdist
        tar xzf /tmp/rust.tgz -C /tmp/rustdist --strip-components=1
        /tmp/rustdist/install.sh --prefix=/usr/local --disable-ldconfig >/dev/null 2>&1
        rm -f /usr/local/cargo/bin/cargo /usr/local/cargo/bin/rustc
        hash -r
        rm -rf /tmp/rust.tgz /tmp/rustdist
    fi
fi

if [ -n "$TOOLCHAIN" ] && ! cargo --version >/dev/null 2>&1; then
    for offset in 1 2 3 4 5 6 7 8 9 10; do
        alt="$(date -u -d "${{TOOLCHAIN#nightly-}} +$offset day" +%Y-%m-%d 2>/dev/null)"
        [ -n "$alt" ] || break
        echo "prepare.sh: cargo still unusable; trying nightly-$alt"
        rustup toolchain install "nightly-$alt" --profile minimal >/dev/null 2>&1 || continue
        rustup default "nightly-$alt" >/dev/null 2>&1 || continue
        # Test the toolchain's own binaries: an earlier standalone fallback may
        # have put a broken cargo on PATH, which would mask every candidate.
        tc_bin="$RUSTUP_HOME/toolchains/nightly-$alt-$(uname -m)-unknown-linux-gnu/bin"
        if "$tc_bin/cargo" --version >/dev/null 2>&1; then
            rm -f /usr/local/bin/cargo /usr/local/bin/rustc
            ln -sf "$tc_bin/cargo" /usr/local/bin/cargo
            ln -sf "$tc_bin/rustc" /usr/local/bin/rustc
            hash -r
            echo "prepare.sh: using nightly-$alt (pinned $TOOLCHAIN had no working cargo)"
            break
        fi
    done
fi

rustc --version || true
cargo --version || true

# The style crate expands .mako.rs templates through python2 at build time.
# Mako comes from apt (python-mako): this pip is 8.1.1 and silently builds a
# broken "unknown-0.0.0" package from a modern Mako sdist instead of failing.
python -c "import mako" || pip install --no-cache-dir "mako<1.1" || true

# Pin the crates.io index to the versions this commit locks; see freeze_index.py.
python3 /home/freeze_index.py "$REPO_DIR"

# servo reads this at compile time via env!("GIT_INFO"); `mach` normally supplies
# it from `git describe`, and the history here is stripped, so define it empty.
export GIT_INFO=""

# Pre-build the target crates into the image. Each of the three test stages runs
# in a fresh container off this image, so without a warm target/ directory every
# stage would recompile the whole dependency graph from scratch.
cd "$REPO_DIR/{cargo_dir}"
# `cargo fetch` is an optional warm-up -- the prebuild below fetches whatever it
# still needs -- so a transient failure here is tolerated. That is only safe
# because the hard gate at the end of this script asserts the real result.
cargo fetch || true

# Retried: crates.io intermittently answers a .crate download with a 500, and
# cargo 0.13 has no retry of its own. Once this succeeds the crate files live in
# CARGO_HOME inside the image, so the three test stages never hit the network.
# Exhausting the retries is FATAL: a swallowed prebuild failure ships an image
# that looks built, then yields an empty test report the harness reads as
# "0 failures" rather than "broken image".
for crate in {crates}; do
    # Same feature rule the run scripts use; see the comment there.
    FEAT=""
    if [ "$crate" = "style_tests" ] && grep -qE '^testing *=' Cargo.toml; then
        FEAT="--features testing"
    fi
    prebuilt=0
    for attempt in 1 2 3 4 5; do
        if cargo test -p "$crate" $FEAT --no-run; then prebuilt=1; break; fi
        echo "prepare.sh: prebuild attempt $attempt for $crate failed, retrying"
        sleep 20
    done
    if [ "$prebuilt" -ne 1 ]; then
        echo "prepare.sh: FATAL - prebuild for $crate failed after 5 attempts"
        exit 1
    fi
done

# Hard gate. Non-tolerant and last: re-assert that every graded crate still
# builds, so a partial install aborts the build here rather than surfacing three
# stages later as an empty report. No `|| true` on this line by design.
for crate in {crates}; do
    FEAT=""
    if [ "$crate" = "style_tests" ] && grep -qE '^testing *=' Cargo.toml; then
        FEAT="--features testing"
    fi
    cargo test -p "$crate" $FEAT --no-run
done
echo DEPS_OK
"""

# One body for all three stages. They must differ only in which patches are
# applied — a different test command per stage makes the f2p comparison
# meaningless because it compares two different suites.
_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1
export PATH=/usr/local/cargo/bin:$PATH
# read at compile time by env!("GIT_INFO") in components/util
export GIT_INFO=""

cd /home/{repo}
{patch_line}
# Crates the patches actually touch. Restricting to these is what makes an f2p
# measurable here at all: a full tests/unit sweep drags in script_tests, and
# with it SpiderMonkey, for PRs whose patch never touches it. Falls back to the
# same discovery `mach test-unit` does when the patch names no test crate.
CRATES="{crates}"
if [ -z "$CRATES" ]; then
    for d in tests/unit/*/; do
        name="$(basename "$d")"
        [ "$name" = "stylo" ] && continue
        [ -f "$d/Cargo.toml" ] || continue
        CRATES="$CRATES ${{name}}_tests"
    done
fi

cd /home/{repo}/{cargo_dir}
for crate in $CRATES; do
    # parse_log keys every test name on the crate that produced it: cargo does
    # not print the package name, and module paths repeat across crates.
    echo "===CRATE=== $crate"
    # `mach test-unit` runs style separately as
    #   cargo test -p style_tests --features testing
    # In cargo 0.13 --features applies to the manifest in the CWD, not to -p, so
    # the flag turns on `testing` for this directory's package, which cascades to
    # style/testing. Without it the gecko-only longhands (mask_*, text_emphasis_*,
    # font_feature_settings) are never generated and style_tests fails to compile.
    FEAT=""
    if [ "$crate" = "style_tests" ] && grep -qE '^testing *=' Cargo.toml; then
        FEAT="--features testing"
    fi
    if ! cargo test -p "$crate" $FEAT; then
        echo "===CRATE_STATUS=== $crate nonzero"
    fi
done
"""


def _target_crates(pr: PullRequest) -> str:
    """tests/unit/<dir>/... in either patch -> `<dir>_tests`, the cargo package."""
    seen = []
    for patch in (pr.test_patch, pr.fix_patch):
        for d in re.findall(r"^diff --git a/tests/unit/([^/]+)/", patch or "", re.M):
            if d != "stylo" and d not in seen:
                seen.append(d)
    return " ".join(f"{d}_tests" for d in sorted(seen))


def _toolchain_fallback(pr: PullRequest) -> str:
    return _TOOLCHAIN_FALLBACK.get(pr.number, _TOOLCHAIN_DEFAULT)


class ServoImageBase(Image):
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
        # Ubuntu 16.04 is what servo targeted across this era: glibc 2.23,
        # OpenSSL 1.0.2 via `libssl-dev`, gcc 5, python2 — what the 2015-2016
        # rustc these commits pin, and their C dependencies, expect. A current
        # `rust:*` image is bookworm with OpenSSL 3 and cannot build them.
        return "ubuntu:16.04"

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            # ubuntu:16.04 ships a GnuTLS-backed git that intermittently drops a
            # clone this size ("RPC failed; curl 56"), so retry rather than lose
            # the whole image build to one flaky transfer.
            code = (
                f'RUN git config --global http.postBuffer 524288000 \\\n'
                f'    && for i in 1 2 3 4 5; do \\\n'
                f'        git clone "${{REPO_URL}}" /home/{self.pr.repo} && break; \\\n'
                f'        echo "clone attempt $i failed, retrying"; \\\n'
                f'        rm -rf /home/{self.pr.repo}; sleep 10; \\\n'
                f'    done \\\n'
                f'    && git -C /home/{self.pr.repo} rev-parse HEAD > /dev/null'
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        # The syntax directive makes DockerfileEnhancer.enhance() return this
        # file verbatim, so the infrastructure block below is written out here
        # rather than injected. Base image = clone only: the per-PR image is
        # what checks out a commit and strips history.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    PATH=/usr/local/cargo/bin:$PATH
{global_block}
LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

# servo's own documented Debian/Ubuntu dependency set for this era, plus git and
# ca-certificates, which ubuntu:16.04 does not ship and the clone below needs.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    curl \\
    ca-certificates \\
    build-essential \\
    g++ \\
    cmake \\
    autoconf \\
    pkg-config \\
    gperf \\
    python \\
    python-dev \\
    python-pip \\
    python-setuptools \\
    python-mako \\
    python3 \\
    python-virtualenv \\
    virtualenv \\
    libssl-dev \\
    libbz2-dev \\
    libglib2.0-dev \\
    libfreetype6-dev \\
    libfontconfig1-dev \\
    freeglut3-dev \\
    libgl1-mesa-dri \\
    libglu1-mesa-dev \\
    libgles2-mesa-dev \\
    libegl1-mesa-dev \\
    libosmesa6-dev \\
    libxmu6 \\
    libxmu-dev \\
    xorg-dev \\
    libdbus-1-dev \\
    libudev-dev \\
    && rm -rf /var/lib/apt/lists/* \\
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \\
        | sh -s -- -y --no-modify-path --profile minimal --default-toolchain none

{code}
{clear_block}
CMD ["/bin/bash"]
"""


class ServoImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return ServoImageBase(self.pr, self.config)

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
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "freeze_index.py",
                _FREEZE_INDEX_PY,
            ),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(
                    pr=self.pr,
                    toolchain=_toolchain_fallback(self.pr),
                    cargo_dir=_CARGO_DIR,
                    crates=_target_crates(self.pr),
                ),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH.format(
                    repo=self.pr.repo,
                    cargo_dir=_CARGO_DIR,
                    crates=_target_crates(self.pr),
                    patch_line="",
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _RUN_SH.format(
                    repo=self.pr.repo,
                    cargo_dir=_CARGO_DIR,
                    crates=_target_crates(self.pr),
                    patch_line="git apply --whitespace=nowarn /home/test.patch\n",
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _RUN_SH.format(
                    repo=self.pr.repo,
                    cargo_dir=_CARGO_DIR,
                    crates=_target_crates(self.pr),
                    patch_line="git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        sha = self.pr.base.sha
        number = self.pr.number
        repo = self.pr.repo

        # The base image is a plain clone with origin intact, so the checkout and
        # the history stripping both belong here, per PR. DockerfileEnhancer does
        # not touch this file (dependency() returns an Image, not a string).
        return f"""FROM {name}:{tag}

{self.global_env}
{copy_commands}
WORKDIR /home/{repo}

# prepare.sh recovers this PR's base commit if the shared base image's clone does
# not carry it, checks it out, and installs. It has to precede the hardening block
# below, which asserts HEAD is that commit and prunes everything else away.
RUN bash /home/prepare.sh

# Git stripping / hardening. Pins the tree to the base commit and reduces the
# repository to exactly that history, then asserts the four invariants:
# HEAD == base commit, no residual refs, no remotes, no unreachable objects.
RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
{self.clear_env}
"""


@Instance.register("servo", _INTERVAL_NAME)
class SERVO_10189_TO_6188(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ServoImageDefault(self.pr, self._config)

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
        # Strip colour first: every pattern below is anchored and would miss a
        # line wrapped in SGR escapes.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_crate = re.compile(r"^===CRATE=== (\S+)$")
        re_pass = re.compile(r"^test (\S+) \.\.\. ok$")
        re_fail = re.compile(r"^test (\S+) \.\.\. FAILED$")
        re_skip = re.compile(r"^test (\S+) \.\.\. ignored$")

        crate = ""
        for line in log.splitlines():
            line = line.strip()

            match = re_crate.match(line)
            if match:
                crate = match.group(1)
                continue

            for pattern, bucket in (
                (re_pass, passed_tests),
                (re_fail, failed_tests),
                (re_skip, skipped_tests),
            ):
                match = pattern.match(line)
                if match:
                    name = match.group(1)
                    bucket.add(f"{crate}::{name}" if crate else name)
                    break

        # A name landing in two buckets resolves to the worse outcome, keeping
        # the three sets disjoint as TestResult requires.
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
