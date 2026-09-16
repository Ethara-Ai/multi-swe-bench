from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ORG = "pola-rs"
_REPO = "polars"
_ERA_KEY = "polars_16479_to_17111"
_BASE_TAG = "base-16479_to_17111"
_PYTHON_IMAGE = "python:3.11-slim-bookworm"
_APT_PACKAGES = "git ca-certificates build-essential cmake curl pkg-config"
_RUSTUP_VERSION = "1.27.1"
_RUSTUP_SHA256_AMD64 = "6aeece6993e902708983b209d04c0d1dbb14ebb405ddb87def578d41f920f56d"
_RUSTUP_SHA256_ARM64 = "1cffbf51e63e634c746f741de50649bbbcbd9dbe1de363c9ecef64e278dba2b2"
_UV_PIN = "uv==0.4.30"
_VENV = "/opt/venv"
_PY_DIR = "py-polars"
_FALLBACK_TEST_TARGET = "tests/unit"
_EXCLUDED_REQUIREMENTS = ("connectorx",)

_PYTEST = (
    "COLUMNS=200 python -m pytest $TEST_TARGETS -v -rA -p no:cacheprovider "
    '-m "not release and not benchmark and not docs" '
    "--hypothesis-seed=0 --continue-on-collection-errors 2>&1"
)

_GATE_PY = (
    "import polars as pl, pytest, hypothesis, numpy, pandas, pyarrow; "
    'assert pl.DataFrame({"a": [1, 2]}).select(pl.col("a").sum()).item() == 3; '
    'print("DEPS_OK")'
)

_MITM_PROXY_ARGS = (
    'ARG http_proxy=""\n'
    'ARG https_proxy=""\n'
    'ARG HTTP_PROXY=""\n'
    'ARG HTTPS_PROXY=""\n'
    'ARG no_proxy="localhost,127.0.0.1,::1"\n'
    'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
    'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
)

_BASE_ENV_BLOCK = (
    "ENV DEBIAN_FRONTEND=noninteractive \\\n"
    "    LANG=C.UTF-8 \\\n"
    "    LC_ALL=C.UTF-8 \\\n"
    "    TZ=UTC \\\n"
    "    http_proxy=${http_proxy} \\\n"
    "    https_proxy=${https_proxy} \\\n"
    "    HTTP_PROXY=${HTTP_PROXY} \\\n"
    "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
    "    no_proxy=${no_proxy} \\\n"
    "    NO_PROXY=${NO_PROXY} \\\n"
    "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
    "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    CURL_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    PIP_CERT=${CA_CERT_PATH} \\\n"
    "    CARGO_HTTP_CAINFO=${CA_CERT_PATH} \\\n"
    "    UV_NATIVE_TLS=true \\\n"
    "    UV_NO_CACHE=1 \\\n"
    "    PIP_NO_CACHE_DIR=1 \\\n"
    "    PIP_DISABLE_PIP_VERSION_CHECK=1 \\\n"
    "    PYTHONDONTWRITEBYTECODE=1 \\\n"
    "    PYTHONUNBUFFERED=1 \\\n"
    "    PY_COLORS=0 \\\n"
    "    RUSTUP_HOME=/usr/local/rustup \\\n"
    "    CARGO_HOME=/usr/local/cargo \\\n"
    '    RUSTFLAGS="-C debuginfo=0" \\\n'
    "    RUST_BACKTRACE=1 \\\n"
    "    CARGO_TERM_COLOR=never \\\n"
    f"    VIRTUAL_ENV={_VENV} \\\n"
    f"    PATH={_VENV}/bin:/usr/local/cargo/bin:$PATH \\\n"
    "    CI=true"
)

_MITM_CERT_SYMLINKS = (
    "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
)

_HARDENING_BLOCK = """RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${BASE_COMMIT}"; \\
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
    test "$(git rev-parse HEAD)" = "${BASE_COMMIT}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\""""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_VERBOSE_RE = re.compile(
    r"^(?P<id>\S+\.py::.+?)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|RERUN)"
    r"(?:\s+\(.*\))?\s*(?:\[\s*\d+%\])?\s*$"
)
_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS)\s+"
    r"(?P<id>\S+\.py::.+?)(?:\s+-\s+.*)?$"
)


def _submodule_scrub_block(repo: str) -> str:
    return f"""RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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
    fi"""


def _test_targets(test_patch: str) -> str:
    prefix = f"{_PY_DIR}/"
    targets: list[str] = []
    for path in re.findall(r"^\+\+\+ b/(\S+)", test_patch, re.MULTILINE):
        if not path.startswith(f"{prefix}tests/") or not path.endswith(".py"):
            continue
        name = path.rsplit("/", 1)[-1]
        if not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        relative = path[len(prefix):]
        if relative not in targets:
            targets.append(relative)
    if not targets:
        targets.append(_FALLBACK_TEST_TARGET)
    return " ".join(shlex.quote(path) for path in targets)


def _requirements_filter() -> str:
    names = "|".join(re.escape(name) for name in _EXCLUDED_REQUIREMENTS)
    return f"grep -vE '^[[:space:]]*({names})([^A-Za-z0-9_.-]|$)'"


def _test_command(targets: str) -> str:
    return (
        'TEST_TARGETS=""\n'
        f"for f in {targets}; do\n"
        '    if [ -e "$f" ]; then\n'
        '        TEST_TARGETS="$TEST_TARGETS $f"\n'
        "    fi\n"
        "done\n"
        'if [ -z "$TEST_TARGETS" ]; then\n'
        '    echo "No test targets present"\n'
        "    exit 0\n"
        "fi\n"
        f"{_PYTEST}"
    )


class PolarsMid2024ImageBase(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {_PYTHON_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{_MITM_PROXY_ARGS}

{_BASE_ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_MITM_CERT_SYMLINKS}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        {_APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

RUN arch="${{TARGETARCH:-$(dpkg --print-architecture)}}" \\
    && case "$arch" in \\
        amd64) triple=x86_64-unknown-linux-gnu; sum={_RUSTUP_SHA256_AMD64} ;; \\
        arm64) triple=aarch64-unknown-linux-gnu; sum={_RUSTUP_SHA256_ARM64} ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac \\
    && curl --proto '=https' --tlsv1.2 -fsSL \\
        "https://static.rust-lang.org/rustup/archive/{_RUSTUP_VERSION}/$triple/rustup-init" -o /tmp/rustup-init \\
    && echo "$sum  /tmp/rustup-init" | sha256sum -c - \\
    && chmod +x /tmp/rustup-init \\
    && /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none \\
    && rm -f /tmp/rustup-init \\
    && rustup set profile minimal

RUN python -m pip install --no-cache-dir "{_UV_PIN}" \\
    && python -m venv {_VENV} \\
    && uv --version

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class PolarsMid2024ImageDefault(Image):
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
        return PolarsMid2024ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        test_cmd = _test_command(_test_targets(self.pr.test_patch))

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "ERROR: /home/{repo} is not a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is not clean:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "GIT_TREE_CLEAN"
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

git config --global --add safe.directory '*'

cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

rustup show
rustup default "$(rustup show active-toolchain | cut -d' ' -f1)"

EXCLUDE_NEWER="$(TZ=UTC git log -1 --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd HEAD)"
{_requirements_filter()} {_PY_DIR}/requirements-dev.txt > /tmp/requirements-dev.txt
uv pip install --python {_VENV}/bin/python --exclude-newer "$EXCLUDE_NEWER" -r /tmp/requirements-dev.txt

cd /home/{repo}/{_PY_DIR}
maturin develop

rustc --version > /dev/null
cargo --version > /dev/null
python -c '{_GATE_PY}'
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fd

cd /home/{repo}/{_PY_DIR}
maturin develop

{test_cmd}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch

cd /home/{repo}/{_PY_DIR}
maturin develop

{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

cd /home/{repo}/{_PY_DIR}
maturin develop

{test_cmd}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        repo = self.pr.repo

        sections = [
            f"FROM {dep.image_name()}:{dep.image_tag()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            f"WORKDIR /home/{repo}",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "COPY fix.patch /home/\n"
            "COPY test.patch /home/\n"
            "COPY check_git_changes.sh /home/\n"
            "COPY prepare.sh /home/\n"
            "COPY run.sh /home/\n"
            "COPY test-run.sh /home/\n"
            "COPY fix-run.sh /home/"
        )

        sections.append("RUN bash /home/prepare.sh")
        sections.append(_HARDENING_BLOCK)
        sections.append(_submodule_scrub_block(repo))

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register(_ORG, _ERA_KEY)
class POLARS_16479_TO_17111(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return PolarsMid2024ImageDefault(self.pr, self._config)

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
        final_status: dict[str, str] = {}
        for raw in _ANSI_RE.sub("", log).splitlines():
            line = raw.strip()
            match = _VERBOSE_RE.match(line) or _SUMMARY_RE.match(line)
            if not match or match.group("status") == "RERUN":
                continue
            final_status[match.group("id").strip()] = match.group("status")

        passed_tests: set[str] = {n for n, s in final_status.items() if s in ("PASSED", "XPASS")}
        failed_tests: set[str] = {n for n, s in final_status.items() if s in ("FAILED", "ERROR")}
        skipped_tests: set[str] = {n for n, s in final_status.items() if s in ("SKIPPED", "XFAIL")}

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
