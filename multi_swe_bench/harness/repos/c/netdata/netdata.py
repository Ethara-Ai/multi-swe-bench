from __future__ import annotations

"""netdata/netdata registry config (modern era, PR #8000+).

ARCHITECTURE: SHARED BASE.
  ``ImageBase.image_tag()`` returns the literal ``"base"``, so the whole PR set
  collapses onto ONE base image that is built once.  Consequences, enforced
  below (QC Phase 0, checks 0A/0B/0C/0D):

    * the base Dockerfile carries ``# syntax=docker/dockerfile:1.6`` as line 1,
      which opts it out of ``DockerfileEnhancer.enhance()`` (image.py:317-318).
      Without the directive the enhancer's ``_inject_final_sanitize()``
      (image.py:389-435) would append ``Image._HARDENING_BLOCK`` here — a block
      that checks out ``${BASE_COMMIT}`` and then deletes every ref — pruning the
      ONE shared base down to ONE arbitrary PR's base commit and breaking
      ``git checkout`` for all the others ("reference is not a tree").
    * because it is opted out, nothing is injected, so the proxy/CA/label/cert
      infrastructure is written out by hand here (QC check 2E, row 2).
    * the base therefore keeps FULL git history and never consumes
      ``${BASE_COMMIT}``; ``ARG BASE_COMMIT`` is declared only so the build arg
      the harness always passes for a string-dependency image
      (build_dataset.py:623-629) does not raise an unconsumed-ARG warning.
    * the pin AND the anti-reward-hack history scrub both live in the PR layer
      (``ImageDefault``), which is never enhanced (image.py:315-316).
    * the base Dockerfile is the FIXED TEMPLATE and nothing else: syntax
      directive, ``ARG TARGETARCH`` / ``REPO_URL`` / ``BASE_COMMIT``, the proxy
      ARGs, the proxy/CA ``ENV`` block, the OCI labels, the cert symlink farm,
      ``WORKDIR /home/``, the clone, ``WORKDIR /home/<repo>``, ``CMD``.  It does
      no provisioning at all -- every system dependency and every toolchain env
      var is set up in ``prepare.sh`` instead, so the base stays byte-identical
      in shape to the template this registry is standardised on.

TEST STRATEGY — derived per PR from its own ``test_patch``.
  netdata is a C daemon that also vendors a large Go module at ``src/go``
  (module ``github.com/netdata/netdata/go/plugins``, ``go 1.26``-``1.27``).
  Across this dataset 12 of 15 PRs change ONLY Go test files under ``src/go``,
  so a C-only test command (``netdata -W unittest``) could never observe their
  transitions.  Each instance therefore gets exactly one of two paths:

    GO PATH  (test_patch touches ``src/go/**/*_test.go``)
      ``go test -json -count=1`` over precisely the packages the test patch
      touches.  No C build at all — it would add 20-40 min per image and
      contribute nothing to those PRs' transitions.

    C PATH   (test_patch touches no Go test file)
      full ``netdata-installer.sh --dev`` build in ``prepare.sh`` (so the three
      stages only ever rebuild incrementally), then ``netdata -W unittest``,
      plus any ``tests/*.sh`` suite the test patch adds, plus
      ``ebpf.plugin --unittest`` when the patch touches the eBPF unit tests.

  Both paths run the SAME command text in ``run.sh`` / ``test-run.sh`` /
  ``fix-run.sh`` (QC check 3B point 6); only the patches applied differ.

ERA ANALYSIS (QC check 5A -- the dataset spans PR #22750..#24409, width 1659).
  Checked at the oldest base commit (pr-22750, 3043f83f) and the newest
  (pr-24409, 6a9d13b0); the toolchain is unchanged across the range:

    * build system   : CMake via ``netdata-installer.sh`` at both ends
                       (``NETDATA_BUILD_DIR`` defaults to ``./build``,
                       ``--dev`` keeps it; installer lines 285/393/717).
    * C test runner  : ``netdata -W unittest`` at both ends
                       (``tests/run-unit-tests.sh:c_unit_tests``).
    * Go module      : one module, ``src/go/go.mod``, module path
                       ``github.com/netdata/netdata/go/plugins`` at both ends;
                       no ``go.work``, no vendor dir, and no go.mod/go.sum
                       change anywhere in the 15 patches.
    * Go language rev: ``go 1.26.0`` (oldest) -> ``go 1.27.0`` (newest), so a
                       single 1.27 toolchain serves the whole range; no split
                       needed.
    * submodules     : ``src/aclk/aclk-schemas`` and the vendored lm-sensors
                       tree, unchanged.

  The pre-v1.20 autotools era (#1986..#7436) already lives in its own file,
  ``netdata_7436_to_1986.py``, registered under its own ``number_interval`` key;
  the two eras share no image tag (that file builds only ``pr-<N>`` images
  straight from ubuntu:20.04 and the PR numbers do not overlap).
"""

import json
import re
from pathlib import PurePosixPath
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest



_PROXY_ARGS = (
    'ARG http_proxy=""\n'
    'ARG https_proxy=""\n'
    'ARG HTTP_PROXY=""\n'
    'ARG HTTPS_PROXY=""\n'
    'ARG no_proxy="localhost,127.0.0.1,::1"\n'
    'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
    'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
)

_ENV_BLOCK = (
    "ENV DEBIAN_FRONTEND=noninteractive \\\n"
    "    LANG=C.UTF-8 \\\n"
    "    TZ=UTC \\\n"
    "    http_proxy=${http_proxy} \\\n"
    "    https_proxy=${https_proxy} \\\n"
    "    HTTP_PROXY=${HTTP_PROXY} \\\n"
    "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
    "    no_proxy=${no_proxy} \\\n"
    "    NO_PROXY=${NO_PROXY} \\\n"
    "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
    "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    CURL_CA_BUNDLE=${CA_CERT_PATH}"
)

_CERT_SYMLINKS = (
    "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
)

_HISTORY_SCRUB = """\
RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
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
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""



_DIFF_FILE_RE = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$', re.MULTILINE)

_GO_ROOT = "src/go/"
_GO_MODULE = "github.com/netdata/netdata/go/plugins"


def _touched_files(patch: str) -> list[str]:
    if not patch:
        return []
    text = patch.replace("\r\n", "\n").replace("\r", "\n")
    return [m[0] for m in _DIFF_FILE_RE.findall(text)]


def _go_packages(pr: PullRequest) -> list[str]:
    pkgs = set()
    for f in _touched_files(pr.test_patch):
        if f.startswith(_GO_ROOT) and f.endswith("_test.go"):
            rel = str(PurePosixPath(f[len(_GO_ROOT) :]).parent)
            pkgs.add("." if rel in ("", ".") else rel)
    return sorted(pkgs)


def _shell_suites(pr: PullRequest) -> list[str]:
    out = []
    for f in _touched_files(pr.test_patch):
        if not (f.startswith("tests/") and f.endswith(".sh")):
            continue
        if f == "tests/run-unit-tests.sh" or f.startswith("tests/manual/"):
            continue
        out.append(f)
    return sorted(set(out))


def _touches_ebpf_tests(pr: PullRequest) -> bool:
    return any(
        f.startswith("src/collectors/ebpf.plugin/") and f.endswith((".c", ".h"))
        for f in _touched_files(pr.test_patch)
    )


def _fix_touches_go(pr: PullRequest) -> bool:
    return any(f.startswith(_GO_ROOT) for f in _touched_files(pr.fix_patch))


def _needs_c_build(pr: PullRequest) -> bool:
    return not (_go_packages(pr) and _fix_touches_go(pr))


def _installer_flags(pr: PullRequest) -> str:
    flags = [
        "--dev",
        "--dont-wait",
        "--dont-start-it",
        "--install-prefix /opt/nd",
        "--disable-lto",
        "--disable-go",
        "--disable-plugin-otel",
        "--use-system-protobuf",
    ]
    if not _touches_ebpf_tests(pr):
        flags.append("--disable-ebpf")
    return " ".join(flags)


def _installer_flags_bundled(pr: PullRequest) -> str:
    return " ".join(
        f for f in _installer_flags(pr).split(" ") if f != "--use-system-protobuf"
    )


_NETDATA_BIN = "/opt/nd/netdata/usr/sbin/netdata"
_EBPF_PLUGIN = "/opt/nd/netdata/usr/libexec/netdata/plugins.d/ebpf.plugin"


def _go_test_block(pr: PullRequest) -> str:
    pkgs = " ".join(f'"{p}"' for p in _go_packages(pr))
    return f"""cd /home/netdata/src/go

GO_PKGS=""
for d in {pkgs}; do
    if [ -d "$d" ]; then GO_PKGS="$GO_PKGS ./$d"; fi
done

if [ -z "$GO_PKGS" ]; then
    echo "NETDATA_HARNESS: none of this PR's Go packages exist at this revision"
    exit 0
fi

rc=0
# shellcheck disable=SC2086
go test -json -count=1 -timeout 25m $GO_PKGS || rc=$?
echo "NETDATA_GO_TEST_EXIT_CODE=$rc"
exit "$rc\""""


def _c_test_block(pr: PullRequest) -> str:
    lines = [
        "cd /home/netdata",
        "",
        "# Incremental rebuild of the patched tree. --dev reuses ./build, so this",
        "# is a no-op in the baseline stage and a delta build in the other two.",
        "# Deliberately NOT tolerated: if the patched tree does not compile, the",
        "# stage must stop here rather than fall through to the stale binary that",
        "# prepare.sh installed and report its (unrelated) passing tests.",
        f"NETDATA_DISABLE_TELEMETRY=1 ./netdata-installer.sh {_installer_flags(pr)} 2>&1",
        "",
        "rc=0",
        "",
        "# netdata's canonical C unit-test suite (tests/run-unit-tests.sh:c_unit_tests).",
        f'ASAN_OPTIONS=detect_leaks=0 {_NETDATA_BIN} -W unittest 2>&1 || rc=$?',
        'echo "NETDATA_UNITTEST_EXIT_CODE=$rc"',
    ]

    if _touches_ebpf_tests(pr):
        lines += [
            "",
            "# ebpf.plugin's own unit tests (src/collectors/ebpf.plugin/ebpf_unittest.c),",
            "# reached through the --unittest option registered in ebpf.c.",
            f"if [ -x {_EBPF_PLUGIN} ]; then",
            "    ebpf_rc=0",
            f"    {_EBPF_PLUGIN} --unittest 2>&1 || ebpf_rc=$?",
            '    echo "NETDATA_EBPF_UNITTEST_EXIT_CODE=$ebpf_rc"',
            '    if [ "$ebpf_rc" -ne 0 ]; then rc="$ebpf_rc"; fi',
            "else",
            f'    echo "NETDATA_HARNESS: {_EBPF_PLUGIN} was not built"',
            "fi",
        ]

    for suite in _shell_suites(pr):
        lines += [
            "",
            f"if [ -f {suite} ]; then",
            "    suite_rc=0",
            f"    NETDATA_BUILD_DIR=/home/netdata/build bash {suite} 2>&1 || suite_rc=$?",
            f'    echo "NETDATA_SHELL_SUITE_EXIT_CODE[{suite}]=$suite_rc"',
            '    if [ "$suite_rc" -ne 0 ]; then rc="$suite_rc"; fi',
            "else",
            f'    echo "NETDATA_HARNESS: {suite} does not exist at this revision"',
            "fi",
        ]

    lines += ["", 'exit "$rc"']
    return "\n".join(lines)


def _test_block(pr: PullRequest) -> str:
    return _c_test_block(pr) if _needs_c_build(pr) else _go_test_block(pr)




class ImageBase(Image):
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
        return "golang:1.27.3-bookworm"

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

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{_PROXY_ARGS}

{_ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_CERT_SYMLINKS}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_provision(self) -> str:
        if _needs_c_build(self.pr):
            return f"""# ---- Section 2: PROVISION ----------------------------------------------
# System dependencies.  These live here, not in the base Dockerfile: the base
# image is the fixed template (syntax directive, ARGs, proxy/CA ENV, LABEL, cert
# symlinks, clone, WORKDIR, CMD) and carries no provisioning of its own.  The
# cert symlink farm and the proxy ENV are already in place from the base, so this
# is the first network call and it is already behind the CA trust (QC 2E).
#
# C toolchain plus every library netdata's CMake marks REQUIRED (libcurl,
# libcrypto, zlib, liblz4, uuid, libuv) and the optional ones it probes for.  No
# apt source is pinned to an architecture and nothing is downloaded with a
# hardcoded arch, so this provisions identically on amd64 and arm64 (QC 2D).
apt-get update
apt-get install -y --no-install-recommends \\
    autoconf \\
    autoconf-archive \\
    autogen \\
    automake \\
    bison \\
    build-essential \\
    ca-certificates \\
    cmake \\
    curl \\
    file \\
    flex \\
    git \\
    gzip \\
    libatomic1 \\
    libbrotli-dev \\
    libcurl4-openssl-dev \\
    libelf-dev \\
    libjson-c-dev \\
    liblz4-dev \\
    libmnl-dev \\
    libpcre2-dev \\
    libprotobuf-dev \\
    libsnappy-dev \\
    libssl-dev \\
    libsystemd-dev \\
    libtool \\
    libuv1-dev \\
    libyaml-dev \\
    libzstd-dev \\
    make \\
    netcat-openbsd \\
    ninja-build \\
    pkg-config \\
    procps \\
    protobuf-compiler \\
    python3 \\
    tar \\
    uuid-dev \\
    zlib1g-dev
apt-get clean
rm -rf /var/lib/apt/lists/*

# aclk-schemas and the vendored lm-sensors tree are submodules; the C build
# needs both.
cd /home/netdata
git submodule update --init --recursive

# MAKEOPTS caps the job count (netdata-installer.sh:416-417 parses -j out of it).
# Left unset the installer uses every core; this harness builds images
# concurrently, so an unbounded -j thrashes the daemon's memory and the build
# rate collapses.
export MAKEOPTS="-j4"

if ! NETDATA_DISABLE_TELEMETRY=1 ./netdata-installer.sh {_installer_flags(self.pr)} 2>&1; then
    # Provisioning only -- no test command is tolerated anywhere.  If the system
    # protobuf turns out to be unusable at this revision, redo the build with the
    # vendored copy so a faster default can never become a failed image.  The
    # gate below stays hard either way.
    echo "INSTALLER: system protobuf unusable, retrying with the vendored copy"
    rm -rf /home/netdata/build
    NETDATA_DISABLE_TELEMETRY=1 ./netdata-installer.sh {_installer_flags_bundled(self.pr)} 2>&1
fi

# ---- Section 3: GATE ---------------------------------------------------
# Hard, non-tolerant: the agent must be installed and runnable, otherwise every
# stage would run a binary that does not exist and parse_log would see 0/0/0.
test -x {_NETDATA_BIN}
{_NETDATA_BIN} -v
echo DEPS_OK
"""

        pkgs = " ".join(f'"./{p}"' for p in _go_packages(self.pr))
        return f"""# ---- Section 2: PROVISION ----------------------------------------------
# System dependencies.  These live here, not in the base Dockerfile: the base
# image is the fixed template (syntax directive, ARGs, proxy/CA ENV, LABEL, cert
# symlinks, clone, WORKDIR, CMD) and carries no provisioning of its own.  The
# cert symlink farm and the proxy ENV are already in place from the base, so this
# is the first network call and it is already behind the CA trust (QC 2E).
#
# C toolchain plus every library netdata's CMake marks REQUIRED (libcurl,
# libcrypto, zlib, liblz4, uuid, libuv) and the optional ones it probes for.  No
# apt source is pinned to an architecture and nothing is downloaded with a
# hardcoded arch, so this provisions identically on amd64 and arm64 (QC 2D).
apt-get update
apt-get install -y --no-install-recommends \\
    autoconf \\
    autoconf-archive \\
    autogen \\
    automake \\
    bison \\
    build-essential \\
    ca-certificates \\
    cmake \\
    curl \\
    file \\
    flex \\
    git \\
    gzip \\
    libatomic1 \\
    libbrotli-dev \\
    libcurl4-openssl-dev \\
    libelf-dev \\
    libjson-c-dev \\
    liblz4-dev \\
    libmnl-dev \\
    libpcre2-dev \\
    libssl-dev \\
    libsystemd-dev \\
    libtool \\
    libuv1-dev \\
    libyaml-dev \\
    libzstd-dev \\
    make \\
    netcat-openbsd \\
    ninja-build \\
    pkg-config \\
    procps \\
    python3 \\
    tar \\
    uuid-dev \\
    zlib1g-dev
apt-get clean
rm -rf /var/lib/apt/lists/*

cd /home/netdata/src/go
go mod download

# ---- Section 3: GATE ---------------------------------------------------
# Hard, non-tolerant.  `-run '^$'` runs no test but still compiles and links the
# package's test binary, so a half-resolved module or a cgo dependency that will
# not build is caught here, at image-build time, instead of surfacing as an empty
# test log three stages later.  It also warms the build cache for the stages.
#
# Per package, with at least one required: a PR may touch several packages and
# one of them (e.g. plugin/ibm.d, which links a vendor C client) can be
# unbuildable in a plain container while the rest grade perfectly well.  Failing
# the whole image on that would discard a usable instance, so each failure is
# named in the build log and the gate still fails hard if NONE builds -- which
# is the "no dependencies installed at all" case this gate exists to catch.
cd /home/netdata/src/go
gate_ok=0
for p in {pkgs}; do
    if go test -count=1 -run '^$' "$p"; then
        gate_ok=1
    else
        echo "GATE: $p does not build at this revision"
    fi
done
test "$gate_ok" -eq 1
echo DEPS_OK
"""

    def files(self) -> list[File]:
        test_block = _test_block(self.pr)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail
# GOTOOLCHAIN=auto lets a base commit whose go.mod asks for a newer patch
# release fetch it instead of failing; GOFLAGS=-mod=mod keeps `go test` from
# erroring on a vendor/ dir that some eras carry.  Exported here (and in the run
# scripts) rather than set as ENV in the base image, which is the fixed template.
export CI=true
export DEBIAN_FRONTEND=noninteractive
export GOTOOLCHAIN=auto
export GOFLAGS=-mod=mod
export CGO_ENABLED=1

git config --global --add safe.directory '*'

# ---- Section 1: PIN ----------------------------------------------------
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{provision}""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    provision=self._prepare_provision(),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto
export GOFLAGS=-mod=mod
export CGO_ENABLED=1

{test}
""".format(test=test_block),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto
export GOFLAGS=-mod=mod
export CGO_ENABLED=1

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

{test}
""".format(repo=self.pr.repo, test=test_block),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto
export GOFLAGS=-mod=mod
export CGO_ENABLED=1

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test}
""".format(repo=self.pr.repo, test=test_block),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{_HISTORY_SCRUB}

{self.clear_env}
"""




@Instance.register("netdata", "netdata")
class Netdata(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log or "")

        non_json_lines: list[str] = []

        for raw in test_log.splitlines():
            line = raw.strip()
            if not (line.startswith("{") and '"Action"' in line):
                non_json_lines.append(raw)
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                non_json_lines.append(raw)
                continue
            if not isinstance(ev, dict):
                non_json_lines.append(raw)
                continue

            test = ev.get("Test")
            action = ev.get("Action")
            if not test or action not in ("pass", "fail", "skip"):
                continue

            pkg = ev.get("Package") or ""
            if pkg.startswith(_GO_MODULE):
                pkg = pkg[len(_GO_MODULE) :].lstrip("/") or "."
            name = f"go:{pkg}::{test}" if pkg else f"go:{test}"

            if action == "fail":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif action == "pass":
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
            else:
                if name not in failed_tests and name not in passed_tests:
                    skipped_tests.add(name)

        re_running_test = re.compile(r"^Running test '([^']+)'")
        re_unit_test_fail = re.compile(r"^UNIT TEST\((\d+),\s*(\d+)\)\s*FAILED")
        re_make_check = re.compile(r"^(PASS|FAIL|SKIP|XFAIL|XPASS):\s+(\S+)")
        re_cmocka_ok = re.compile(r"^\[\s*OK\s*\]\s+(\S+)")
        re_cmocka_fail = re.compile(r"^\[\s*FAILED\s*\]\s+(\S+)")
        re_unittest_fn = re.compile(
            r"^(\w+(?:_unittest|_test|_tests))\(\)(?::|\s)\s*(.*)"
        )
        re_spawn_testing = re.compile(r"^TESTING\s+(.+?)\s*$")
        re_nd_running = re.compile(r"^(\w+(?:_unittest|_test|_tests))\(\)\s+running")
        re_nd_subresult = re.compile(r"^(OK|FAILED)\s+([^:]{1,60}):")
        re_slug_chars = re.compile(r"\W+")
        pass_verbs = ("passed", "ok", "done", "completed", "success")
        fail_verbs = ("failed", "fail", "error")

        def slug(text, maxlen=80):
            return re_slug_chars.sub("_", text.lower()).strip("_")[:maxlen]

        current_name = None
        current_tag = None
        current_failed = False
        awaiting_description = False
        suite_completed = False

        spawn_tags: list[str] = []
        spawn_completed = False

        nd_suite = None

        def close_current(failed_override=None):
            nonlocal current_name, current_tag, current_failed, awaiting_description
            if not current_tag:
                return
            failed = current_failed if failed_override is None else failed_override
            (failed_tests if failed else passed_tests).add(current_tag)
            current_name = None
            current_tag = None
            current_failed = False
            awaiting_description = False

        for line in non_json_lines:
            stripped = line.strip()

            if awaiting_description:
                awaiting_description = False
                if stripped:
                    current_tag = f"{current_name}_{slug(stripped)}"
                continue

            if not stripped:
                continue

            m = re_running_test.match(stripped)
            if m:
                close_current()
                current_name = slug(m.group(1))
                current_tag = current_name
                current_failed = False
                awaiting_description = True
                continue

            m = re_spawn_testing.match(stripped)
            if m:
                spawn_tags.append(f"spawn:{slug(m.group(1))}")
                continue

            m = re_nd_running.match(stripped)
            if m:
                nd_suite = m.group(1)
                continue

            if nd_suite:
                m = re_nd_subresult.match(stripped)
                if m:
                    tag = f"nd:{nd_suite}:{slug(m.group(2))}"
                    if m.group(1) == "OK":
                        if tag not in failed_tests:
                            passed_tests.add(tag)
                    else:
                        passed_tests.discard(tag)
                        failed_tests.add(tag)
                    continue

            if stripped.startswith("Tests passed!"):
                spawn_completed = True
                continue

            if current_tag and "### E R R O R ###" in stripped:
                current_failed = True
                continue

            if "SQLite is OK" in stripped:
                passed_tests.add("test_sqlite")
                continue
            if "Failed to test SQLite" in stripped:
                failed_tests.add("test_sqlite")
                continue

            m = re_unit_test_fail.match(stripped)
            if m:
                failed_tests.add(f"unit_test_{m.group(1)}_{m.group(2)}")
                continue

            m = re_make_check.match(stripped)
            if m:
                verdict, name = m.group(1), m.group(2)
                tag = f"check_{slug(name)}"
                if verdict in ("PASS", "XFAIL"):
                    passed_tests.add(tag)
                elif verdict in ("FAIL", "XPASS"):
                    failed_tests.add(tag)
                else:
                    skipped_tests.add(tag)
                continue

            m = re_cmocka_ok.match(stripped)
            if m:
                passed_tests.add(f"cmocka_{m.group(1)}")
                continue
            m = re_cmocka_fail.match(stripped)
            if m:
                failed_tests.add(f"cmocka_{m.group(1)}")
                continue

            m = re_unittest_fn.match(stripped)
            if m:
                name = m.group(1)
                rest = (m.group(2) or "").lstrip().lower()
                if rest.startswith(pass_verbs):
                    passed_tests.add(name)
                    continue
                if rest.startswith(fail_verbs):
                    failed_tests.add(name)
                    continue

            if "all tests passed" in stripped.lower():
                close_current()
                passed_tests.add("netdata_unittest_suite")
                suite_completed = True
                continue

        if current_tag:
            close_current(failed_override=False if suite_completed else True)

        if spawn_tags:
            if spawn_completed:
                passed_tests.update(spawn_tags)
            else:
                passed_tests.update(spawn_tags[:-1])
                failed_tests.add(spawn_tags[-1])

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
