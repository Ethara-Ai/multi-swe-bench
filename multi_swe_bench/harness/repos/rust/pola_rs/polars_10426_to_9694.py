import posixpath
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-10426_to_9694"
_RUSTUP_VERSION = "1.28.2"
_UV_VERSION = "0.12.14"
_EXCLUDED_REQUIREMENTS = "connectorx"


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:${PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cmake curl git pkg-config \
    && rm -rf /var/lib/apt/lists/*

__GLOBAL_ENV__

WORKDIR /home/

__CODE__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


_CLONE_CODE = r"""RUN git clone "${REPO_URL}" /home/__REPO__"""


_COPY_CODE = r"""COPY __REPO__ /home/__REPO__"""


_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

ARG BASE_COMMIT="__BASE_SHA__"

__GLOBAL_ENV__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

WORKDIR /home/__REPO__

__HARDENING__
__CLEAR_ENV__
"""


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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


_SHELL_ENV = r"""export CI=true
export VIRTUAL_ENV=/opt/venv
export PATH="/opt/venv/bin:${PATH}"
export RUSTFLAGS="-C debuginfo=0"
export CARGO_INCREMENTAL=0
export CARGO_TERM_COLOR=never
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export RUST_BACKTRACE=1"""


_BUILD = r"""maturin develop"""


_APPLY_BLOCK = r"""patch_excludes=()
for entry in __BINARY_FILES__; do
    patch_excludes+=("--exclude=${entry%|*}")
done

git apply --whitespace=nowarn "${patch_excludes[@]}" __PATCHES__

for entry in __BINARY_FILES__; do
    path="${entry%|*}"
    blob="${entry##*|}"
    case "${blob}" in
        *[!0]*)
            mkdir -p "$(dirname "${path}")"
            cp "/home/patch_blobs/${path}" "${path}"
            ;;
        *)
            rm -f "${path}"
            ;;
    esac
done"""


_TEST_BLOCK = r"""test_targets=()
for test_target in __TEST_TARGETS__; do
    if [ -e "${test_target}" ]; then
        test_targets+=("${test_target}")
    fi
done

if [ "${#test_targets[@]}" -eq 0 ]; then
    echo "run: none of the test targets exist at this revision"
    exit 0
fi

python -m pytest -n auto --dist loadgroup -m "not benchmark and not docs" --continue-on-collection-errors -v -rA --tb=no --color=no -p no:cacheprovider "${test_targets[@]}"
"""


_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

arch="${TARGETARCH:-$(dpkg --print-architecture)}"
case "${arch}" in amd64) rust_arch=x86_64 ;; arm64) rust_arch=aarch64 ;; *) echo "unsupported architecture ${arch}" >&2; exit 1 ;; esac
curl -fsSL "https://static.rust-lang.org/rustup/archive/__RUSTUP_VERSION__/${rust_arch}-unknown-linux-gnu/rustup-init" -o /tmp/rustup-init
chmod +x /tmp/rustup-init
/tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none
rm -f /tmp/rustup-init
rustup toolchain list

python -m venv /opt/venv
/opt/venv/bin/python -m pip install "uv==__UV_VERSION__"
/opt/venv/bin/uv --version

__SHELL_ENV__

mkdir -p /home/patch_blobs
for entry in __BINARY_FILES__; do
    path="${entry%|*}"
    blob="${entry##*|}"
    case "${blob}" in
        *[!0]*)
            mkdir -p "$(dirname "/home/patch_blobs/${path}")"
            git cat-file blob "${blob}" > "/home/patch_blobs/${path}"
            ;;
    esac
done

toolchain="$(sed -n 's/^channel *= *"\([^"]*\)".*/\1/p' rust-toolchain.toml)"
test -n "${toolchain}"
rustup toolchain install "${toolchain}" --profile minimal
rustc --version
cargo --version

test "$(command -v python)" = "/opt/venv/bin/python"
uv --version

cutoff="$(git show -s --format=%cI HEAD)"

cd /home/__REPO__/py-polars

echo "prepare: not installing __EXCLUDED_REQUIREMENTS__ - no Linux arm64 wheels exist for this era, so it is excluded on every architecture to keep results identical"
grep -vE '^[[:space:]]*(--|(__EXCLUDED_REQUIREMENTS__)([^A-Za-z0-9_.-]|$))' requirements-dev.txt > /tmp/requirements-dev.txt
uv pip install --compile-bytecode --exclude-newer "${cutoff}" -r /tmp/requirements-dev.txt
rm -f /tmp/requirements-dev.txt

fix_requirements=(__FIX_REQUIREMENTS__)
if [ "${#fix_requirements[@]}" -gt 0 ]; then
    uv pip freeze | grep -viE '^(__FIX_REQUIREMENT_NAMES__)==' > /tmp/constraints.txt
    uv pip install --compile-bytecode --resolution lowest-direct -c /tmp/constraints.txt "${fix_requirements[@]}"
    rm -f /tmp/constraints.txt
fi

cargo fetch --locked

__BUILD__

uv cache clean

python -c "import polars, pytest, xdist, hypothesis, numpy, pandas, pyarrow; print('polars', polars.__version__)"
python -m pytest --version
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

__SHELL_ENV__

cd /home/__REPO__/py-polars

__TEST_BLOCK__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

__SHELL_ENV__

__APPLY_BLOCK__

cd /home/__REPO__/py-polars

__TEST_BLOCK__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

__SHELL_ENV__

__APPLY_BLOCK__

cd /home/__REPO__/py-polars

__BUILD__

__TEST_BLOCK__"""


def _sections(patch: str) -> list[str]:
    return re.split(r"(?=^diff --git )", patch or "", flags=re.M)


def _test_targets(pr: PullRequest) -> str:
    files = {}
    for section in _sections(pr.test_patch):
        match = re.match(r"diff --git a/\S+ b/py-polars/(tests/\S+)\n", section)
        if match and re.search(r"(^|/)test_[^/]*\.py$", match.group(1)):
            header = section.split("\n@@", 1)[0]
            files[match.group(1)] = files.get(match.group(1), True) and "\nnew file mode" in header
    if files and all(files.values()):
        return " ".join(shlex.quote(path) for path in sorted({posixpath.dirname(path) for path in files}))
    return " ".join(shlex.quote(path) for path in sorted(files))


def _binary_files(*patches: str) -> str:
    entries = []
    for patch in patches:
        for section in _sections(patch):
            header = re.match(r"diff --git a/\S+ b/(\S+)\n", section)
            blob = re.search(r"^index [0-9a-f]+\.\.([0-9a-f]+)(?: \d+)?\nBinary files ", section, flags=re.M)
            if header and blob:
                entries.append(shlex.quote(f"{header.group(1)}|{blob.group(1)}"))
    return " ".join(entries)


def _fix_requirements(pr: PullRequest) -> list[str]:
    requirements = []
    for section in _sections(pr.fix_patch):
        if not re.match(r"diff --git a/\S+ b/py-polars/requirements-dev\.txt\n", section):
            continue
        for line in section.split("\n@@", 1)[-1].splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            requirement = line[1:].split("#", 1)[0].strip()
            if not requirement or requirement.startswith("-"):
                continue
            if re.match(rf"({_EXCLUDED_REQUIREMENTS})([^A-Za-z0-9_.-]|$)", requirement, flags=re.I):
                continue
            requirements.append(requirement)
    return requirements


def _requirement_names(requirements: list[str]) -> str:
    names = set()
    for requirement in requirements:
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        if name:
            names.add(re.sub(r"[-_.]+", "[-_.]", name.group(0)))
    return "|".join(sorted(names))


class PolarsImageBase_10426_TO_9694(Image):

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
        return "python:3.11.4-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        code = _CLONE_CODE if self.config.need_clone else _COPY_CODE

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class PolarsImageDefault_10426_TO_9694(Image):

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
        return PolarsImageBase_10426_TO_9694(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str, patches: str = "", binary_files: str = "") -> str:
        fix_requirements = _fix_requirements(self.pr)
        return (
            template.replace("__SHELL_ENV__", _SHELL_ENV)
            .replace("__RUSTUP_VERSION__", _RUSTUP_VERSION)
            .replace("__UV_VERSION__", _UV_VERSION)
            .replace("__APPLY_BLOCK__", _APPLY_BLOCK)
            .replace("__BUILD__", _BUILD)
            .replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__PATCHES__", patches)
            .replace("__BINARY_FILES__", binary_files)
            .replace("__EXCLUDED_REQUIREMENTS__", _EXCLUDED_REQUIREMENTS)
            .replace("__FIX_REQUIREMENTS__", " ".join(shlex.quote(r) for r in fix_requirements))
            .replace("__FIX_REQUIREMENT_NAMES__", _requirement_names(fix_requirements))
            .replace("__TEST_TARGETS__", _test_targets(self.pr))
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        test_binaries = _binary_files(self.pr.test_patch)
        all_binaries = _binary_files(self.pr.test_patch, self.pr.fix_patch)
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH, binary_files=all_binaries)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(
                ".",
                "test-run.sh",
                self._render(_TEST_RUN_SH, "/home/test.patch", test_binaries),
            ),
            File(
                ".",
                "fix-run.sh",
                self._render(_FIX_RUN_SH, "/home/test.patch /home/fix.patch", all_binaries),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace(" --aggressive", "")

        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__HARDENING__", hardening)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_XDIST_RE = re.compile(
    r"^\[gw\d+\] \[\s*\d+%\] (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+\.py::\S.*)$"
)
_VERBOSE_RE = re.compile(
    r"^(\S+\.py::\S.*?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?: +\[\s*\d+%\])?$"
)
_SUMMARY_RE = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (\S+\.py(?:::\S.*?)?)(?: - .*)?$")


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(name: str, status: str) -> None:
        if "::" not in name:
            name = f"{name}::<collection error>"
        if status in ("PASSED", "XFAIL", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        elif status == "SKIPPED":
            skipped_tests.add(name)

    xdist_seen = False
    summary_entries = []

    for raw_line in _ANSI_RE.sub("", test_log).replace("\r\n", "\n").splitlines():
        line = raw_line.strip()

        match = _XDIST_RE.match(line)
        if match:
            record(match.group(2), match.group(1))
            xdist_seen = True
            continue

        match = _VERBOSE_RE.match(line)
        if match:
            record(match.group(1), match.group(2))
            continue

        match = _SUMMARY_RE.match(line)
        if match:
            summary_entries.append((match.group(2), match.group(1)))

    for name, status in summary_entries:
        if xdist_seen and "::" in name:
            continue
        record(name, status)

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


@Instance.register("pola-rs", "polars_10426_to_9694")
class POLARS_10426_TO_9694(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PolarsImageDefault_10426_TO_9694(self.pr, self._config)

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
        return _parse_log(test_log)
