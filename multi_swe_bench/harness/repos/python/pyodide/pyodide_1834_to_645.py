import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "pyodide_1834_to_645"
_BASE_TAG = "base-1834_to_645"
_PYTHON_IMAGE = "python:3.8-slim-bookworm"
_SPLIT_LAYOUT_FROM = 1834


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
    PIP_CERT=${CA_CERT_PATH} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

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
        git \
        ca-certificates \
        build-essential \
        libffi-dev \
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

__GLOBAL_ENV__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

WORKDIR /home/__REPO__

RUN set -eux; \
    git checkout --detach "__BASE_SHA__"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi

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


_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach "__BASE_SHA__"
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

export CI=true
export PYTHONDONTWRITEBYTECODE=1

python --version

__PROVISION__
"""


_PROVISION_FLAT_LAYOUT = r"""python -m pip install --retries 5 --timeout 120 \
    "pytest==5.4.3" \
    "PyYAML==5.3.1" \
    "selenium==3.141.0" \
    "urllib3==1.25.9"

python -c "import pytest, yaml, selenium.webdriver.common.utils, pkg_resources, pyodide_build.common, pyodide_build.buildall, pyodide_build.buildpkg, pyodide_build.mkpkg, pyodide_build.pywasmcross; assert pytest.__version__ == '5.4.3'"
python -m pytest --collect-only -q -p no:cacheprovider test/pyodide_build test/test_common.py > /tmp/pytest-collect.txt
grep -q "test/pyodide_build/test_pywasmcross.py::test_handle_command" /tmp/pytest-collect.txt
echo "DEPS_OK"
"""


_PROVISION_SPLIT_LAYOUT = r"""cat > /tmp/pip-constraints.txt <<'EOF'
pytest==6.2.5
pluggy==1.0.0
py==1.10.0
attrs==21.2.0
iniconfig==1.1.0
toml==0.10.2
pytest-asyncio==0.15.1
pytest-httpserver==1.0.1
Werkzeug==2.0.1
hypothesis==6.21.1
sortedcontainers==2.4.0
pexpect==4.8.0
ptyprocess==0.7.0
PyYAML==5.4.1
ruamel.yaml==0.17.16
ruamel.yaml.clib==0.2.6
packaging==21.0
pyparsing==2.4.7
Cython==0.29.24
selenium==4.0.0b3
urllib3==1.26.6
trio==0.19.0
trio-websocket==0.9.2
EOF

python -m pip install --retries 5 --timeout 120 -c /tmp/pip-constraints.txt \
    "cython<3.0" \
    packaging \
    pyyaml \
    ruamel.yaml \
    hypothesis \
    pexpect \
    pytest \
    pytest-asyncio \
    pytest-httpserver \
    "selenium==4.0.0b3"

PYTHONPATH=pyodide-build:src/py python -c "import pytest, pytest_asyncio, pytest_httpserver, hypothesis, pexpect, yaml, ruamel.yaml, packaging, pkg_resources, selenium, Cython, pyodide_build.buildall, pyodide_build.buildpkg, pyodide_build.common, pyodide_build.io, pyodide_build.mkpkg, pyodide_build.testing; assert pytest.__version__ == '6.2.5'"
python -m pytest --collect-only -q -p no:cacheprovider pyodide-build > /tmp/pytest-collect.txt
grep -q "pyodide-build/pyodide_build/tests/test_common.py::test_parse_package_subset" /tmp/pytest-collect.txt
echo "DEPS_OK"
"""


_TEST_CMD_FLAT_LAYOUT = "python -m pytest test pyodide_build -v -rA -p no:cacheprovider --continue-on-collection-errors -k 'not (chrome or firefox)'"


_TEST_CMD_SPLIT_LAYOUT = "python -m pytest src pyodide-build packages/micropip -v -rA -p no:cacheprovider --continue-on-collection-errors -k 'not (chrome or firefox or node)'"


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1

cd /home/__REPO__

__TEST_CMD__
"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch

__TEST_CMD__
"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_CMD__
"""


def _is_split_layout(pr: PullRequest) -> bool:
    return pr.number >= _SPLIT_LAYOUT_FROM


class PyodideImageBase_1834_TO_645(Image):

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
        code = _CLONE_CODE if self.config.need_clone else _COPY_CODE

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class PyodideImageDefault_1834_TO_645(Image):

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
        return PyodideImageBase_1834_TO_645(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        if _is_split_layout(self.pr):
            provision = _PROVISION_SPLIT_LAYOUT
            test_cmd = _TEST_CMD_SPLIT_LAYOUT
        else:
            provision = _PROVISION_FLAT_LAYOUT
            test_cmd = _TEST_CMD_FLAT_LAYOUT

        return (
            template.replace("__PROVISION__", provision)
            .replace("__TEST_CMD__", test_cmd)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_VERBOSE_RE = re.compile(
    r"^(?P<id>\S+\.py::\S.*?)"
    r"(?:\s+<-\s+\S+)?\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\[\s*\d+%\])?\s*$"
)
_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
    r"(?P<id>\S+\.py::\S.*?)(?:\s+-\s+.*)?$"
)


def _restore_split_layout_id(name: str) -> str:
    path, _, rest = name.partition("::")

    if "/" not in path:
        path = f"src/tests/{path}"
    elif path.startswith("micropip/"):
        path = f"packages/{path}"

    module = path.rsplit("/", 1)[-1]
    if not (module.startswith("test_") and module.endswith(".py")):
        return f"{path}::{rest}"

    head, bracket, params = rest.partition("[")
    segments = [
        segment if segment.startswith("Test") else f"test_{segment}"
        for segment in head.split("::")
    ]
    return f"{path}::{'::'.join(segments)}{bracket}{params}"


def _parse_log(test_log: str, split_layout: bool) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw_line in _ANSI_RE.sub("", test_log).replace("\r\n", "\n").splitlines():
        line = raw_line.strip()

        match = _VERBOSE_RE.match(line) or _SUMMARY_RE.match(line)
        if not match:
            continue

        name = match.group("id").strip()
        if split_layout:
            name = _restore_split_layout_id(name)

        status = match.group("status")
        if status in ("PASSED", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

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


@Instance.register("pyodide", _INTERVAL_NAME)
class PYODIDE_1834_TO_645(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PyodideImageDefault_1834_TO_645(self.pr, self._config)

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
        return _parse_log(test_log, _is_split_layout(self.pr))
