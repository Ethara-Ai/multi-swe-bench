from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_MITM_PROXY_ARGS = (
    'ARG http_proxy=""\n'
    'ARG https_proxy=""\n'
    'ARG HTTP_PROXY=""\n'
    'ARG HTTPS_PROXY=""\n'
    'ARG no_proxy="localhost,127.0.0.1,::1"\n'
    'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
    'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
)

_MITM_ENV_BLOCK = (
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
    "    CURL_CA_BUNDLE=${CA_CERT_PATH}"
)

_BASE_ENV_BLOCK = _MITM_ENV_BLOCK + (
    " \\\n"
    "    PYTHONUNBUFFERED=1 \\\n"
    "    PYTHONDONTWRITEBYTECODE=1 \\\n"
    "    PIP_DISABLE_PIP_VERSION_CHECK=1 \\\n"
    "    PIP_NO_CACHE_DIR=1 \\\n"
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


_ORG = "AlertaDengue"
_REPO = "PySUS"
_PYTHON_IMAGE = "python:3.9-slim"
_APT_PACKAGES = "git ca-certificates build-essential libffi-dev"
_PIP_PINS = '"pip==21.0.1" "setuptools==56.0.0" "wheel==0.36.2"'
_DEPS = " ".join(
    f'"{spec}"'
    for spec in (
        "numpy==1.20.2",
        "pandas==1.2.2",
        "python-dateutil==2.8.1",
        "pytz==2021.1",
        "six==1.15.0",
        "pyarrow==4.0.0",
        "geopandas==0.9.0",
        "shapely==1.8.0",
        "pyproj==3.0.1",
        "dbfread==2.0.7",
        "cffi==1.14.5",
        "pycparser==2.20",
        "geocoder==1.38.1",
        "click==7.1.2",
        "decorator==5.0.7",
        "future==0.18.2",
        "ratelim==0.1.6",
        "requests==2.25.1",
        "chardet==4.0.0",
        "idna==2.10",
        "urllib3==1.26.4",
        "certifi==2020.12.5",
        "pytest==6.2.3",
        "attrs==20.3.0",
        "iniconfig==1.1.1",
        "packaging==20.9",
        "pyparsing==2.4.7",
        "pluggy==0.13.1",
        "py==1.10.0",
        "toml==0.10.2",
        "pytest-rerunfailures==9.1.1",
        "pytest-timeout==1.4.2",
    )
)
_FALLBACK_TEST_TARGET = "pysus/tests"
_RERUN_ON = (
    "ConnectionRefusedError|ConnectionResetError|ConnectionAbortedError|"
    "BrokenPipeError|TimeoutError|timed out|gaierror|EOFError|"
    "error_temp|error_reply|error_perm|Timeout|NoneType|not available|"
    "None is not an instance"
)

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


def _test_targets(test_patch: str) -> str:
    targets: list[str] = []
    for path in re.findall(r"^\+\+\+ b/(\S+)", test_patch, re.MULTILINE):
        name = path.rsplit("/", 1)[-1]
        is_test = name.startswith("test_") or name.endswith("_test.py")
        if path.endswith(".py") and is_test and path not in targets:
            targets.append(path)
    if not targets:
        targets.append(_FALLBACK_TEST_TARGET)
    return " ".join(shlex.quote(path) for path in targets)


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
        "COLUMNS=200 pytest $TEST_TARGETS -v -rA -p no:cacheprovider "
        "--continue-on-collection-errors --reruns 5 --reruns-delay 20 "
        f'--only-rerun "{_RERUN_ON}" --timeout 900 2>&1'
    )


class PySUSImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return "base"

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

RUN python -m pip install --no-cache-dir {_PIP_PINS}

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class PySUSImageDefault(Image):
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
        return PySUSImageBase(self.pr, self.config)

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

pip install --no-cache-dir --no-deps --prefer-binary {_DEPS}
pip install --no-cache-dir --no-deps --no-build-isolation -e .

(
{test_cmd}
) || true

git reset --hard
git clean -fd

python -c "\\
import pysus, pandas, geopandas, pyarrow, dbfread; \\
import pytest, pytest_rerunfailures, pytest_timeout; \\
from pysus.utilities.readdbc import read_dbc; \\
from pysus.online_data import CACHEPATH; \\
print('DEPS_OK')"
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


@Instance.register(_ORG, _REPO)
class PySUS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return PySUSImageDefault(self.pr, self._config)

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
            m = _VERBOSE_RE.match(line) or _SUMMARY_RE.match(line)
            if not m:
                continue
            status = m.group("status")
            if status == "RERUN":
                continue
            final_status[m.group("id").strip()] = status

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        for name, status in final_status.items():
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
