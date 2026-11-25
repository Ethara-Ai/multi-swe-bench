import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NUMBER_INTERVAL = "sqlfluff_sqlfluff_4747_to_4780"
_BASE_TAG = f"base-{_NUMBER_INTERVAL}"
# sqlfluff/sqlfluff PRs 4747..4780 (April 2023, sqlfluff 2.0.x).
#
# Same shape as the 5602_to_6874 / 7048_to_8818 eras (shared base, pin in
# prepare.sh, scrub in the PR layer, pip resolved at the base commit's date), but
# on Python 3.10: these commits declare py37-310 in tox and setup.cfg, and the
# April-2023 releases of their C-extension deps (PyYAML, regex) predate 3.12
# wheels, so a 3.12 interpreter would have to build them from source.
# The example plugin's import package is `example` in this era (renamed to
# sqlfluff_plugin_example later), so the gate imports that name; the entry point
# `sqlfluff_example` is the same in both.
_PYTHON_IMAGE = "python:3.10.20-slim-bookworm"
_PIP_VERSION = "26.2.2"

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_ENV = """export CI=true
export SQLFLUFF_TESTENV=1
"""

_PREPARE = """#!/bin/bash
set -e
ulimit -c 0

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __SHA__^{commit} 2>/dev/null || git fetch --no-tags origin __SHA__
git checkout --detach __SHA__
bash /home/check_git_changes.sh

__ENV__
cutoff="$(git show -s --format=%cI HEAD)"
python -m pip install --uploaded-prior-to "${cutoff}" -r requirements_dev.txt setuptools wheel || python -m pip install --uploaded-prior-to "${cutoff}" -r <(grep -vE '^types-' requirements_dev.txt) setuptools wheel || true
python -m pip install --uploaded-prior-to "${cutoff}" --no-build-isolation -e . -e plugins/sqlfluff-plugin-example

python -m pytest --collect-only -qq -p no:cacheprovider test plugins/sqlfluff-plugin-example/test
python -m pytest --collect-only -qq -p no:cacheprovider --doctest-modules src
python -c "import importlib.metadata, hypothesis, pytest, setuptools, xdist, sqlfluff, example; from sqlfluff.core import Linter; assert sqlfluff.__file__.startswith('/home/__REPO__/src/'); assert 'sqlfluff_example' in importlib.metadata.entry_points(group='sqlfluff').names; assert Linter(dialect='ansi').parse_string('select 1').tree is not None; print('DEPS_OK')"
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
"""

_TEST = """

status=0
python -m pytest test plugins/sqlfluff-plugin-example/test -v -p no:cacheprovider -n auto --maxprocesses=8 --continue-on-collection-errors --tb=short || status=$?
python -m pytest src --doctest-modules -v -p no:cacheprovider -n auto --maxprocesses=8 --continue-on-collection-errors --tb=short || status=$?
exit "${status}"
"""

_RUN_SH = _RUN_HEADER + _TEST

_TEST_RUN_SH = _RUN_HEADER + "git apply --whitespace=nowarn /home/test.patch\n" + _TEST

_FIX_RUN_SH = (
    _RUN_HEADER
    + "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
    + _TEST
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_SESSION_START_RE = re.compile(r"^=+ test session starts =+$")
_SECTION_RE = re.compile(r"^=+ .+ =+$")
_RESULT_RE = re.compile(
    r"^\[gw\d+\](?: \[\s*\d+%\])? "
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (?P<name>\S.*?)\s*$"
)


class SqlfluffImageBase4747To4780(Image):
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
        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""
        return f"""# syntax=docker/dockerfile:1.6

FROM {_PYTHON_IMAGE}

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
    PIP_CERT=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PYTHONUNBUFFERED=1

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
{global_env}
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "pip=={_PIP_VERSION}"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null
{clear_env}
CMD ["/bin/bash"]
"""


class SqlfluffImageDefault4747To4780(Image):
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
        return SqlfluffImageBase4747To4780(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, script: str) -> str:
        return (
            script.replace("__ENV__", _ENV)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._render(_PREPARE)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""
        return f"""FROM {base.image_full_name()}
{global_env}
{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
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
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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
    fi
{clear_env}"""


@Instance.register("sqlfluff", _NUMBER_INTERVAL)
class SQLFLUFF_4747_TO_4780(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SqlfluffImageDefault4747To4780(self.pr, self._config)

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

        in_results = False
        for line in _ANSI_RE.sub("", test_log).splitlines():
            stripped = line.rstrip()
            if _SESSION_START_RE.match(stripped):
                in_results = True
                continue
            if _SECTION_RE.match(stripped):
                in_results = False
                continue
            if not in_results:
                continue
            match = _RESULT_RE.match(stripped)
            if not match:
                continue
            status = match.group("status")
            name = match.group("name")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
