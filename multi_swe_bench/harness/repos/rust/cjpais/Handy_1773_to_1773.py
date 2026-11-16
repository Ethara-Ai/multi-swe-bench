import os
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace("_", "-")
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_UBUNTU_IMAGE = "ubuntu:noble-20260610"
_RUST_TOOLCHAIN = "1.97.1"
_CRATE_DIR = "src-tauri"
_CARGO_JOBS = 4
_INSTALL_ATTEMPTS = 3
_APT_PACKAGES = (
    "git",
    "ca-certificates",
    "curl",
    "build-essential",
    "pkg-config",
    "cmake",
    "libwebkit2gtk-4.1-dev",
    "libappindicator3-dev",
    "librsvg2-dev",
    "libasound2-dev",
    "libssl-dev",
    "libgtk-layer-shell-dev",
    "libvulkan-dev",
    "glslc",
    "spirv-headers",
)
_CARGO_TEST = "cargo test --locked --no-fail-fast"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RUNNING = re.compile(r"^\s*Running (?:unittests )?(\S+)")
_DOCTESTS = re.compile(r"^\s*Doc-tests (\S+)")
_TEST_LINE = re.compile(r"^test (.+?) \.\.\. ?(.*)$")
_STATUS_WORD = re.compile(r"^(ok|FAILED|ignored)(?:,.*)?$")
_DOC_LINE = re.compile(r"\s*\(line \d+\)$")
_STATUS = {"ok": "pass", "FAILED": "fail", "ignored": "skip"}

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


def _cargo_env() -> str:
    return "\n".join(
        (
            "export CI=true",
            f"export CARGO_BUILD_JOBS={_CARGO_JOBS}",
            "export CARGO_INCREMENTAL=0",
            "export CARGO_PROFILE_DEV_DEBUG=0",
            "export CARGO_TERM_COLOR=never",
            "export RUST_BACKTRACE=1",
        )
    )


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


def _prepare_script(pr: PullRequest) -> str:
    repo = pr.repo
    sha = pr.base.sha
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        _cargo_env(),
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        "rustc --version\ncargo --version",
        _retry_block(),
        f"""cd /home/{repo}/{_CRATE_DIR}
retry cargo fetch --locked
retry {_CARGO_TEST} --no-run""",
        f"""listed=$({_CARGO_TEST} -- --list --format terse | grep -c ': test$')
echo "prepare: $listed tests listed"
test "$listed" -gt 0""",
    )


def _run_script(repo: str, apply: str = "") -> str:
    setup = "\n".join(line for line in (f"cd /home/{repo}", apply, f"cd {_CRATE_DIR}") if line)
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _cargo_env(),
        setup,
        _CARGO_TEST,
    )


class Handy1773To1773ImageBase(Image):
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
        return _UBUNTU_IMAGE

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
        packages = " ".join(_APT_PACKAGES)
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
    {packages} \\
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain {_RUST_TOOLCHAIN} && \\
    rustc --version && cargo --version

RUN git config --system --add safe.directory '*' && glslc --version && pkg-config --modversion webkit2gtk-4.1 vulkan"""
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


class Handy1773To1773ImageDefault(Image):
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
        return Handy1773To1773ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, fix_apply)),
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
class HANDY_1773_TO_1773(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Handy1773To1773ImageDefault(self.pr, self._config)

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
        target = ""
        pending = ""
        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.rstrip()
            running = _RUNNING.match(line)
            if running:
                target, pending = running.group(1), ""
                continue
            doctests = _DOCTESTS.match(line)
            if doctests:
                target, pending = f"doctests:{doctests.group(1)}", ""
                continue
            test_line = _TEST_LINE.match(line)
            if test_line:
                name = _DOC_LINE.sub("", test_line.group(1)).strip()
                full = f"{target}::{name}" if target else name
                status = _STATUS_WORD.match(test_line.group(2).strip())
                if status:
                    buckets[_STATUS[status.group(1)]].add(full)
                    pending = ""
                else:
                    pending = full
                continue
            if pending:
                status = _STATUS_WORD.match(line.strip())
                if status:
                    buckets[_STATUS[status.group(1)]].add(pending)
                    pending = ""
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
