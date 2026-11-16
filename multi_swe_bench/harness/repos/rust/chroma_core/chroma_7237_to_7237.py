import os
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace("_", "-")
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_PYTHON_IMAGE = "python:3.9.25-bookworm"
_RUST_TOOLCHAIN = "1.96.0"
_PROTOC_VERSION = "35.0"
_UV = "uv==0.11.19"
_PROTO_TOOLS = ("grpcio==1.58.0", "grpcio-tools==1.58.0")
_REQUIREMENTS = ("requirements.txt", "requirements_dev.txt")
_CARGO_JOBS = 4
_INSTALL_ATTEMPTS = 3
_TEST_FILE = re.compile(r"(^|/)test_[^/]*\.py$")

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_VERBOSE_LINE = re.compile(
    r"^(\S+::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?: \(.*\))?(?:\s+\[\s*\d+%\])?$"
)
_COLLECT_ERROR = re.compile(r"^ERROR (\S+\.py)(?: - .*)?$")
_STATUS = {
    "PASSED": "pass",
    "XFAIL": "pass",
    "FAILED": "fail",
    "ERROR": "fail",
    "XPASS": "fail",
    "SKIPPED": "skip",
}

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
    return "\n\n".join(part for part in parts if part) + "\n"


def _test_files(pr: PullRequest) -> list[str]:
    paths = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({path for path in paths if _TEST_FILE.search(path)})


def _retry_block() -> str:
    return f"""retry() {{
    for attempt in $(seq 1 {_INSTALL_ATTEMPTS}); do
        if "$@"; then
            return 0
        fi
        if [ "$attempt" -eq {_INSTALL_ATTEMPTS} ]; then
            return 1
        fi
        sleep 15
    done
}}"""


def _test_block(files: list[str]) -> str:
    quoted = " ".join(shlex.quote(path) for path in files)
    return f"""tests=()
for f in {quoted}; do
    if [ -f "$f" ]; then
        tests+=("$f")
    fi
done
if [ "${{#tests[@]}}" -eq 0 ]; then
    echo "no test files from the test patch exist"
    exit 1
fi
python -m pytest -v -rA --tb=short -p no:cacheprovider --continue-on-collection-errors "${{tests[@]}}\""""


def _prepare_script(pr: PullRequest) -> str:
    repo = pr.repo
    sha = pr.base.sha
    requirements = " ".join(f"-r {name}" for name in _REQUIREMENTS)
    existing = " ".join(shlex.quote(path) for path in _test_files(pr))
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        f"export CI=true\nexport CARGO_BUILD_JOBS={_CARGO_JOBS}",
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        "python --version\nrustc --version\ncargo --version\nprotoc --version",
        _retry_block(),
        f"""retry pip install --no-cache-dir {_UV}
EXCLUDE_NEWER=$(git show -s --format=%cI HEAD)
echo "prepare: resolving Python packages published before $EXCLUDE_NEWER\"""",
        f"""retry uv pip install --system --no-cache --exclude-newer "$EXCLUDE_NEWER" {' '.join(_PROTO_TOOLS)}
make -C idl proto_python
uv pip uninstall --system {' '.join(name.split('==')[0] for name in _PROTO_TOOLS)}""",
        f'retry uv pip install --system --no-cache --exclude-newer "$EXCLUDE_NEWER" {requirements}',
        """retry maturin build --locked
uv pip install --system --no-cache --no-deps target/wheels/*.whl
rm -rf target""",
        f"""python -c "import chromadb_rust_bindings, chromadb; from chromadb.server.fastapi import FastAPI; print('prepare: chromadb ' + chromadb.__version__ + ' from ' + chromadb.__file__); assert chromadb.__file__.startswith('/home/{repo}/')"
collect=()
for f in {existing}; do
    if [ -f "$f" ]; then
        collect+=("$f")
    fi
done
test "${{#collect[@]}}" -gt 0
python -m pytest --collect-only -q -p no:cacheprovider "${{collect[@]}}\"""",
    )


def _run_script(repo: str, files: list[str], apply: str = "") -> str:
    setup = "\n".join(line for line in (f"cd /home/{repo}", apply) if line)
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        "export CI=true",
        setup,
        _test_block(files),
    )


class Chroma7237To7237ImageBase(Image):
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
        return _PYTHON_IMAGE

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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    PATH=/usr/local/cargo/bin:${{PATH}}

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl unzip make build-essential pkg-config libssl-dev clang libclang-dev cmake \\
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain {_RUST_TOOLCHAIN} && \\
    rustc --version && cargo --version

RUN case "${{TARGETARCH}}" in \\
        amd64) protoc_arch=x86_64 ;; \\
        arm64) protoc_arch=aarch_64 ;; \\
        *) echo "unsupported TARGETARCH=${{TARGETARCH}}" && exit 1 ;; \\
    esac && \\
    curl -fsSL -o /tmp/protoc.zip "https://github.com/protocolbuffers/protobuf/releases/download/v{_PROTOC_VERSION}/protoc-{_PROTOC_VERSION}-linux-${{protoc_arch}}.zip" && \\
    unzip -q /tmp/protoc.zip -d /usr/local && \\
    rm -f /tmp/protoc.zip && \\
    protoc --version

RUN git config --system --add safe.directory '*' && python --version && pip --version"""
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


class Chroma7237To7237ImageDefault(Image):
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
        return Chroma7237To7237ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        files = _test_files(self.pr)
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script(repo, files)),
            File(".", "test-run.sh", _run_script(repo, files, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, files, fix_apply)),
        ]

    def dockerfile(self) -> str:
        copies = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            copies,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _ERA)
class CHROMA_7237_TO_7237(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Chroma7237To7237ImageDefault(self.pr, self._config)

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
        buckets = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}
        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.rstrip()
            verbose = _VERBOSE_LINE.match(line)
            if verbose:
                buckets[_STATUS[verbose.group(2)]].add(verbose.group(1))
                continue
            collect_error = _COLLECT_ERROR.match(line)
            if collect_error:
                failed_tests.add(f"{collect_error.group(1)}::<collection error>")
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
