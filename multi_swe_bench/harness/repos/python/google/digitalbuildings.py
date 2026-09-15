from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_BASE = "python:3.7-bullseye"

_REPO_DIR = "/home/digitalbuildings"
_ONTOLOGY_VALIDATOR_DIR = f"{_REPO_DIR}/tools/validators/ontology_validator"
_INSTANCE_VALIDATOR_DIR = f"{_REPO_DIR}/tools/validators/instance_validator"
_TEST_TARGET = "tools/validators/instance_validator/tests"
_PYTHONPATH = f"{_ONTOLOGY_VALIDATOR_DIR}:{_INSTANCE_VALIDATOR_DIR}"

_RUNTIME_DEPS = (
    "'pytest==7.4.4' 'absl-py==2.1.0' 'six==1.17.0' 'pyyaml==6.0.1' "
    "'protobuf==3.17.3' 'ruamel.yaml==0.15.93' 'strictyaml==1.1.0' "
    "'google-cloud-pubsub==2.6.1' 'google-auth==1.35.0' "
    "'googleapis-common-protos==1.52.0'"
)


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


_VERIFY = r"""
python -c "import pytest; from absl.testing import absltest; print('DEPS_OK: pytest + absltest')"
python -c "from validate import constants, entity_instance, field_translation, generate_universe, handler, instance_parser, telemetry_validator; print('DEPS_OK: instance validator')"
python -c "from yamlformat.validator import presubmit_validate_types_lib; print('DEPS_OK: ontology validator')"
python -c "from os import path; from tests import test_constants; assert path.isdir(test_constants.ONTOLOGY_ROOT), test_constants.ONTOLOGY_ROOT; print('DEPS_OK: ontology root resolved')"
python -m pytest --version
echo "DEPS_OK: pytest runs"
"""


_PREPARE_SH = (
    r"""#!/bin/bash
set -e

export CI=true
export DEBIAN_FRONTEND=noninteractive
export PYTHONPATH=__PYTHONPATH__

cd __REPO_DIR__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin __BASE_SHA__
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
echo "=== HEAD pinned at $(git rev-parse HEAD) ==="
"""
    + _VERIFY
    + r"""
python -m pytest -q -p no:cacheprovider __TEST_TARGET__ > /dev/null 2>&1 || true
"""
)


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONPATH=__PYTHONPATH__
export PYTHONDONTWRITEBYTECODE=1
export PAGER=/bin/cat

cd __REPO_DIR__

apply_patches() {
    if git apply --whitespace=nowarn "$@"; then
        return 0
    fi
    echo "=== plain git apply failed; retrying with --3way ===" >&2
    git reset --hard
    git clean -fdx
    git apply --3way --whitespace=nowarn "$@"
}
"""


_RESET = r"""
git reset --hard
git clean -fdx
"""


_APPLY_TEST_PATCH = r"""
if ! apply_patches /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
"""


_APPLY_BOTH_PATCHES = r"""
if ! apply_patches /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch + fix.patch failed" >&2
    exit 1
fi
"""


_EXEC_TESTS = r"""
python -m pytest -rA --tb=short -v -p no:cacheprovider --color=no \
    --continue-on-collection-errors __TEST_TARGET__
"""


_RUN_SH = _SCRIPT_HEADER + _RESET + _EXEC_TESTS
_TEST_RUN_SH = _SCRIPT_HEADER + _RESET + _APPLY_TEST_PATCH + _EXEC_TESTS
_FIX_RUN_SH = _SCRIPT_HEADER + _RESET + _APPLY_BOTH_PATCHES + _EXEC_TESTS


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).rstrip("\n") + "\n"


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

__PROXY_ARGS__

__ENV_BLOCK__

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

__CERT_SYMLINKS__

ENV PIP_PROGRESS_BAR=off \
    PIP_NO_COLOR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=__PYTHONPATH__ \
    NO_COLOR=1 \
    PY_COLORS=0 \
    CI=true

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir __RUNTIME_DEPS__

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__ \
 && git -C /home/__REPO__ rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

WORKDIR __REPO_DIR__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

__HARDENING__

__CLEAR_ENV__
"""


class DigitalBuildingsImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _PYTHON_BASE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_image = self.dependency()
        if isinstance(base_image, Image):
            base_image = base_image.image_full_name()

        return _tidy(
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", base_image)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__PYTHONPATH__", _PYTHONPATH)
            .replace("__RUNTIME_DEPS__", _RUNTIME_DEPS)
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", DockerfileEnhancer._ENV_BLOCK)
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
        )


class DigitalBuildingsImageDefault(Image):
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
        return DigitalBuildingsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__PYTHONPATH__", _PYTHONPATH)
            .replace("__TEST_TARGET__", _TEST_TARGET)
            .replace("__REPO_DIR__", _REPO_DIR)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
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

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return _tidy(
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace(
                "__HARDENING__",
                Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha),
            )
            .replace("__CLEAR_ENV__", self.clear_env)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_VERBOSE_RE = re.compile(
    r"^(?P<name>\S+\.py::\S+?)"
    r"\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)

_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"\s+(?P<name>\S+\.py::\S+)"
)

_COLLECT_ERROR_RE = re.compile(r"^ERROR\s+(?P<name>\S+\.py)(?:\s|$)")

_PASS_STATUSES = frozenset({"PASSED", "XPASS"})
_FAIL_STATUSES = frozenset({"FAILED", "ERROR"})


def digitalbuildings_parse_log(test_log: str) -> TestResult:
    log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(name: str, status: str) -> None:
        if not name:
            return
        if status in _PASS_STATUSES:
            if name in failed_tests:
                return
            skipped_tests.discard(name)
            passed_tests.add(name)
        elif status in _FAIL_STATUSES:
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        else:
            if name in passed_tests or name in failed_tests:
                return
            skipped_tests.add(name)

    for raw_line in log.splitlines():
        line = raw_line.strip()

        match = _VERBOSE_RE.match(line)
        if match:
            record(match.group("name"), match.group("status"))
            continue

        match = _SUMMARY_RE.match(line)
        if match:
            record(match.group("name"), match.group("status"))
            continue

        match = _COLLECT_ERROR_RE.match(line)
        if match:
            record(match.group("name"), "ERROR")

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


@Instance.register("google", "digitalbuildings")
class DIGITALBUILDINGS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return DigitalBuildingsImageDefault(self.pr, self._config)

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
        return digitalbuildings_parse_log(test_log)
