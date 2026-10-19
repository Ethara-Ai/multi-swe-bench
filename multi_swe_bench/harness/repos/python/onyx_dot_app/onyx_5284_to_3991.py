import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "onyx_5284_to_3991"
_BASE_TAG = "base-5284_to_3991"
_PYTHON_IMAGE = "python:3.11.7-slim-bookworm"
_BACKEND_DIR = "backend"


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
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DANSWER_RUNNING_IN_DOCKER=true \
    DO_NOT_TRACK=true

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
        curl \
        zip \
        build-essential \
        gcc \
        g++ \
        make \
        cmake \
        pkg-config \
        libpq-dev \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
        libxmlsec1-dev \
        zlib1g-dev \
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

export CI=true

python --version

cd /home/__REPO__/__BACKEND_DIR__

printf 'setuptools<81\n' > /tmp/pip-constraints.txt

python -m pip install --upgrade pip
python -m pip install --retries 5 --timeout 120 -c /tmp/pip-constraints.txt -r requirements/default.txt

PYTEST_PINS="$(grep -E '^(pytest|pytest-asyncio|pytest-dotenv|pytest-mock)==' requirements/dev.txt | tr '\n' ' ')"
test -n "${PYTEST_PINS}"
python -m pip install --retries 5 --timeout 120 -c /tmp/pip-constraints.txt ${PYTEST_PINS}

python -c "import pkg_resources, pytest, onyx.configs.constants, onyx.connectors.factory, onyx.main; print('DEPS_OK')"
"""


_TEST_BLOCK = r"""TEST_TARGETS=""
for candidate in __TEST_TARGETS__; do
    if [ -e "${candidate}" ]; then
        TEST_TARGETS="${TEST_TARGETS} ${candidate}"
    fi
done

if [ -z "${TEST_TARGETS}" ]; then
    echo "No test targets present at this stage"
    exit 0
fi

python -m pytest ${TEST_TARGETS} -v -rA --tb=no --color=no -p no:cacheprovider --continue-on-collection-errors"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

cd /home/__REPO__/__BACKEND_DIR__

__TEST_BLOCK__
"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --whitespace=nowarn __PATCH_EXCLUDES__/home/test.patch

cd /home/__REPO__/__BACKEND_DIR__

__TEST_BLOCK__
"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --whitespace=nowarn __PATCH_EXCLUDES__/home/test.patch /home/fix.patch

cd /home/__REPO__/__BACKEND_DIR__

__TEST_BLOCK__
"""


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_BINARY_STUB_RE = re.compile(r"^Binary files .* differ$", re.M)
_GIT_BINARY_RE = re.compile(r"^GIT binary patch$", re.M)


def _diff_sections(patch: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    matches = list(_DIFF_HEADER_RE.finditer(patch or ""))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(patch)
        sections.append((match.group(2), patch[start:end]))
    return sections


def _unappliable_binary_paths(pr: PullRequest) -> list[str]:
    paths: list[str] = []
    for patch in (pr.test_patch or "", pr.fix_patch or ""):
        for path, body in _diff_sections(patch):
            if _BINARY_STUB_RE.search(body) and not _GIT_BINARY_RE.search(body):
                if path not in paths:
                    paths.append(path)
    return sorted(paths)


def _patch_excludes(pr: PullRequest) -> str:
    excludes = [
        f"--exclude={shlex.quote(path)}" for path in _unappliable_binary_paths(pr)
    ]
    return " ".join(excludes) + " " if excludes else ""


def _test_targets(pr: PullRequest) -> str:
    prefix = f"{_BACKEND_DIR}/"
    targets: list[str] = []
    for path, _ in _diff_sections(pr.test_patch or ""):
        name = path.rsplit("/", 1)[-1]
        if not path.startswith(prefix) or not path.endswith(".py"):
            continue
        if not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        relative = path[len(prefix) :]
        if relative not in targets:
            targets.append(relative)
    return " ".join(shlex.quote(path) for path in sorted(targets))


class OnyxImageBase_5284_TO_3991(Image):

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


class OnyxImageDefault_5284_TO_3991(Image):

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
        return OnyxImageBase_5284_TO_3991(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__TEST_TARGETS__", _test_targets(self.pr))
            .replace("__PATCH_EXCLUDES__", _patch_excludes(self.pr))
            .replace("__BACKEND_DIR__", _BACKEND_DIR)
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

        hardening = Image._HARDENING_BLOCK.replace(
            '"${BASE_COMMIT}"', self.pr.base.sha
        )

        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__HARDENING__", hardening)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_VERBOSE_RE = re.compile(
    r"^(?P<id>\S+\.py::\S.*?)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\(.*\))?\s*(?:\[\s*\d+%\])?\s*$"
)
_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<id>\S+\.py(?:::\S.*?)?)(?:\s+-\s+.*)?$"
)


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

    for raw_line in _ANSI_RE.sub("", test_log).replace("\r\n", "\n").splitlines():
        line = raw_line.strip()

        match = _VERBOSE_RE.match(line)
        if match:
            record(match.group("id"), match.group("status"))
            continue

        match = _SUMMARY_RE.match(line)
        if match:
            record(match.group("id"), match.group("status"))

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


@Instance.register("onyx-dot-app", _INTERVAL_NAME)
class ONYX_5284_TO_3991(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return OnyxImageDefault_5284_TO_3991(self.pr, self._config)

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


Instance.register("onyx-dot-app", "onyx")(ONYX_5284_TO_3991)
