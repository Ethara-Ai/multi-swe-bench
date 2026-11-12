import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "strix_1495_to_866"
_BASE_TAG = "base-1495_to_866"
_PYTHON_IMAGE = "python:3.12-slim-bookworm"


_BASE_DOCKERFILE = r"""__SYNTAX__

FROM __BASE_IMAGE__

__INFRA__

ENV LC_ALL=C.UTF-8 \
    PIP_CERT=${CA_CERT_PATH} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        build-essential \
        gcc \
        g++ \
        make \
        pkg-config \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

__GLOBAL_ENV__

WORKDIR /home/

__CODE__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


_CLONE_CODE = r"""RUN git clone "${REPO_URL}" /home/__REPO__ && \
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null"""


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
set -e

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

printf 'setuptools<81\n' > /tmp/pip-constraints.txt

python -m pip install --upgrade pip || true
if [ "$(uname -m)" = "x86_64" ]; then
    # glibc uv segfaults under QEMU amd64 emulation; the static musl build does not.
    curl -fsSL https://github.com/astral-sh/uv/releases/download/0.13.6/uv-x86_64-unknown-linux-musl.tar.gz \
        | tar -xz -C /tmp && install -m 0755 /tmp/uv-x86_64-unknown-linux-musl/uv /usr/local/bin/uv || true
else
    python -m pip install --retries 5 --timeout 120 "uv==0.13.6" || true
fi
uv --version || true

# Install the exact dependency set pinned in the repo's uv.lock (project deps +
# the default "dev" dependency group, which carries pytest / pytest-asyncio).
if [ -f uv.lock ] && uv export --frozen --no-hashes --no-emit-project --format requirements-txt > /tmp/locked-requirements.txt; then
    # uv.lock is authoritative here (it pins its own setuptools), so no extra constraints.
    python -m pip install --retries 5 --timeout 120 -r /tmp/locked-requirements.txt || true
    python -m pip install --retries 5 --timeout 120 --no-deps -e . || true
else
    echo "prepare.sh: uv.lock export unavailable, falling back to unlocked install" >&2
    python -m pip install --retries 5 --timeout 120 -c /tmp/pip-constraints.txt -e . pytest pytest-asyncio || true
fi

# Fail the build only if the environment is genuinely unusable.
python -c "import pytest, strix; print('DEPS_OK')"
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

__TEST_BLOCK__
"""


_PATCH_APPLY_SETUP = r"""EXCLUDES="__BINARY_EXCLUDES__"
for candidate in __NON_TEST_PATHS__; do
    if ! git apply --check --include="${candidate}" /home/test.patch >/dev/null 2>&1; then
        EXCLUDES="${EXCLUDES} --exclude=${candidate}"
        echo "patch-apply: dropping non-test path from test.patch (does not apply at base): ${candidate}" >&2
    fi
done"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

__PATCH_APPLY_SETUP__

git apply --whitespace=nowarn ${EXCLUDES} /home/test.patch

__TEST_BLOCK__
"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

__PATCH_APPLY_SETUP__

git apply --whitespace=nowarn ${EXCLUDES} /home/test.patch /home/fix.patch

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
    return " ".join(excludes)


def _non_test_paths_in_test_patch(pr: PullRequest) -> list[str]:
    paths: list[str] = []
    for path, _ in _diff_sections(pr.test_patch or ""):
        name = path.rsplit("/", 1)[-1]
        is_test = (
            path.startswith("tests/")
            or path.startswith("test/")
            or name == "conftest.py"
            or (
                path.endswith(".py")
                and (name.startswith("test_") or name.endswith("_test.py"))
            )
        )
        if not is_test and path not in paths:
            paths.append(path)
    return paths


def _non_test_paths_shell(pr: PullRequest) -> str:
    return " ".join(shlex.quote(p) for p in _non_test_paths_in_test_patch(pr))


def _test_targets(pr: PullRequest) -> str:
    targets: list[str] = []
    for path, _ in _diff_sections(pr.test_patch or ""):
        name = path.rsplit("/", 1)[-1]
        if not path.endswith(".py"):
            continue
        if not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        if not (
            path.startswith("tests/")
            or path.startswith("strix/")
            or path.startswith("skills/")
        ):
            continue
        if path not in targets:
            targets.append(path)
    return " ".join(shlex.quote(path) for path in sorted(targets))


class StrixImageBase_1495_TO_866(Image):

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
            _BASE_DOCKERFILE.replace("__SYNTAX__", DockerfileEnhancer.SYNTAX_DIRECTIVE)
            .replace(
                "__INFRA__",
                DockerfileEnhancer._infrastructure_block(self, self.dependency()).rstrip("\n"),
            )
            .replace("__BASE_IMAGE__", self.dependency())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class StrixImageDefault_1495_TO_866(Image):

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
        return StrixImageBase_1495_TO_866(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__PATCH_APPLY_SETUP__", _PATCH_APPLY_SETUP)
            .replace("__TEST_TARGETS__", _test_targets(self.pr))
            .replace("__BINARY_EXCLUDES__", _patch_excludes(self.pr))
            .replace("__NON_TEST_PATHS__", _non_test_paths_shell(self.pr))
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


@Instance.register("usestrix", _INTERVAL_NAME)
class STRIX_1495_TO_866(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return StrixImageDefault_1495_TO_866(self.pr, self._config)

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


Instance.register("usestrix", "strix")(STRIX_1495_TO_866)
