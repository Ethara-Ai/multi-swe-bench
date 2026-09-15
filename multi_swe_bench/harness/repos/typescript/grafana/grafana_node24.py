from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "grafana"
_REPO = "grafana"

_NODE_BASE = "node:24.11.0-bookworm"
_BASE_TAG = "base-node24"

_REPO_DIR = f"/home/{_REPO}"
_NODE_OPTIONS = "--max-old-space-size=8192"
_FALLBACK_SCOPE = "packages/grafana-ui/src/components/VizLegend"

_JEST_CMD = "yarn jest --ci --verbose --runInBand --watchAll=false __TEST_SCOPE__"


def _test_scope(pr: PullRequest) -> str:
    dirs: list[str] = []
    for line in (pr.test_patch or "").split("\n"):
        if not line.startswith("diff --git a/"):
            continue
        path = line.split(" a/", 1)[1].split(" b/", 1)[0]
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        if directory.endswith("/__snapshots__"):
            directory = directory[: -len("/__snapshots__")]
        if directory and directory not in dirs:
            dirs.append(directory)
    return " ".join(dirs) if dirs else _FALLBACK_SCOPE


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
node --version
yarn --version
node -e "require('./package.json'); console.log('DEPS_OK: package.json')"
node -e "require.resolve('jest'); require.resolve('ts-jest'); console.log('DEPS_OK: jest + ts-jest resolved')"
yarn jest --version
echo "DEPS_OK: jest runs"
yarn jest --ci --listTests __TEST_SCOPE__ > /dev/null
echo "DEPS_OK: jest resolves scoped tests"
"""


_PREPARE_SH = (
    r"""#!/bin/bash
set -e

export CI=true
export DEBIAN_FRONTEND=noninteractive
export NODE_OPTIONS=__NODE_OPTIONS__
export CYPRESS_INSTALL_BINARY=0

cd __REPO_DIR__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin __BASE_SHA__
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
echo "=== HEAD pinned at $(git rev-parse HEAD) ==="

corepack enable
corepack install

yarn install --immutable
"""
    + _VERIFY
)


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS=__NODE_OPTIONS__
export FORCE_COLOR=0
export NO_COLOR=1
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


_EXEC_TESTS = "\n" + _JEST_CMD + "\n"


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

ENV NODE_OPTIONS=__NODE_OPTIONS__ \
    CYPRESS_INSTALL_BINARY=0 \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \
    NO_COLOR=1 \
    FORCE_COLOR=0 \
    CI=true

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    jq \
 && rm -rf /var/lib/apt/lists/*

RUN corepack enable

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


class GrafanaEraImageBase(Image):
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
        return _NODE_BASE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

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
            .replace("__NODE_OPTIONS__", _NODE_OPTIONS)
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", DockerfileEnhancer._ENV_BLOCK)
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
        )


class GrafanaEraImageDefault(Image):
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
        return GrafanaEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__NODE_OPTIONS__", _NODE_OPTIONS)
            .replace("__TEST_SCOPE__", _test_scope(self.pr))
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

_SUITE_RE = re.compile(
    r"^ {0,2}(?:PASS|FAIL)\s+(?:\[[^\]]*\]\s+)?"
    r"(\S+\.[cm]?[jt]sx?)"
    r"(?:\s+\(\d+(?:[.,]\d+)?\s*m?s\))?\s*$"
)

_PASS_MARKS = "\u2713\u2714"
_FAIL_MARKS = "\u2715\u2717\u00d7\u2718"
_SKIP_MARKS = "\u25cb\u25ef\u270e\u2193"

_TEST_LINE_RE = re.compile(
    r"^(\s+)([" + _PASS_MARKS + _FAIL_MARKS + _SKIP_MARKS + r"])\s+(\S.*)$"
)

_DURATION_RE = re.compile(r"\s*\(\d+(?:[.,]\d+)?\s*m?s\)\s*$")

_BLOCK_END_PREFIXES = (
    "\u25cf",
    "Test Suites:",
    "Tests:",
    "Snapshots:",
    "Time:",
    "Ran all test suites",
    "Summary of all failing tests",
)

_CONSOLE_RE = re.compile(r"^\s*console\.[a-zA-Z]+\b")
_CONSOLE_CONTENT_MIN_INDENT = 3

_SKIP_AGGREGATE_RE = re.compile(r"^(?:skipped|todo)\s+\d+\s+tests?$")
_SKIP_PREFIX_RE = re.compile(r"^(?:skipped|todo)\s+")


def _clean_title(title: str) -> str:
    return _DURATION_RE.sub("", title.strip()).strip()


def grafana_jest_parse_log(test_log: str) -> TestResult:
    log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(mark: str, name: str) -> None:
        if not name:
            return
        if mark in _PASS_MARKS:
            passed_tests.add(name)
        elif mark in _FAIL_MARKS:
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    current_file: str | None = None
    describe_stack: list[str] = []
    in_suite = False
    in_console = False

    for raw_line in log.splitlines():
        line = raw_line.rstrip()

        suite = _SUITE_RE.match(line)
        if suite:
            current_file = suite.group(1)
            describe_stack = []
            in_suite = True
            in_console = False
            continue

        if not in_suite:
            continue

        stripped = line.strip()
        if not stripped:
            continue

        indent = len(line) - len(line.lstrip(" "))

        if in_console:
            if indent > _CONSOLE_CONTENT_MIN_INDENT or _CONSOLE_RE.match(line):
                continue
            in_console = False

        if _CONSOLE_RE.match(line):
            in_console = True
            continue

        if stripped.startswith(_BLOCK_END_PREFIXES):
            in_suite = False
            continue

        if indent == 0:
            continue

        test = _TEST_LINE_RE.match(line)
        if test:
            mark = test.group(2)
            title = _clean_title(test.group(3))
            if mark in _SKIP_MARKS:
                if _SKIP_AGGREGATE_RE.match(title):
                    continue
                title = _SKIP_PREFIX_RE.sub("", title).strip()
            context = describe_stack[: max(indent // 2 - 1, 0)]
            parts = ([current_file] if current_file else []) + context + [title]
            record(mark, " > ".join(part for part in parts if part))
            continue

        level = max(indent // 2, 1)
        describe_stack = describe_stack[: level - 1]
        describe_stack.append(stripped)

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


@Instance.register(_ORG, _REPO)
class GRAFANA(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return GrafanaEraImageDefault(self.pr, self._config)

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
        return grafana_jest_parse_log(test_log)
