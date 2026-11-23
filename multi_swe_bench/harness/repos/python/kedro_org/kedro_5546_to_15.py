"""kedro-org/kedro, PRs #15-#5546 (base commits 2019-07 .. 2026-05), one shared base.

Era analysis (clone checked at every base.sha in the JSONL):
  * #15 (kedro 0.14, 2019): python_requires <3.8, requirements.txt + test_requirements.txt,
    pytest 3.x (no `-rA` summary; parsed from `-v` lines), pyspark 2.x (Java 8, installed
    by prepare.sh for that era only).
  * #1355-#2642 (kedro 0.17/0.18, 2022-23): Python <=3.10, requirements.txt /
    dependency/requirements.txt + test_requirements.txt, pytest 6/7, pyspark 3.x (Java 11).
  * #3065 (2023-09): setup.py `test` extra, Python <=3.11.
  * #3253-#5546 (kedro 0.19/1.x, 2023-26): pyproject `[test]` extra, Python 3.8-3.14.
The test runner (pytest over `tests/`) and the repo layout hold across all eras; what
differs (Python version, requirement source, era pins) is resolved per commit in
prepare.sh from the checked-out tree, with uv's resolution cut off at the commit date.

Known limits (arm64 build host): the 2022 tensorflow pins and pyarrow 0.12 (#15) have no
build here, so their test modules error identically in every stage. #15 and #4212 yield no
fail-to-pass test: their test patches' tests already pass on the base commit.
"""
import os
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace("_", "-")
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_PY_IMAGE = "python:3.12.11-bookworm"
_UV_VERSION = "0.13.8"
# uv's static musl release binaries (sha256 from the GitHub release). The glibc build that
# pip installs segfaults under qemu-user amd64 emulation, which multi-arch builds on an
# arm64 host use; the musl build runs everywhere and still selects manylinux wheels and
# glibc managed Pythons, since uv detects the libc at runtime.
_UV_MUSL_SHA256 = {
    "x86_64": "61fb26dabc4f0cf1d050fc6c67d40e43e4fae2994af21ebc1e050b233bf2b5ed",
    "aarch64": "0c6f8593fcf1eadfc60e663b685c408267559d7c5d61c689bbb9f3a7e369ba32",
}
_VENV = "/opt/mswe/venv"
_PY37 = "3.7.17"
_PY37_PREFIX = "/opt/cpython-3.7"
_PY37_SHA256 = "fd50161bc2a04f4c22a0971ff0f3856d98b4bf294f89740a9f06b520aae63b49"
_PANDAS_024_SHA256 = "4f919f409c433577a501e023943e582c57355d50a724c589e78bc1d551a535a2"
_PYTEST = "python -m pytest -v -rA -p no:cacheprovider --no-cov --continue-on-collection-errors tests"

# Lists the requirements of kedro's `test` extra (pyproject or setup.py) from the
# installed metadata, with the `extra == "test"` marker clause removed.
_LIST_TEST_EXTRA = r"""import importlib.metadata as md
import re

marker = re.compile(r"\s*(?:and\s+)?extra\s*==\s*[\"']test[\"']")
for req in md.requires("kedro") or []:
    if marker.search(req):
        print(marker.sub("", req).strip().rstrip(";").strip())
"""

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# A pytest node id; a parametrize id may itself contain spaces or " - ".
_NODE = r"(tests/[^\s\[]+(?:\[.*?\])?)"
# `-rA` short summary (pytest >= 5): "PASSED tests/x.py::t", "FAILED tests/x.py::t - msg".
_SUMMARY = re.compile(rf"^(PASSED|XPASS|FAILED|ERROR|XFAIL) {_NODE}(?= - |\s*$)", re.M)
# `-v` per-test lines, the only per-test record pytest < 5 prints (the 2019 era runs
# pytest 3.10, which ignores `-rA`): "tests/x.py::t PASSED   [ 10%]".
_VERBOSE = re.compile(
    rf"^{_NODE} (PASSED|XPASS|FAILED|ERROR|XFAIL)(?=\s+\[\s*\d+%\]\s*$|\s*$)", re.M
)
_STATUS = {"PASSED": "pass", "XPASS": "pass", "FAILED": "fail", "ERROR": "fail", "XFAIL": "skip"}

_CHECK_GIT_CHANGES = """#!/bin/bash
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


def _sections(*parts: str) -> str:
    return "\n\n".join(filter(None, parts)) + "\n"


def _python_env() -> str:
    return "\n".join(
        (
            "export CI=true",
            f"export VIRTUAL_ENV={_VENV}",
            f"export PATH={_VENV}/bin:$PATH",
        )
    )


_REQ_FILE = re.compile(r"^diff --git a/(\S*requirements[^/\s]*\.txt) ", re.M)


def _added_requirements(pr: PullRequest) -> list[str]:
    """Requirement lines the PR's own patches add to its pip requirement files.

    The PR may depend on a package its base commit did not declare (e.g. #1706 adds
    `importlib_resources`). Only the names are rendered into prepare.sh; neither patch
    is applied or read inside the image.
    """
    added: list[str] = []
    for patch in (pr.test_patch, pr.fix_patch):
        chunks = re.split(r"^(?=diff --git )", patch or "", flags=re.M)
        for chunk in chunks:
            if not _REQ_FILE.match(chunk):
                continue
            for line in chunk.splitlines():
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                req = re.sub(r"\s+#.*$", "", line[1:]).strip()
                if req and not req.startswith(("#", "-")) and req not in added:
                    added.append(req)
    return added


_DEP_FILE = re.compile(
    r"^diff --git a/(pyproject\.toml|setup\.py|\S*requirements[^/\s]*\.txt) ", re.M
)
_PLUGIN_NAME = re.compile(r"""^[\s"']*(kedro[-_][A-Za-z0-9_.-]+)""")


def _removed_plugins(pr: PullRequest) -> list[str]:
    """kedro plugins (`kedro-*`) the PR's patches drop from its declared dependencies.

    kedro loads plugins through entry points, so an installed plugin changes the
    behaviour of the post-fix code even though no source imports it (#4607 removes
    `kedro-telemetry`, whose hooks import a module the same fix deletes). Ordinary
    libraries a fix stops using stay installed: the base code still imports them.
    """
    removed: list[str] = []
    kept: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for chunk in re.split(r"^(?=diff --git )", patch or "", flags=re.M):
            if not _DEP_FILE.match(chunk):
                continue
            for line in chunk.splitlines():
                if line.startswith(("+++", "---")) or line[:1] not in "+-":
                    continue
                m = _PLUGIN_NAME.match(line[1:])
                if not m:
                    continue
                name = re.split(r"[<>=~!;\[\s\"']", m.group(1))[0].lower().replace("_", "-")
                if line[0] == "-":
                    removed.append(name)
                else:
                    kept.add(name)
    return [name for name in dict.fromkeys(removed) if name not in kept]


def _prepare_script(pr: PullRequest) -> str:
    repo = pr.repo
    sha = pr.base.sha
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        _python_env(),
        "export UV_PYTHON_PREFERENCE=only-managed\nexport UV_CACHE_DIR=/tmp/mswe-uv-cache",
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        "uv --version\ngit --version",
        f"""export UV_EXCLUDE_NEWER=$(git log -1 --format=%cd --date=iso-strict HEAD)
# Old setuptools (<64) has no PEP 660 hook, so editable installs of pre-2023 commits
# need a newer build backend; never older than the commit's own era.
setuptools_day=$(git log -1 --format=%cs HEAD)
[[ "$setuptools_day" > 2023-01-01 ]] || setuptools_day=2023-01-01
uvflags=(--exclude-newer-package "setuptools=${{setuptools_day}}T00:00:00Z")

# Interpreter: the newest CPython (capped at 3.12) the commit declares, via trove
# classifiers (<=2023) or the CI test matrix (2023+).
py_sources() {{
    for f in pyproject.toml setup.py; do
        if [ -f "$f" ]; then sed -nE "s/.*Python :: (3\\.[0-9]+).*/\\1/p" "$f"; fi
    done
    f=.github/workflows/all-checks.yml
    if [ -f "$f" ]; then sed -nE "/python-version: *\\[/p" "$f" | grep -oE "3\\.[0-9]+"; fi
}}
py=$(py_sources | sort -uV | awk -F. '$2 <= 12' | tail -n 1)
test -n "$py"
if [ "$py" = "3.7" ]; then
    uv venv --python {_PY37_PREFIX}/bin/python3.7 {_VENV}
else
    uv venv --python "cpython-$py" {_VENV}
fi
python --version

if [ "$py" = "3.7" ]; then
    # pandas 0.24 (the 2019 pin) declares `pytz >= 2011k`, a legacy version string uv
    # refuses to parse. Build that release from its sdist with the specifier normalised;
    # its setup.py (like tables 3.5's) imports numpy/Cython, so those come first, at era
    # versions. atomicwrites' 2019 releases were re-uploaded in 2022, so the era cutoff
    # hides every version that pytest 3.x needs.
    export SETUPTOOLS_USE_DISTUTILS=stdlib
    # cryptography 2.x (moto, azure) cannot compile against OpenSSL 3; 3.3 is the first
    # release with prebuilt arm64 wheels.
    uvflags+=(--exclude-newer-package "atomicwrites=2022-07-09T00:00:00Z")
    uvflags+=(--exclude-newer-package "cryptography=2021-02-08T00:00:00Z")
    uv pip install "${{uvflags[@]}}" setuptools wheel "numpy>=1.12,<1.17" "cython>=0.28.2,<0.30"
    curl -fsSL -o /opt/mswe/pandas-0.24.2.tar.gz https://files.pythonhosted.org/packages/source/p/pandas/pandas-0.24.2.tar.gz
    echo "{_PANDAS_024_SHA256}  /opt/mswe/pandas-0.24.2.tar.gz" | sha256sum -c -
    tar -xzf /opt/mswe/pandas-0.24.2.tar.gz -C /opt/mswe
    sed -i "s/'pytz >= 2011k'/'pytz >= 2011'/" /opt/mswe/pandas-0.24.2/setup.py
    grep -q "'pytz >= 2011'" /opt/mswe/pandas-0.24.2/setup.py
    echo "pandas @ file:///opt/mswe/pandas-0.24.2" > /opt/mswe/overrides.txt
    uvflags+=(--override /opt/mswe/overrides.txt --no-build-isolation-package pandas --no-build-isolation-package tables)
fi

# pyspark 2.x (the 2019 pin) runs only on Java 8; the base's Java 11 serves pyspark 3.x.
# Era-specific, so it is installed here (from the Adoptium repo the base configures).
if grep -qiE "^pyspark.*<[[:space:]]*3" test_requirements.txt requirements.txt 2>/dev/null; then
    apt-get update
    apt-get install -y --no-install-recommends temurin-8-jre
    rm -rf /var/lib/apt/lists/*
    jre8=$(find /usr/lib/jvm -maxdepth 1 -name "temurin-8-jre-*" | head -n 1)
    test -n "$jre8"
    update-alternatives --set java "$jre8/bin/java"
    java -version 2>&1 | grep -q '"1\\.8\\.'
fi

# Prints one requirement per line from a pip requirements file (no comments, no -r).
reqs_of() {{ grep -vE "^[[:space:]]*(#|-|$)" "$1" | sed -E "s/[[:space:]]+#.*$//"; }}

if [ -f test_requirements.txt ]; then
    requirements=(-e . -r test_requirements.txt)
else
    requirements=(-e ".[test]")
fi
if ! uv pip install "${{uvflags[@]}}" "${{requirements[@]}}"; then
    # Some era pins (tensorflow, pyspark, pyarrow, tables, ...) have no build for this
    # platform. Install the project, then each requirement on its own; the gate below
    # re-checks everything the test command needs.
    echo "prepare: full requirement install failed; installing per requirement"
    if uv pip install "${{uvflags[@]}}" -e .; then
        anchor=(-e .)
        reqs=""
    else
        echo "prepare: kedro's own pins do not all resolve; installing them one by one"
        uv pip install "${{uvflags[@]}}" --no-deps -e .
        anchor=()
        core=requirements.txt
        [ -f "$core" ] || core=dependency/requirements.txt
        reqs=$(reqs_of "$core")
    fi
    if [ -f test_requirements.txt ]; then
        reqs+=$'\\n'$(reqs_of test_requirements.txt)
    else
        reqs+=$'\\n'$(python /home/list_test_extra.py)
    fi
    # Only requirements with no build for this platform/era may be skipped: the 2022
    # tensorflow pins (no arm64 wheel) and pyarrow 0.12 (2019, needs a C++ toolchain
    # build of Arrow). Anything else failing to install fails the image.
    unbuildable="tensorflow pyarrow"
    while IFS= read -r req; do
        [ -n "$req" ] || continue
        if ! uv pip install "${{uvflags[@]}}" "${{anchor[@]}}" "$req"; then
            name=$(sed -E "s/^([A-Za-z0-9_.-]+).*/\\1/" <<< "$req" | tr "[:upper:]_" "[:lower:]-")
            case " $unbuildable " in
                *" $name "*) echo "prepare: skipped unbuildable requirement: $req" ;;
                *) echo "prepare: required package failed to install: $req"; exit 1 ;;
            esac
        fi
    done <<< "$reqs"
fi""",
        _added_requirements_section(pr),
        _removed_plugins_section(pr),
        # The download cache is several GB per image and is not needed after provisioning.
        'rm -rf "$UV_CACHE_DIR"',
        """python -c "import kedro, pytest, pytest_cov; print('kedro', kedro.__version__, 'pytest', pytest.__version__)"
# Same collection mode as the graded command: a module whose optional dependency has no
# build here is reported as an error, but pytest itself must work (exit 0/1, never
# 2-5) and must collect tests.
status=0
python -m pytest --collect-only -q -p no:cacheprovider --no-cov --continue-on-collection-errors tests > /tmp/mswe-collect.log 2>&1 || status=$?
sed -n '/^ERROR /p' /tmp/mswe-collect.log
tail -n 1 /tmp/mswe-collect.log
test "$status" -le 1
collected=$(grep -c "::" /tmp/mswe-collect.log)
echo "prepare: $collected tests collected"
test "$collected" -gt 0""",
    )


def _added_requirements_section(pr: PullRequest) -> str:
    reqs = _added_requirements(pr)
    if not reqs:
        return ""
    quoted = " ".join(shlex.quote(req) for req in reqs)
    return (
        "# Requirements this PR adds to its requirement files; installed at build time\n"
        "# so the graded stages run offline against the PR's full dependency set.\n"
        f'uv pip install "${{uvflags[@]}}" {quoted}'
    )


def _removed_plugins_section(pr: PullRequest) -> str:
    plugins = _removed_plugins(pr)
    if not plugins:
        return ""
    return (
        "# kedro plugins this PR removes from its dependencies (auto-loaded via entry points)\n"
        f"uv pip uninstall {' '.join(shlex.quote(p) for p in plugins)}"
    )


_BINARY_STUB = re.compile(r"^Binary files (\S+) and (\S+) differ$", re.M)


def _binary_excludes(pr: PullRequest) -> str:
    """`--exclude` args for binary entries the JSONL patches carry without data.

    A `Binary files a/x and b/x differ` stub has no content, so `git apply` rejects the
    whole patch (#1355 adds/removes PNG images). Those paths are skipped explicitly.
    """
    paths: list[str] = []
    for patch in (pr.test_patch, pr.fix_patch):
        for old, new in _BINARY_STUB.findall(patch or ""):
            for side in (old, new):
                if side != "/dev/null" and side[2:] not in paths:
                    paths.append(side[2:])
    return "".join(f" --exclude={shlex.quote(path)}" for path in paths)


def _run_script(repo: str, apply: str = "") -> str:
    setup = "\n".join(filter(None, (f"cd /home/{repo}", apply)))
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _python_env(),
        setup,
        _PYTEST,
    )


class Kedro5546To15ImageBase(Image):
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
        return _PY_IMAGE

    def image_tag(self) -> str:
        hi, lo = re.search(r"_(\d+)_to_(\d+)$", _ERA).groups()
        return f"base-{lo}_to_{hi}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    LC_ALL=C.UTF-8 \\
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
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential pkg-config libhdf5-dev libgdal-dev libgl1 libglib2.0-0 && \\
    rm -rf /var/lib/apt/lists/*

# Java 11 for the pyspark 3.x test suites (2022-2023 eras): Spark 3.2 cannot start on the
# Debian-default Java 17, and bookworm ships no Java 11. Adoptium publishes amd64 + arm64.
RUN curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public | gpg --dearmor -o /usr/share/keyrings/adoptium.gpg && \\
    echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb bookworm main" > /etc/apt/sources.list.d/adoptium.list && \\
    apt-get update && apt-get install -y --no-install-recommends temurin-11-jre && \\
    rm -rf /var/lib/apt/lists/* && java -version 2>&1 | grep -q '"11\\.'

# CPython 3.7 for the 2019 era (python_requires <3.8); uv ships no managed 3.7 on arm64.
RUN curl -fsSL -o /tmp/py37.tgz "https://www.python.org/ftp/python/{_PY37}/Python-{_PY37}.tgz" && \\
    echo "{_PY37_SHA256}  /tmp/py37.tgz" | sha256sum -c - && \\
    tar -xzf /tmp/py37.tgz -C /tmp && rm /tmp/py37.tgz && \\
    cd /tmp/Python-{_PY37} && ./configure --prefix={_PY37_PREFIX} --without-ensurepip >/dev/null && \\
    make -j"$(nproc)" >/dev/null && make altinstall >/dev/null && \\
    cd / && rm -rf /tmp/Python-{_PY37} && {_PY37_PREFIX}/bin/python3.7 -c "import ssl, sqlite3, bz2, lzma, ctypes, zlib"

RUN arch="$(uname -m)" && \\
    case "$arch" in \\
        x86_64) sha="{_UV_MUSL_SHA256['x86_64']}" ;; \\
        aarch64) sha="{_UV_MUSL_SHA256['aarch64']}" ;; \\
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \\
    esac && \\
    curl -fsSL -o /tmp/uv.tgz "https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}/uv-$arch-unknown-linux-musl.tar.gz" && \\
    echo "$sha  /tmp/uv.tgz" | sha256sum -c - && \\
    tar -xzf /tmp/uv.tgz -C /tmp && install -m 0755 /tmp/uv-$arch-unknown-linux-musl/uv /tmp/uv-$arch-unknown-linux-musl/uvx /usr/local/bin/ && \\
    rm -rf /tmp/uv.tgz /tmp/uv-$arch-unknown-linux-musl && \\
    uv --version | grep -qx "uv {_UV_VERSION}.*" && \\
    git config --system --add safe.directory '*' && python3 --version && uv --version && git --version"""
        clone = f"""WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null"""
        return _sections(
            header,
            self.global_env,
            clone,
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class Kedro5546To15ImageDefault(Image):
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
        return Kedro5546To15ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        exclude = _binary_excludes(self.pr)
        test_apply = f"git apply --whitespace=nowarn{exclude} /home/test.patch"
        fix_apply = f"git apply --whitespace=nowarn{exclude} /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "list_test_extra.py", _LIST_TEST_EXTRA),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, fix_apply)),
        ]

    def dockerfile(self) -> str:
        names = " ".join(map(lambda file: file.name, self.files()))
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            f"COPY {names} /home/",
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _ERA)
class KEDRO_5546_TO_15(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Kedro5546To15ImageDefault(self.pr, self._config)

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
        test_log = _ANSI.sub("", test_log)
        records = [(status, node) for status, node in _SUMMARY.findall(test_log)]
        if not records:
            records = [(status, node) for node, status in _VERBOSE.findall(test_log)]
        by_status: dict[str, set[str]] = {"pass": set(), "fail": set(), "skip": set()}
        for status, node in records:
            by_status[_STATUS[status]].add(node)
        failed_tests = by_status["fail"]
        passed_tests = by_status["pass"] - failed_tests
        skipped_tests = by_status["skip"] - failed_tests - passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
