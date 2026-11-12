import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "learn_claude_code_548_to_326"
_PYTHON_IMAGE = "python:3.11-slim-bookworm"
_UV = "0.13.6"
_VENV = "/opt/learn-claude-code-venv"

_BASE_DOCKERFILE = r"""__SYNTAX__

FROM __BASE_IMAGE__

__INFRA__

ENV LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        build-essential \
        pkg-config \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# The glibc uv binary segfaults under QEMU amd64 emulation on arm64 build hosts,
# so amd64 uses the statically linked musl release of the same uv version.
RUN set -eux; \
    if [ "${TARGETARCH:-}" = "amd64" ]; then \
        curl -fsSL "https://github.com/astral-sh/uv/releases/download/__UV__/uv-x86_64-unknown-linux-musl.tar.gz" \
            | tar -xz -C /tmp; \
        install -m 0755 /tmp/uv-x86_64-unknown-linux-musl/uv /tmp/uv-x86_64-unknown-linux-musl/uvx /usr/local/bin/; \
        rm -rf /tmp/uv-x86_64-unknown-linux-musl; \
    else \
        python -m pip install --no-cache-dir "uv==__UV__"; \
    fi; \
    uv --version

RUN git config --global --add safe.directory '*'

__GLOBAL_ENV__

WORKDIR /home/

__CODE__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""

_CLONE_CODE = 'RUN git clone "${REPO_URL}" /home/__REPO__'

_COPY_CODE = "COPY __REPO__ /home/__REPO__"

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

_RUN_ENV = r"""export CI=true
export TZ=UTC
export VIRTUAL_ENV=__VENV__
export PATH=__VENV__/bin:$PATH
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

__RUN_ENV__
export UV_LINK_MODE=copy
export UV_PYTHON_DOWNLOADS=never

EXCLUDE_NEWER="$(git log -1 --format=%cI HEAD)"
uv venv --python /usr/local/bin/python3 __VENV__
uv pip install --python __VENV__/bin/python --exclude-newer "$EXCLUDE_NEWER" -r requirements.txt pytest || true
# Fail the build only if the environment is genuinely unusable.
__VENV__/bin/python -c "import anthropic, dotenv, yaml, pytest; print('DEPS_OK')"
bash /home/check_git_changes.sh
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

python -m pytest ${TEST_TARGETS} -v -rA --tb=short --color=no -p no:cacheprovider --continue-on-collection-errors"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

__RUN_ENV__

__TEST_BLOCK__
"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

__RUN_ENV__

git apply --whitespace=nowarn /home/test.patch

__TEST_BLOCK__
"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

__RUN_ENV__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_BLOCK__
"""

_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
_TEST_FILE_RE = re.compile(r"^tests/(?:[^/]+/)*test_[^/]+\.py$")


def _test_targets(pr: PullRequest) -> str:
    targets = {
        path
        for path in _DIFF_HEADER_RE.findall(pr.test_patch or "")
        if _TEST_FILE_RE.match(path)
    }
    return " ".join(shlex.quote(path) for path in sorted(targets))


class LearnClaudeCode548To326ImageBase(Image):
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
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

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
            .replace("__UV__", _UV)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class LearnClaudeCode548To326ImageDefault(Image):
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
        return LearnClaudeCode548To326ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__RUN_ENV__", _RUN_ENV)
            .replace("__TEST_TARGETS__", _test_targets(self.pr))
            .replace("__VENV__", _VENV)
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
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        hardening = Image._HARDENING_BLOCK.replace(
            '"${BASE_COMMIT}"', self.pr.base.sha
        )
        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency().image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__HARDENING__", hardening)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VERBOSE_RE = re.compile(
    r"^(?P<id>\S+\.py::\S.*?)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\(.*\))?\s*(?:\[\s*\d+%\])?\s*$"
)
_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<id>\S+\.py(?:::\S.*?)?)(?:\s+-\s+.*)?$"
)
_SUBTEST_RE = re.compile(
    r"^(?P<status>SUBFAILED|SUBFAIL)\b.*?\s(?P<id>\S+\.py::\S.*?)(?:\s+-\s+.*)?$"
)


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(name: str, status: str) -> None:
        if "::" not in name:
            name = f"{name}::<collection error>"
        if status in ("PASSED", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    for raw_line in _ANSI_RE.sub("", test_log).replace("\r\n", "\n").splitlines():
        line = raw_line.strip()
        match = _VERBOSE_RE.match(line) or _SUMMARY_RE.match(line)
        if match:
            record(match.group("id"), match.group("status"))
            continue

        match = _SUBTEST_RE.match(line)
        if match:
            record(match.group("id"), "FAILED")

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


@Instance.register("shareAI-lab", _INTERVAL)
class LearnClaudeCode548To326(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return LearnClaudeCode548To326ImageDefault(self.pr, self._config)

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


Instance.register("shareAI-lab", "learn-claude-code")(LearnClaudeCode548To326)
