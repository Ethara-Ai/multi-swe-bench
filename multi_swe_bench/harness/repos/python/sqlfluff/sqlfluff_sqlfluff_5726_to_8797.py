import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "sqlfluff_sqlfluff_5726_to_8797"
_BASE_TAG = f"base-{_INTERVAL}"
_PYTHON_IMAGE = "python:3.12.15-slim-bookworm"
_PIP_VERSION = "26.2.2"
_PLUGIN_DIR = "plugins/sqlfluff-plugin-example"
_SUITE_TARGETS = f"test {_PLUGIN_DIR}/test"
_DOCTEST_TARGETS = "src"
_PYTEST_FLAGS = "-v -p no:cacheprovider -n 4 --continue-on-collection-errors --tb=short"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null
changes="$(git status --porcelain)"
if [ -n "$changes" ]; then
    echo "check_git_changes: uncommitted changes"
    echo "$changes"
    exit 1
fi
echo "check_git_changes: clean"
"""

_PREPARE_SH = """#!/bin/bash
set -e

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}"; then
    git fetch --no-tags origin __SHA__
fi
git checkout --detach __SHA__
rm -f .git/ORIG_HEAD .git/FETCH_HEAD
bash /home/check_git_changes.sh

export CI=true
export SQLFLUFF_TESTENV=1
cutoff="$(git show -s --format=%cI HEAD)"
sed '/^types-/d' requirements_dev.txt > /tmp/requirements_dev.txt
python -m pip install --uploaded-prior-to "$cutoff" -r /tmp/requirements_dev.txt || true
python -m pip install --uploaded-prior-to "$cutoff" setuptools wheel pytest pytest-xdist pytest-cov coverage hypothesis || true
python -m pip install --uploaded-prior-to "$cutoff" --no-build-isolation -e . -e __PLUGIN__ || true

python -m pytest --collect-only -qq -p no:cacheprovider __SUITE__
python -m pytest --collect-only -qq -p no:cacheprovider --doctest-modules __DOCTEST__
python - <<'PY'
import importlib.metadata

import coverage
import hypothesis
import pytest
import pytest_cov
import setuptools
import xdist

import sqlfluff
import sqlfluff_plugin_example
from sqlfluff.core import Linter

assert sqlfluff.__file__.startswith("/home/__REPO__/src/"), sqlfluff.__file__
assert "sqlfluff_example" in importlib.metadata.entry_points(group="sqlfluff").names
assert Linter(dialect="ansi").parse_string("select 1\\n").tree is not None
print("DEPS_OK")
PY
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export SQLFLUFF_TESTENV=1
cd /home/__REPO__
__APPLY__
if ! git diff --quiet HEAD -- pyproject.toml __PLUGIN__; then
    python -m pip install --no-index --no-deps --no-build-isolation -e . -e __PLUGIN__
fi

status=0
python -m pytest __SUITE__ __FLAGS__ || status=$?
python -m pytest --doctest-modules __DOCTEST__ __FLAGS__ || status=$?
exit "$status"
"""

_APPLY_NONE = ""
_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SESSION_START = re.compile(r"^=+ test session starts =+$")
_SECTION_BANNER = re.compile(r"^=+ .+ =+$")
_COLLECT_ERROR = re.compile(r"^_+ ERROR collecting (\S+) _+$")
_WORKER_RESULT = re.compile(
    r"^\[gw\d+\] \[\s*\d+%\] (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S.*)$"
)
_SERIAL_RESULT = re.compile(
    r"^(\S+::\S.*?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?: +\[\s*\d+%\])?$"
)
_STATUS_BUCKET = {
    "PASSED": "passed",
    "XPASS": "passed",
    "FAILED": "failed",
    "ERROR": "failed",
    "SKIPPED": "skipped",
    "XFAIL": "skipped",
}


def _render(template: str, pr: PullRequest, apply: str = _APPLY_NONE) -> str:
    return (
        template.replace("__APPLY__", apply)
        .replace("__SUITE__", _SUITE_TARGETS)
        .replace("__DOCTEST__", _DOCTEST_TARGETS)
        .replace("__FLAGS__", _PYTEST_FLAGS)
        .replace("__PLUGIN__", _PLUGIN_DIR)
        .replace("__REPO__", pr.repo)
        .replace("__SHA__", pr.base.sha)
    )


class Sqlfluff5726To8797ImageBase(Image):
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
        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {_PYTHON_IMAGE}",
            f"""ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT""",
            """ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt\"""",
            """ENV DEBIAN_FRONTEND=noninteractive \\
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
    CURL_CA_BUNDLE=${CA_CERT_PATH}""",
            """ENV PIP_CERT=${CA_CERT_PATH} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PYTHONUNBUFFERED=1""",
            self.global_env,
            f"""LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/\"""",
            """RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt""",
            """RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*""",
            f'RUN python -m pip install "pip=={_PIP_VERSION}"',
            "RUN git config --global --add safe.directory '*'",
            "WORKDIR /home/",
            f"""RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null""",
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(section for section in sections if section) + "\n"


class Sqlfluff5726To8797ImageDefault(Image):
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
        return Sqlfluff5726To8797ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "run.sh", _render(_RUN_SH, self.pr, _APPLY_NONE)),
            File(".", "test-run.sh", _render(_RUN_SH, self.pr, _APPLY_TEST)),
            File(".", "fix-run.sh", _render(_RUN_SH, self.pr, _APPLY_FIX)),
        ]

    def dockerfile(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        copies = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        sections = [
            f"FROM {self.dependency().image_full_name()}",
            self.global_env,
            copies,
            "RUN bash /home/prepare.sh",
            f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\"""",
            f"""RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi""",
            self.clear_env,
        ]
        return "\n\n".join(section for section in sections if section) + "\n"


@Instance.register("sqlfluff", _INTERVAL)
class SQLFLUFF_5726_TO_8797(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Sqlfluff5726To8797ImageDefault(self.pr, self._config)

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
        buckets: dict[str, set[str]] = {"passed": set(), "failed": set(), "skipped": set()}
        in_results = False
        for raw_line in _ANSI_ESCAPE.sub("", test_log).splitlines():
            line = raw_line.strip()
            if _SESSION_START.match(line):
                in_results = True
                continue
            collect_error = _COLLECT_ERROR.match(line)
            if collect_error:
                buckets["failed"].add(collect_error.group(1))
                continue
            if _SECTION_BANNER.match(line):
                in_results = False
                continue
            if not in_results:
                continue
            worker = _WORKER_RESULT.match(line)
            if worker:
                status, name = worker.group(1), worker.group(2)
            else:
                serial = _SERIAL_RESULT.match(line)
                if not serial:
                    continue
                name, status = serial.group(1), serial.group(2)
            buckets[_STATUS_BUCKET[status]].add(name.strip())

        passed_tests = buckets["passed"]
        failed_tests = buckets["failed"]
        skipped_tests = buckets["skipped"]
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
