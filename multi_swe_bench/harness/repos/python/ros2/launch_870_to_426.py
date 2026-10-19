import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "launch_870_to_426"
_BASE_TAG = "base-870_to_426"
_PYTHON_IMAGE = "python:3.8-slim-bookworm"
_HUMBLE_ERA_FROM = 870
_AMENT_PREFIX = "/opt/ament_prefix"


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
    && rm -rf /var/lib/apt/lists/*

__GLOBAL_ENV__

WORKDIR /home/

__CODE__

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

export PYTHONDONTWRITEBYTECODE=1
export AMENT_PREFIX_PATH=__AMENT_PREFIX__

python --version
grep -q "<version>__LAUNCH_VERSION__" launch/package.xml || { echo "prepare.sh: launch/package.xml is not version __LAUNCH_VERSION__x" >&2; exit 1; }

python -m pip install --retries 5 --timeout 120 \
__PINS__

for pkg in __PACKAGES__; do
    python -m pip install --no-deps --no-build-isolation -e "$pkg"
done

rm -rf __AMENT_PREFIX__
mkdir -p __AMENT_PREFIX__/share/ament_index/resource_index/packages __AMENT_PREFIX__/lib
for pkg in __PACKAGES__; do
    touch "__AMENT_PREFIX__/share/ament_index/resource_index/packages/$pkg"
    mkdir -p "__AMENT_PREFIX__/share/$pkg"
    ln -s "/home/__REPO__/$pkg/package.xml" "__AMENT_PREFIX__/share/$pkg/package.xml"
done
ln -s /home/__REPO__/launch/share/launch/frontend __AMENT_PREFIX__/share/launch/frontend
ln -s /home/__REPO__/launch_testing/example_processes __AMENT_PREFIX__/lib/launch_testing
ln -s /home/__REPO__/launch_testing/test/launch_testing/examples __AMENT_PREFIX__/share/launch_testing/examples
__EXTRA_LINKS__
test -f __AMENT_PREFIX__/share/launch/frontend/grammar.lark || { echo "prepare.sh: grammar.lark not reachable through the ament prefix" >&2; exit 1; }
ls __AMENT_PREFIX__/lib/launch_testing/terminating_proc* > /dev/null || { echo "prepare.sh: launch_testing example processes not reachable through the ament prefix" >&2; exit 1; }

cd /tmp
python -c "import pytest, pytest_timeout, lark, yaml, osrf_pycommon, ament_index_python, ament_copyright, ament_flake8, ament_pep257, launch, launch.frontend, launch_testing, launch_xml, launch_yaml; assert pytest.__version__ == '__PYTEST_VERSION__'; print('DEPS_OK', pytest.__version__)" || { echo "prepare.sh: dependency import gate failed" >&2; exit 1; }
cd /home/__REPO__
for pkg in __PACKAGES__; do
    (cd "$pkg" && python -m pytest --collect-only -q -p no:cacheprovider > "/tmp/collect-$pkg.txt") || { tail -30 "/tmp/collect-$pkg.txt"; echo "prepare.sh: pytest cannot collect $pkg" >&2; exit 1; }
    grep -Eq "^test/[^ ]+\.py::" "/tmp/collect-$pkg.txt" || { echo "prepare.sh: no tests collected in $pkg" >&2; exit 1; }
done
echo "COLLECT_OK"
"""


_FOXY_PINS = (
    "setuptools==47.3.1",
    "pytest==5.4.3",
    "pytest-timeout==1.4.1",
    "lark-parser==0.8.9",
    "osrf-pycommon==0.1.9",
    "PyYAML==5.3.1",
    "mock==4.0.2",
    "flake8==3.8.3",
    "pydocstyle==5.0.2",
    "ament-index-python @ git+https://github.com/ament/ament_index.git@1.0.2#subdirectory=ament_index_python",
    "ament-copyright @ git+https://github.com/ament/ament_lint.git@0.9.4#subdirectory=ament_copyright",
    "ament-flake8 @ git+https://github.com/ament/ament_lint.git@0.9.4#subdirectory=ament_flake8",
    "ament-pep257 @ git+https://github.com/ament/ament_lint.git@0.9.4#subdirectory=ament_pep257",
    "ament-lint @ git+https://github.com/ament/ament_lint.git@0.9.4#subdirectory=ament_lint",
)


_HUMBLE_PINS = (
    "setuptools==59.6.0",
    "pytest==6.2.5",
    "pytest-timeout==2.0.2",
    "lark==1.1.1",
    "osrf-pycommon==2.0.2",
    "PyYAML==6.0.1",
    "importlib-metadata==4.6.4",
    "flake8==4.0.1",
    "pydocstyle==6.1.1",
    "ament-index-python @ git+https://github.com/ament/ament_index.git@1.4.0#subdirectory=ament_index_python",
    "ament-copyright @ git+https://github.com/ament/ament_lint.git@0.12.12#subdirectory=ament_copyright",
    "ament-flake8 @ git+https://github.com/ament/ament_lint.git@0.12.12#subdirectory=ament_flake8",
    "ament-pep257 @ git+https://github.com/ament/ament_lint.git@0.12.12#subdirectory=ament_pep257",
    "ament-lint @ git+https://github.com/ament/ament_lint.git@0.12.12#subdirectory=ament_lint",
)


_HUMBLE_EXTRA_LINKS = r"""ln -s /home/__REPO__/launch_pytest/test/launch_pytest/examples __AMENT_PREFIX__/share/launch_pytest/examples"""


_TEST_CMD = r"""status=0
for pkg in __PACKAGES__; do
    echo "ROS2_LAUNCH_PACKAGE $pkg"
    pkg_flags=""
    case "$pkg" in
        launch) pkg_flags="-o addopts= -p no:launch_testing -p no:launch -p no:launch_pytest" ;;
        launch_xml|launch_yaml) pkg_flags="-p no:launch_testing -p no:launch -p no:launch_pytest" ;;
    esac
    pkg_status=0
    (cd "$pkg" && python -m pytest -v -rfEsxX --tb=no --color=no -p no:cacheprovider $pkg_flags --continue-on-collection-errors --timeout=300) || pkg_status=$?
    test "$pkg_status" -le 1 || { echo "pytest exited with status $pkg_status in $pkg" >&2; exit "$pkg_status"; }
    test "$pkg_status" -eq 0 || status=1
done
exit "$status"
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1
export AMENT_PREFIX_PATH=__AMENT_PREFIX__

cd /home/__REPO__

__TEST_CMD__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1
export AMENT_PREFIX_PATH=__AMENT_PREFIX__

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch

__TEST_CMD__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1
export AMENT_PREFIX_PATH=__AMENT_PREFIX__

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_CMD__"""


def _is_humble_era(pr: PullRequest) -> bool:
    return pr.number >= _HUMBLE_ERA_FROM


class ROS2LaunchImageBase_870_TO_426(Image):

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


class ROS2LaunchImageDefault_870_TO_426(Image):

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
        return ROS2LaunchImageBase_870_TO_426(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        if _is_humble_era(self.pr):
            packages = "launch launch_testing launch_xml launch_yaml launch_pytest"
            pins = _HUMBLE_PINS
            extra_links = _HUMBLE_EXTRA_LINKS
            launch_version = "1.0."
            pytest_version = "6.2.5"
        else:
            packages = "launch launch_testing launch_xml launch_yaml"
            pins = _FOXY_PINS
            extra_links = ""
            launch_version = "0.10."
            pytest_version = "5.4.3"

        return (
            template.replace("__TEST_CMD__", _TEST_CMD)
            .replace("__PINS__", " \\\n".join(f'    "{pin}"' for pin in pins))
            .replace("__EXTRA_LINKS__", extra_links)
            .replace("__PACKAGES__", packages)
            .replace("__LAUNCH_VERSION__", launch_version)
            .replace("__PYTEST_VERSION__", pytest_version)
            .replace("__AMENT_PREFIX__", _AMENT_PREFIX)
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
_PACKAGE_RE = re.compile(r"^ROS2_LAUNCH_PACKAGE (?P<pkg>[A-Za-z0-9_]+)$")
_VERBOSE_RE = re.compile(
    r"^(?P<id>[^\s:]+/[^\s:]+\.py::\S.*?)"
    r"\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\[\s*\d+%\])?\s*$"
)
_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
    r"(?P<id>[^\s:]+/[^\s:]+\.py(?:::\S.*?)?)(?:\s+-\s+.*)?$"
)


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    package = ""
    for raw_line in _ANSI_RE.sub("", test_log).replace("\r\n", "\n").splitlines():
        line = raw_line.strip()

        package_match = _PACKAGE_RE.match(line)
        if package_match:
            package = package_match.group("pkg")
            continue
        if not package:
            continue

        match = _VERBOSE_RE.match(line) or _SUMMARY_RE.match(line)
        if not match:
            continue

        name = f"{package}/{match.group('id').strip()}"
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


@Instance.register("ros2", _INTERVAL_NAME)
class ROS2_LAUNCH_870_TO_426(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ROS2LaunchImageDefault_870_TO_426(self.pr, self._config)

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
