from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "ansible_86665_to_84251"

PR_LOW = 84251
PR_HIGH = 86665

_PYTHON_BASE = "python:3.11-slim-bookworm"

_REPO_DIR = "/home/ansible"
_PLUGIN_DIR = "/home/ansible/test/lib/ansible_test/_util/target/pytest/plugins"
_STUB_MODULE_UTILS = "/home/units_module_utils"
_STUB_MODULES = "/home/units_modules"
_PYTEST_INI = "/home/pytest.ini"
_ANSIBLE_CFG = "/home/ansible-test.cfg"

_PYTEST_PINS = '"pytest<9" "pytest-mock<4" "bcrypt<5" paramiko'


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


_PYTEST_INI_BODY = """[pytest]
xfail_strict = true
junit_family = xunit1
"""


_INSTALL = r"""
python -m pip install --no-cache-dir --upgrade pip setuptools wheel

installed=0
for attempt in 1 2 3 4 5; do
  if python -m pip install --no-cache-dir -r requirements.txt; then
    installed=1
    break
  fi
  echo "=== runtime requirement install attempt ${attempt} failed, retrying in 20s ==="
  sleep 20
done
test "$installed" -eq 1

for reqfile in test/lib/ansible_test/_data/requirements/units.txt test/units/requirements.txt; do
  test -f "$reqfile" || continue
  installed=0
  for attempt in 1 2 3 4 5; do
    if python -m pip install --no-cache-dir -r "$reqfile"; then
      installed=1
      break
    fi
    echo "=== ${reqfile} install attempt ${attempt} failed, retrying in 20s ==="
    sleep 20
  done
  test "$installed" -eq 1
done

installed=0
for attempt in 1 2 3 4 5; do
  if python -m pip install --no-cache-dir __PYTEST_PINS__; then
    installed=1
    break
  fi
  echo "=== test tooling install attempt ${attempt} failed, retrying in 20s ==="
  sleep 20
done
test "$installed" -eq 1
"""


_STUBS = r"""
mkdir -p __STUB_MODULE_UTILS__/ansible __STUB_MODULES__/ansible
: > __STUB_MODULE_UTILS__/ansible/__init__.py
: > __STUB_MODULES__/ansible/__init__.py
ln -sfn __REPO_DIR__/lib/ansible/module_utils __STUB_MODULE_UTILS__/ansible/module_utils
ln -sfn __REPO_DIR__/lib/ansible/module_utils __STUB_MODULES__/ansible/module_utils
ln -sfn __REPO_DIR__/lib/ansible/modules __STUB_MODULES__/ansible/modules
"""


_WRITE_CONFIG = r"""
: > __ANSIBLE_CFG__
cat > __PYTEST_INI__ <<'PYTEST_INI_EOF'
__PYTEST_INI_BODY__
PYTEST_INI_EOF
"""


_VERIFY = r"""
PYTHONPATH=__REPO_DIR__/lib python -c "from ansible.release import __version__; import jinja2, yaml, resolvelib, packaging; print('DEPS_OK: ansible-core ' + __version__)"
PYTHONPATH=__STUB_MODULE_UTILS__ python -c "import ansible.module_utils.basic; print('DEPS_OK: module_utils stub')"
PYTHONPATH=__STUB_MODULES__ python -c "import ansible.modules; print('DEPS_OK: modules stub')"
python -m pytest --version
echo "DEPS_OK: pytest runs"
"""


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export PYTEST_PLUGINS=ansible_forked
export ANSIBLE_CONFIG=__ANSIBLE_CFG__
export ANSIBLE_DEVEL_WARNING=false
export ANSIBLE_DEPRECATION_WARNINGS=false
export ANSIBLE_FORCE_COLOR=false
export ANSIBLE_FORCE_HANDLERS=true
export ANSIBLE_HOST_KEY_CHECKING=false
export ANSIBLE_HOST_PATTERN_MISMATCH=error
export ANSIBLE_RETRY_FILES_ENABLED=false
export ANSIBLE_INVENTORY=/dev/null
export ANSIBLE_LIBRARY=/dev/null
export PAGER=/bin/cat

cd __REPO_DIR__

apply_patches() {
    if git apply --whitespace=nowarn "$@"; then
        return 0
    fi
    echo "=== plain git apply failed; retrying with --3way ===" >&2
    git reset --hard
    git clean -fd
    git apply --3way --whitespace=nowarn "$@"
}
"""


_RESET = r"""
git reset --hard
git clean -fd
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
PYTEST_ARGS="-v -p no:cacheprovider -c __PYTEST_INI__ --rootdir __REPO_DIR__ --confcutdir __REPO_DIR__ --color=no --continue-on-collection-errors"

STATUS=0

echo "=== unit test context: module_utils ==="
PYTHONPATH=__STUB_MODULE_UTILS__:__PLUGIN_DIR__ python -m pytest $PYTEST_ARGS test/units/module_utils/ || STATUS=$?

echo "=== unit test context: modules ==="
PYTHONPATH=__STUB_MODULES__:__PLUGIN_DIR__ python -m pytest $PYTEST_ARGS test/units/modules/ || STATUS=$?

echo "=== unit test context: controller ==="
PYTHONPATH=__REPO_DIR__/lib:__PLUGIN_DIR__ python -m pytest $PYTEST_ARGS --ignore=test/units/module_utils --ignore=test/units/modules test/units/ || STATUS=$?

echo "=== pytest exited with status $STATUS ==="
exit $STATUS
"""


_PREPARE_SH = (
    r"""#!/bin/bash
set -e

export CI=true
export DEBIAN_FRONTEND=noninteractive

cd __REPO_DIR__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin __BASE_SHA__
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
echo "=== HEAD pinned at $(git rev-parse HEAD) ==="
"""
    + _INSTALL
    + _STUBS
    + _WRITE_CONFIG
    + _VERIFY
)


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

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    build-essential \
    pkg-config \
    libssl-dev \
    libffi-dev \
    openssh-client \
    sshpass \
    patch \
 && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__ \
 && git -C /home/__REPO__ rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

WORKDIR /home/__REPO__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

__HARDENING__

__CLEAR_ENV__
"""


class AnsibleBundle86665To84251ImageBase(Image):
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
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

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
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", DockerfileEnhancer._ENV_BLOCK)
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
        )


class AnsibleBundle86665To84251ImageDefault(Image):
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
        return AnsibleBundle86665To84251ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__PYTEST_INI_BODY__", _PYTEST_INI_BODY.rstrip("\n"))
            .replace("__PYTEST_PINS__", _PYTEST_PINS)
            .replace("__STUB_MODULE_UTILS__", _STUB_MODULE_UTILS)
            .replace("__STUB_MODULES__", _STUB_MODULES)
            .replace("__PLUGIN_DIR__", _PLUGIN_DIR)
            .replace("__PYTEST_INI__", _PYTEST_INI)
            .replace("__ANSIBLE_CFG__", _ANSIBLE_CFG)
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

_OUTCOME_RE = re.compile(
    r"^(?P<name>\S+::\S(?:.*\S)?)"
    r"\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\(.*\))?"
    r"(?:\s+\[\s*\d+%\s*\])?"
    r"\s*$",
    re.MULTILINE,
)

_PASS_STATUSES = frozenset({"PASSED"})
_FAIL_STATUSES = frozenset({"FAILED", "ERROR"})
_SKIP_STATUSES = frozenset({"SKIPPED", "XFAIL", "XPASS"})


def ansible_bundle_86665_to_84251_parse_log(test_log: str) -> TestResult:
    log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for match in _OUTCOME_RE.finditer(log):
        name = match.group("name").strip()
        status = match.group("status")
        if not name:
            continue
        if status in _PASS_STATUSES:
            passed_tests.add(name)
        elif status in _FAIL_STATUSES:
            failed_tests.add(name)
        elif status in _SKIP_STATUSES:
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


@Instance.register("ansible", _INTERVAL_NAME)
class ANSIBLE_86665_TO_84251(Instance):
    PR_LOW = PR_LOW
    PR_HIGH = PR_HIGH

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return AnsibleBundle86665To84251ImageDefault(self.pr, self._config)

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
        return ansible_bundle_86665_to_84251_parse_log(test_log)
