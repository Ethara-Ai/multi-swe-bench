"""sqlalchemy/sqlalchemy harness config for the LHT (bundled-PR) dataset.

Routing: one registry key per bundle, taken verbatim from each record's
``number_interval`` (dash-joined ``prs_in_bundle``), so ``#keys == #instances``.
A bare ``sqlalchemy/sqlalchemy`` key is kept as a fallback for records whose
``number_interval`` is not populated.

Two-level build:

Level 1 (``SQLAlchemyImageBase``, tag ``base``): ONE image shared by every
sqlalchemy PR. The tag carries no PR number and no commit, so all 24 bundles
resolve to the same image and it is built once. It holds the PR-independent
cost -- apt toolchain, the four CPython interpreters the dataset needs, and a
full-history clone. It deliberately does NOT check out BASE_COMMIT and carries
only *light* hardening (remote dropped, no submodule recursion): a full scrub
would pin this shared base to one bundle's commit and destroy the history every
other bundle needs.

Level 2 (``SQLAlchemyImageDefault``, tag ``pr-<n>``): FROM the base;
``prepare.sh`` checks out the bundle's base commit and builds the release-line
virtualenv, then ``Image._HARDENING_BLOCK`` runs so the fix cannot be recovered
from git history.

Neither Dockerfile carries any proxy or CA plumbing. ``DockerfileEnhancer``'s
``_MITM_MOUNT`` (the mitm_ca secret mount), ``_PROXY_ARGS`` (the
http_proxy/https_proxy/no_proxy/CA_CERT_PATH build-args) and ``_ENV_BLOCK``
(which exports those plus SSL_CERT_FILE/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE) are
all deliberately unused, so none of those ARGs or ENVs reach the base image,
the per-PR images, or the eval containers. TLS still works during the build
because openssl, curl and requests fall back to the distro trust store that the
``ca-certificates`` package installs.
"""

import re
from dataclasses import dataclass
from typing import Optional

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Where the per-PR virtualenv lives. Outside the git tree, so the hardening
# pass and any `git checkout` in the run scripts leave it alone.
_VENV = "/opt/venv"


@dataclass(frozen=True)
class _ReleaseLine:
    """Everything that differs between SQLAlchemy release lines."""

    # CPython version, as `uv python install` names it.
    python: str
    # pip requirement list, already shell-quoted (specifiers contain '<'/'>').
    test_deps: str
    # Flags accepted only by the newer pytest of that era.
    pytest_opts: str
    # Test *selection*, taken from the release line's own tox.ini.
    pytest_select: str


_RELEASE_LINES = {
    # 1.3 predates the pytest markers; selection goes through SQLAlchemy's own
    # --exclude-tag, and pytest 4.6 has no --no-header.  Pinned to the last
    # pytest/pytest-xdist pair 1.3's testing plugin was written against.
    "1.3": _ReleaseLine(
        python="3.8",
        test_deps="'pytest==4.6.11' 'pytest-xdist==1.34.0' mock",
        pytest_opts="",
        pytest_select=(
            "--exclude-tag memory-intensive --exclude-tag timing-intensive"
        ),
    ),
    # Every 1.4 base commit in this dataset is < 1.4.29, i.e. before upstream
    # added pytest 7 support, so pin to the 6.2 series.
    "1.4": _ReleaseLine(
        python="3.9",
        test_deps="'pytest==6.2.5' 'pytest-xdist==2.5.0' greenlet mock",
        pytest_opts="--no-header",
        pytest_select=(
            "--exclude-tag memory-intensive --exclude-tag timing-intensive"
        ),
    ),
    # 2.0 moved the exclusions to real pytest markers (tox.ini PYTEST_EXCLUDES).
    # Excluding "mypy" is what lets us skip installing mypy + the stubs.
    "2.0": _ReleaseLine(
        python="3.11",
        test_deps="'pytest>=7.0.0rc1,<8' 'pytest-xdist!=3.3.0' greenlet",
        pytest_opts="--no-header",
        pytest_select=(
            '-m "not memory_intensive and not mypy and not timing_intensive"'
        ),
    ),
    # 2.1 requires Python >= 3.10 and builds its Cython extensions with
    # setuptools >= 77, which a bare venv does not ship; a missing setuptools
    # also skips part of the suite outright.
    "2.1": _ReleaseLine(
        python="3.12",
        test_deps=(
            "'pytest>=7.0.0rc1,<10' pytest-xdist 'greenlet>=1' setuptools"
        ),
        pytest_opts="--no-header",
        pytest_select=(
            '-m "not memory_intensive and not mypy and not timing_intensive '
            'and not gc_intensive"'
        ),
    ),
}

# Interpreters the shared base pre-installs -- the union of the release lines
# above. Kept in one place so the base image and the per-PR venv cannot drift.
_BASE_PYTHONS = sorted(
    {line.python for line in _RELEASE_LINES.values()},
    key=lambda v: tuple(int(part) for part in v.split(".")),
)

# Exact python-build-standalone builds for each series, as "<version>+<tag>".
# These are the same artefacts `uv python install` fetches, but pulled with
# curl + tar so that NO foreign-architecture Rust binary has to execute during
# the build: the uv binary segfaults under qemu-x86_64, which breaks the
# linux/amd64 leg of a multi-arch build. curl, tar and CPython all emulate
# cleanly. They are also fully self-contained (bundled libssl/libsqlite3), so
# they do not care what distro the base image is.
# 3.8 and 3.9 are pinned to their final builds -- both series are EOL and no
# longer present in current python-build-standalone releases.
_PYTHON_BUILDS = {
    "3.8": "3.8.20+20241002",
    "3.9": "3.9.25+20251031",
    "3.11": "3.11.15+20260718",
    "3.12": "3.12.13+20260718",
}

# Where the base unpacks them: /opt/pythons/<series>/bin/python3.
_PYTHON_ROOT = "/opt/pythons"


def _python_bin(series: str) -> str:
    return f"{_PYTHON_ROOT}/{series}/bin/python3"


# Base commit -> release line, for every bundle in the LHT dataset. This is
# keyed on the commit rather than the PR number on purpose: several bundles
# branch from a far older release than their number suggests (bundle 7363 is
# 1.4.9, bundle 10506 is 2.0.9), so a number-range mapping would pick the wrong
# interpreter for them. Comments record the __version__ at that commit.
_BASE_COMMIT_RELEASE_LINE = {
    "38d34ca4dadb68d80422e80ce17fc95f7ded6006": "1.3",  # 1.3.0b3
    "98abb4cd0729651e2bcf0aa7ceecb0b02b1902ae": "1.3",  # 1.3.13
    "dba0c8133e6b58499bb1e57dc66862d77e1aae89": "1.4",  # 1.4.0b1
    "1f9738acf30a279f52b7d0b447a379ab84beefd6": "1.4",  # 1.4.0b2
    "a5e10cbd5339d42c49a3d37bb92471800d1ef252": "1.4",  # 1.4.0b3
    "f6da955b6602563811f46f77fa436e4b8432b20d": "1.4",  # 1.4.1
    "7d6700d96dd1fd24a8d459477fbc6f14f3b637a3": "1.4",  # 1.4.2
    "fd669c5618605ffd6c9e70d221113520670af39b": "1.4",  # 1.4.5
    "c46b653893bb7ca68aa1909c75d871766dfda2ae": "1.4",  # 1.4.9
    "507a828de6f8b5f878093d86332f1954bd154ca3": "1.4",  # 1.4.11
    "8a9a2e384db2d4b9fedf8d3b62dbf8937b7ba2b8": "1.4",  # 1.4.15
    "e3ce7ee2ee5b647416654763bd0c8c781b88d79f": "1.4",  # 1.4.20
    "dbca3582f9aa105f9c0ddf4eba5b4130f297ad7d": "1.4",  # 1.4.23
    "3b4b0a3946b2e6ccd3fd51b389be09e8f0f81e07": "1.4",  # 1.4.25
    "2b5aacf114dbab663ef228aeee732dde9425de47": "2.0",  # 2.0.1
    "f3e788e034ef6a76868319a4fb48cbf5e0bc9e60": "2.0",  # 2.0.2
    "0d9221362e5a3e50de7e21e674da3701f4bcd646": "2.0",  # 2.0.9
    "b722c8ab24e2379cd19acc6bff0c73e14deaf644": "2.0",  # 2.0.15
    "ffab94fc5f31c7a4a83d6e22a6be6ff43db0cabf": "2.0",  # 2.0.16
    "e3b9d4c19ec0a77556c630660eb3809dc3cc775d": "2.0",  # 2.0.18
    "a52103c44f3989d53edf4c76abb92afd3ca724c9": "2.0",  # 2.0.20
    "35672ddf0dcd3ac7aab2305901040d30002e1793": "2.0",  # 2.0.21
    "cc9edddba969f1bf4cdc8db932c80e1f15171b62": "2.0",  # 2.0.23
    "d9d947eb8b5c05eb3d1172ed25950bbf1418df62": "2.1",  # 2.1.0b1
}


def _release_line(pr: PullRequest) -> _ReleaseLine:
    key = _BASE_COMMIT_RELEASE_LINE.get(pr.base.sha)
    if key is not None:
        return _RELEASE_LINES[key]

    # Fallback for bundles outside the shipped dataset. Only a rough proxy --
    # see the note on _BASE_COMMIT_RELEASE_LINE -- so extend the table above
    # rather than relying on this.
    number = pr.number
    if number < 5700:
        key = "1.3"
    elif number < 9000:
        key = "1.4"
    elif number < 13000:
        key = "2.0"
    else:
        key = "2.1"
    return _RELEASE_LINES[key]


def _test_command(pr: PullRequest) -> str:
    """The pytest invocation for this PR's release line.

    --maxfail=0 lifts the --maxfail=25 that SQLAlchemy sets in its own addopts:
    a fix-patch run legitimately produces hundreds of failures and must not be
    cut short, or the p2p/f2p comparison sees a truncated run.
    """
    line = _release_line(pr)
    parts = [
        f"{_VENV}/bin/python -m pytest",
        line.pytest_opts,
        "-v -rA --tb=no -p no:cacheprovider --rootdir . --maxfail=0",
        "-n4 --max-worker-restart=5",
        line.pytest_select,
        '-k "not aaa_profiling"',
        "test/",
    ]
    return " ".join(part for part in parts if part)


# A result line as parse_log() reads it, in either of the two shapes it accepts:
# the per-worker verbose line ("[gw2] [ 37%] PASSED test/...") that -n4 emits,
# and the bare "-rA" summary line a non-xdist run would produce.
_RESULT_LINE_RE = (
    r"^(\[gw[0-9]+\][[:space:]]+\[[[:space:]]*[0-9]+%\][[:space:]]+)?"
    r"(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)[[:space:]]+test/"
)

# One stage of the suite, retried once if it captured nothing.
#
# A healthy sqlalchemy stage reports 8k-24k result lines; EXACTLY zero never
# happens on a run that reached the tests, so it is an unambiguous "the runner
# died before reporting" signal rather than a judgement call. The observed
# cause is a pytest-xdist worker losing its asyncio event loop at session start
#
#     RuntimeError: There is no current event loop in thread 'Dummy-1'
#
# which xdist 2.5.0 then escalates: its worker_errordown handler removes the
# already-removed node and raises KeyError inside the master, so ONE crashed
# worker discards the entire session. The race is load-dependent and did not
# reproduce in five deliberate attempts, but it hit 2-3 stages on each full
# 24-instance run; every one of those stages passed when simply run again.
#
# Only the pytest call is retried, never the patch application above it -- the
# patches are already in the tree by then and re-applying would fail. Output
# goes to a temp file so a crashed attempt's INTERNALERROR spew is DISCARDED
# rather than concatenated ahead of the good attempt's results, and the exit
# status of the attempt that is actually emitted is preserved.
_RUN_TESTS_SCRIPT = """#!/bin/bash
# usage: bash /home/run-tests.sh
cd /home/{repo}

_out="$(mktemp)"
trap 'rm -f "$_out"' EXIT
_status=0

_attempt() {{
    {test_command} > "$_out" 2>&1
    _status=$?
}}

_captured() {{
    grep -qE '{result_line_re}' "$_out"
}}

_attempt
if ! _captured; then
    echo "run-tests: no test results captured, retrying once..." >&2
    _attempt
fi

cat "$_out"
exit $_status
"""


# Rebuild the package when a patch touches packaging metadata or a compiled
# extension; a plain editable install already picks up pure-python edits.
_REINSTALL_SNIPPET = """if grep -qE 'setup\\.py|setup\\.cfg|pyproject\\.toml|cyextension|cextension|\\.pyx' "$@" 2>/dev/null; then
    # Same clang-vs-gcc override as prepare.sh -- see the comment there.
    export CC=gcc CXX=g++ LDSHARED="gcc -shared"
    {venv}/bin/python -m pip install --no-cache-dir -e . > /dev/null 2>&1
fi"""


class SQLAlchemyImageBase(Image):
    """Level 1: the single image shared by every sqlalchemy PR."""

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
        # The interpreters come from uv's standalone CPython builds, so the
        # distro only has to supply git and a C toolchain.
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        # No PR number, no commit -> ONE shared image for all sqlalchemy PRs.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # Nothing beyond Image.dockerfile()'s defaults: the suite runs on
        # in-memory SQLite (no DB client libraries), and the C / Cython
        # extensions only need build-essential, which is already in the list.
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        repo_url = f"https://github.com/{org}/{repo}.git"

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        packages_str = " \\\n    ".join(default_packages + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        specs = " ".join(f"{s}:{_PYTHON_BUILDS[s]}" for s in _BASE_PYTHONS)
        pbs_url = (
            "https://github.com/astral-sh/python-build-standalone/releases/download"
        )
        python_install = (
            "RUN set -eux; \\\n"
            '    case "${TARGETARCH}" in \\\n'
            "        amd64|'') PBS_ARCH=x86_64 ;; \\\n"
            "        arm64) PBS_ARCH=aarch64 ;; \\\n"
            '        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \\\n'
            "    esac; \\\n"
            f'    for spec in {specs}; do \\\n'
            '        series="${spec%%:*}"; \\\n'
            '        build="${spec#*:}"; \\\n'
            '        tag="${build#*+}"; \\\n'
            f'        url="{pbs_url}/${{tag}}/cpython-${{build}}-${{PBS_ARCH}}-unknown-linux-gnu-install_only_stripped.tar.gz"; \\\n'
            '        mkdir -p "${PYTHON_ROOT}/${series}"; \\\n'
            '        curl -fsSL "$url" -o /tmp/python.tar.gz; \\\n'
            '        tar -xzf /tmp/python.tar.gz -C "${PYTHON_ROOT}/${series}" --strip-components=1; \\\n'
            "        rm -f /tmp/python.tar.gz; \\\n"
            '        "${PYTHON_ROOT}/${series}/bin/python3" -VV; \\\n'
            "    done"
        )

        sections = [
            # Emitting the syntax directive makes DockerfileEnhancer.enhance()
            # return this content unchanged. That is REQUIRED, not cosmetic: its
            # _inject_final_sanitize() pass appends _HARDENING_BLOCK to any
            # Dockerfile containing "git clone", which would detach this SHARED
            # base at whichever PR's BASE_COMMIT happened to build it first and
            # delete every other ref. Because enhancement is skipped, the proxy
            # and cert infrastructure it would normally inject is emitted by
            # hand below, reusing the same constants -- and NOT _MITM_MOUNT,
            # which this registry never emits.
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="{repo_url}"\n'
                # Declared but intentionally unused: the harness passes
                # BASE_COMMIT as a build-arg for string dependencies, and an
                # undeclared arg warns. Unused -> it cannot bust this cache.
                "ARG BASE_COMMIT"
            ),
            # DockerfileEnhancer._PROXY_ARGS / ._ENV_BLOCK are deliberately NOT
            # used: they declare http_proxy/https_proxy/no_proxy/CA_CERT_PATH
            # build-args and export them plus SSL_CERT_FILE/REQUESTS_CA_BUNDLE/
            # CURL_CA_BUNDLE as ENV, all of which would persist into every PR
            # image and every eval container. None of it is emitted here.
            # Dropping the CA_* vars is safe: openssl, curl and requests all
            # fall back to the distro trust store that `ca-certificates`
            # installs, which is the very path those vars pointed at.
            (
                "ENV DEBIAN_FRONTEND=noninteractive \\\n"
                "    LANG=C.UTF-8 \\\n"
                "    LC_ALL=C.UTF-8 \\\n"
                "    TZ=UTC"
            ),
            (
                f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
                f'      org.opencontainers.image.description="{org}/{repo} base image" \\\n'
                f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
                '      org.opencontainers.image.authors="https://www.ethara.ai/"'
            ),
            self.global_env,
            # Mirrors the standard section Image.dockerfile() emits, so this
            # hand-written base reads the same as an enhanced one.
            (
                "WORKDIR /home/\n"
                "ENV DEBIAN_FRONTEND=noninteractive\n"
                "ENV LANG=C.UTF-8"
            ),
            # DockerfileEnhancer._CERT_SYMLINKS is deliberately not emitted: it
            # only aliases RedHat-style trust-store paths onto the Debian CA
            # bundle, which nothing on this Ubuntu base reads now that the CA_*
            # env vars are gone.
            apt_command,
            # Repo-specific environment.
            (
                f"ENV PYTHON_ROOT={_PYTHON_ROOT}\n"
                "ENV PIP_DISABLE_PIP_VERSION_CHECK=1"
            ),
            # One base carries every interpreter the dataset needs; the distro's
            # own python3 is never used for the suite. TARGETARCH is set by
            # buildx per platform, so the same instruction resolves to the
            # x86_64 or aarch64 tarball as appropriate.
            python_install,
            # Full-history clone, NOT checked out: the base is shared, so it
            # must be able to reach every bundle's BASE_COMMIT. The per-PR layer
            # does the checkout and the history scrub.
            f'RUN git clone "${{REPO_URL}}" /home/{repo}',
            # Light base hardening only -- drop the remote so the shared image
            # carries no upstream pointer, and stop submodule recursion. Refs
            # and objects are deliberately left intact: a full scrub here would
            # pin this shared base to one bundle's commit and destroy the
            # history every other bundle needs.
            (
                f"WORKDIR /home/{repo}\n"
                "RUN git remote remove origin 2>/dev/null || true; \\\n"
                "    git config --local fetch.recurseSubmodules false; \\\n"
                "    git config --local gc.auto 0"
            ),
            "WORKDIR /home/",
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(section for section in sections if section) + "\n"


class SQLAlchemyImageDefault(Image):
    """Level 2: checkout, venv, patches and run scripts, then hardening."""

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
        return SQLAlchemyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = _safe_path_component(self.pr.repo)

        copy_commands = "\n".join(
            f"COPY {file.name} /home/" for file in self.files()
        )

        sections = [
            # dependency() is an Image, so DockerfileEnhancer.enhance() returns
            # this unchanged -- no syntax directive or infrastructure block is
            # injected, and no proxy / CA / MITM plumbing appears anywhere.
            f"FROM {base.image_name()}:{base.image_tag()}",
            # ARG, not ENV: the harness only passes BASE_COMMIT as a build-arg
            # when dependency() is a string, so it needs a literal default here.
            # Using ARG also keeps the sha out of the running container's
            # environment, while still resolving in every RUN below -- including
            # _HARDENING_BLOCK's "${BASE_COMMIT}".
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            f"WORKDIR /home/{repo}",
            copy_commands,
            # prepare.sh checks out ${BASE_COMMIT} in the clone the base image
            # already carries, then builds the release-line virtualenv and
            # installs the checkout. It runs before the hardening block; pip
            # writes *.egg-info / build artefacts, which are untracked, so the
            # hardening pass (git history only) leaves them alone.
            "RUN bash /home/prepare.sh",
            # Hardening lives HERE, per-PR -- never in the shared base.
            Image._HARDENING_BLOCK,
            # _HARDENING_BLOCK destroys the fix's OBJECTS, but ORIG_HEAD --
            # written by the "git reset --hard" above, recording the default
            # branch tip -- still names a post-fix sha in plaintext. It is not a
            # gc root, so the content is already unrecoverable offline, but it
            # hands a model an exact sha to re-fetch if the eval container has
            # network.
            "RUN rm -f .git/FETCH_HEAD .git/ORIG_HEAD",
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(section for section in sections if section) + "\n"

    def files(self) -> list[File]:
        line = _release_line(self.pr)
        run_tests = _RUN_TESTS_SCRIPT.format(
            repo=self.pr.repo,
            test_command=_test_command(self.pr),
            result_line_re=_RESULT_LINE_RE,
        )
        reinstall = _REINSTALL_SNIPPET.format(venv=_VENV)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}

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
                f"""#!/bin/bash
# Checks out this bundle's base commit in the clone the base image carries,
# then builds the release-line virtualenv and installs the checkout into it.
set -e

cd /home/{self.pr.repo}

git reset --hard
git checkout {self.pr.base.sha}

# The python-build-standalone interpreters are compiled with clang and record
# CC="clang -pthread" in their sysconfig, which setuptools reuses when building
# C extensions. The base image ships gcc (build-essential), not clang, so
# without this override every extension build dies with
#   command 'clang' failed: No such file or directory
# and SQLAlchemy either fails to install (1.3 / 1.4) or silently falls back to
# pure Python (2.0 / 2.1, whose Cython extensions are optional). gcc-built
# extensions load into a clang-built CPython fine -- that is how every
# manylinux wheel works. Installing clang instead would add ~400MB of LLVM to
# every one of the per-PR OCI archives.
export CC=gcc
export CXX=g++
export LDSHARED="gcc -shared"

# pip is always driven as `python -m pip`, never via {_VENV}/bin/pip. The venv's
# bin/python3 is a symlink into /opt/pythons/<series>, and the interpreter finds
# its libpython through an $ORIGIN-relative RPATH. When a console script is
# exec'd by its `#!{_VENV}/bin/python3` shebang under qemu-user -- i.e. the
# linux/amd64 leg of the multi-arch build on an arm64 host -- the emulator
# expands $ORIGIN from the unresolved shebang path, looks for
# {_VENV}/bin/../lib/libpython3.8.so.1.0, and the interpreter dies with exit 127
# before pip ever starts. Executing the symlink directly resolves correctly, so
# `-m pip` is immune. Only the 3.8 series ships a shared libpython, but the
# indirection costs nothing on the others.
{_python_bin(line.python)} -m venv {_VENV}
{_VENV}/bin/python -m pip install --no-cache-dir --upgrade pip
{_VENV}/bin/python -m pip install --no-cache-dir {line.test_deps}
{_VENV}/bin/python -m pip install --no-cache-dir -e .

{_VENV}/bin/python -VV
{_VENV}/bin/python -c 'import sqlalchemy; print("sqlalchemy", sqlalchemy.__version__)'
""",
            ),
            File(
                ".",
                "reinstall-if-needed.sh",
                f"""#!/bin/bash
# usage: reinstall-if-needed.sh <patch> [<patch>...]
cd /home/{self.pr.repo}
{reinstall}
""",
            ),
            File(".", "run-tests.sh", run_tests),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
bash /home/run-tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{self.pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed for test.patch" >&2
    exit 1
fi
bash /home/reinstall-if-needed.sh /home/test.patch
bash /home/run-tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{self.pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed for test.patch + fix.patch" >&2
    exit 1
fi
bash /home/reinstall-if-needed.sh /home/test.patch /home/fix.patch
bash /home/run-tests.sh
""",
            ),
        ]


# Every bundle key in the LHT dataset, dash-joined exactly as the record's
# `number_interval` field: Instance.create() looks up f"{org}/{number_interval}"
# whenever that field is populated, so one key per bundle is required
# (#keys == #instances). Era selection stays data-driven via
# _BASE_COMMIT_RELEASE_LINE, so every bundle lands on its correct era config.
_NUMBER_INTERVALS = (
    "4514-4516-4520",
    "5058-5105-5141-5186",
    "5730-5733-5742-5746-5809",
    "5883-5917-5937-5943",
    "5946-5960-5972-6013-6015-6017-6057-6078",
    "6108-6110-6197-6200-6237-6246",
    "6341-6358-6362-6363-6369-6370-6381-6382-6389-6393-6395",
    "6432-6522",
    "6571-6651",
    "6699-6902-6926-6994-7003-7004-7005-7006-7014-7017-7018-7020-7043-7062",
    "6726-6751-6761",
    "6875-8595-8924-9037",
    "7114-7143-7195-7206",
    "7363-7369-7417-7439-7648-7655-7665-7669-7709-7747-7778-7824-7882-7935-7950-7978-7987-8032-8039-8063-8072-8088-8093-8117-8290-8298-8306-8308-8309-8310-8313-8331-8414-8517-8537-8581-8627-8630-8729-8736-8752-8791-8809-8898-8933-9115",
    "9079-9215-9236-9269-9279-9303-9316-9322-9334-9336-9399-9420-9421-9474-9539-9601-9612-9616-9651-9672-9674",
    "9694-9712-9751-10039-10067-10112-10185",
    "9747-9803-9842-9848-9851-9865-9926",
    "9938-9961-9980-9983-9996",
    "10060-10086-10087",
    "10215-10248-10249-10294-10304-10311-10333-10343-10352",
    "10371-10391-10416",
    "10506-10590-10599-10620-10730-10737-10746-10757-10809-10831-10842-10845-10848-10912-10918-10925-10931-10947-10973-10984-10992-11014-11035-11047-11095-11105-11120-11134-11148-11244-11248-11302-11325-11337-11474-11491-11503-11555-11561-11690-11700-11715-11717-11721-11762-11786-11799-11807-11828-11867-11884-11885-11895-11914-11947-11960-11976-12025-12034-12048-12072-12088-12129-12175-12200-12237-12242-12250-12306-12307-12453-12482-12495-12509-12526-12527-12553-12555-12567-12568-12604-12617-12621-12622-12649-12657-12713-12721-12726-12737-12751-12768-12816-12912-12925-12969-13036-13038-13047-13084",
    "10584-10585",
    "13063-13087-13100-13155-13183-13237",
)


@Instance.register("sqlalchemy", "sqlalchemy")
class SQLAlchemy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SQLAlchemyImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Under -n4, pytest-xdist prints one line per test, which is the only
        # place every status appears with a usable id:
        #     [gw2] [ 37%] PASSED test/base/test_utils.py::MiscTest::test_x
        # Ids are matched to end-of-line, not as \S+: SQLAlchemy parametrises
        # with values containing spaces, e.g. "...::test_rt[more :: %colons%]".
        verbose_pattern = re.compile(
            r"^\[gw\d+\]\s+\[\s*\d+%\]\s+"
            r"(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
            r"(test/.+?)\s*$",
            re.MULTILINE,
        )

        # -rA repeats them in the short summary without the worker prefix, which
        # is what a non-xdist run (an overridden run_cmd) would rely on.
        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(test/.+?)\s*$",
            re.MULTILINE,
        )

        # SKIPPED entries in the -rA summary point at the skip's *source*
        # location and carry the real test id in quotes, e.g.
        #     SKIPPED [1] lib/sqlalchemy/testing/config.py:420: \
        #         'test/engine/test_execute.py::ExecuteTest::test_raw (call)' : ...
        # Older releases quote a bare method name instead, which is not a usable
        # id and is therefore ignored. The closing quote must be anchored on the
        # trailing "' :" -- some parametrised ids embed an apostrophe, e.g.
        # "...::test_get_table_comment[quote ' one]".
        skip_pattern = re.compile(
            r"^SKIPPED\s+\[\d+\]\s+.*?'(test/.+?)"
            r"(?:\s+\((?:call|setup|teardown)\))?'\s*:",
            re.MULTILINE,
        )

        def record(status: str, test_id: str) -> None:
            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_id)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_id)
            else:  # SKIPPED, XFAIL
                skipped_tests.add(test_id)

        for status, test_id in verbose_pattern.findall(log):
            # " <- lib/..." is pytest's origin marker for a test defined outside
            # the collected file; it is not part of the id.
            record(status, test_id.split(" <- ")[0].strip())

        for status, test_id in summary_pattern.findall(log):
            test_id = test_id.split(" <- ")[0].strip()
            if status in ("FAILED", "ERROR"):
                # The summary appends " - <exception>" to failures. Only strip
                # it here: the verbose lines above carry no reason, and a few
                # parametrised ids legitimately contain " - ".
                test_id = re.split(r"\s+-\s+", test_id, maxsplit=1)[0].strip()
            record(status, test_id)

        skipped_tests.update(skip_pattern.findall(log))

        # A test that failed anywhere (e.g. passed in the call phase but errored
        # in teardown) counts as failed.
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


# Bundle-level registration: one key per instance, applied to the same class so
# there is no duplicate config to drift. The bare "sqlalchemy/sqlalchemy" key
# above stays as a fallback for records whose number_interval is not populated.
for _interval in _NUMBER_INTERVALS:
    Instance.register("sqlalchemy", _interval)(SQLAlchemy)
