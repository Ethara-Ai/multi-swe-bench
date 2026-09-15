import re
from typing import Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "hoverboard_835_to_877"

_ORG = "gdg-x"
_REPO = "hoverboard"

_TEST_CMD = (
    "npx --no-install jest --ci --verbose --forceExit "
    "--maxWorkers=2 --selectProjects Web"
)

_TOOLCHAIN_ENV = (
    "CI=true",
    "NODE_ENV=test",
    "NODE_NO_WARNINGS=1",
    "NODE_OPTIONS=--max-old-space-size=4096",
    "NPM_CONFIG_AUDIT=false",
    "NPM_CONFIG_FUND=false",
    "NPM_CONFIG_PROGRESS=false",
    "NPM_CONFIG_LOGLEVEL=warn",
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


_DEPS_GATE = (
    "node -e \""
    "const fs=require('fs');"
    "const p=require('./package.json');"
    "const want=Object.keys(p.dependencies||{}).concat(Object.keys(p.devDependencies||{}));"
    "const miss=want.filter(function(d){return !fs.existsSync('node_modules/'+d);});"
    "if(miss.length){console.error('MISSING_DEPS: '+miss.join(', '));process.exit(1);}"
    "console.log('__LABEL__');"
    '"'
)


_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

MANIFEST_PATCHED=0
if git apply --check --whitespace=nowarn \
        --include=package.json --include=package-lock.json \
        /home/fix.patch 2>/dev/null; then
    git apply --whitespace=nowarn \
        --include=package.json --include=package-lock.json \
        /home/fix.patch
    MANIFEST_PATCHED=1
    echo "=== prepare: merged fix-patch manifests for the union install ==="
else
    echo "=== prepare: fix patch does not touch the manifests ==="
fi

npm ci --ignore-scripts --no-audit --unsafe-perm \
    || npm install --ignore-scripts --no-audit --unsafe-perm \
    || true

__UNION_GATE__

if [ "$MANIFEST_PATCHED" = "1" ]; then
    git reset --hard
fi

test -x node_modules/.bin/jest
test -x node_modules/.bin/ts-jest
__BASE_GATE__
"""


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_NO_WARNINGS=1

cd /home/__REPO__
"""


_RESET = r"""
git reset --hard
"""

_APPLY_TEST_PATCH = r"""
git apply --whitespace=nowarn /home/test.patch
"""

_APPLY_BOTH_PATCHES = r"""
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
"""


_EXEC_TESTS = r"""
echo "=== Running: __TEST_CMD__ ==="
STATUS=0
__TEST_CMD__ || STATUS=$?
echo "=== Test run complete (exit ${STATUS}) ==="
exit $STATUS
"""


_RUN_SH = _SCRIPT_HEADER + _RESET + _EXEC_TESTS
_TEST_RUN_SH = _SCRIPT_HEADER + _RESET + _APPLY_TEST_PATCH + _EXEC_TESTS
_FIX_RUN_SH = _SCRIPT_HEADER + _RESET + _APPLY_BOTH_PATCHES + _EXEC_TESTS


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

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__ && \
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

ARG BASE_COMMIT="__BASE_SHA__"

WORKDIR /home/__REPO__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

__HARDENING__

__CLEAR_ENV__
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_DURATION_RE = re.compile(r"\s*\((?:\d+(?:[.,]\d+)?\s*(?:ms|s|m)\s*)+\)\s*$")
_RETRY_RE = re.compile(r"\s*\(retry\s*#\d+\)\s*$")

_PASS_SYMBOLS = "\u2713\u2714\u221a"
_FAIL_SYMBOLS = "\u2715\u2717\u00d7\u2718"
_SKIP_SYMBOLS = "\u25cb\u25ef\u270e\u2193"

_JEST_SUITE_RE = re.compile(
    r"^(PASS|FAIL)\s+(?:(\S+)\s+)?"
    r"(\S+\.(?:test|spec)\.[cm]?[jt]sx?)"
    r"(?:\s+\([^)]*\))?\s*$"
)

_JEST_TEST_RE = re.compile(
    r"^(\s+)([" + _PASS_SYMBOLS + _FAIL_SYMBOLS + _SKIP_SYMBOLS + r"])\s+(\S.*)$"
)

_JEST_BLOCK_TERMINATORS = (
    "Test Suites:",
    "Tests:",
    "Snapshots:",
    "Time:",
    "Ran all test suites",
    "Summary of all failing tests",
    "console.log",
    "console.error",
    "console.warn",
    "console.info",
    "console.debug",
    "at ",
)

_JEST_SKIP_PREFIXES = ("skipped ", "todo ")

_SEP = " > "


def _clean_title(title: str) -> str:
    title = _DURATION_RE.sub("", title.strip())
    title = _RETRY_RE.sub("", title).strip()
    return title


def hoverboard_era_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(symbol: str, name: str) -> None:
        if not name:
            return
        if symbol in _PASS_SYMBOLS:
            passed_tests.add(name)
        elif symbol in _FAIL_SYMBOLS:
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    current_file: str | None = None
    describe_stack: list[str] = []
    in_jest_block = False

    for raw_line in test_log.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        stripped = line.strip()

        suite_match = _JEST_SUITE_RE.match(stripped)
        if suite_match:
            current_file = suite_match.group(3)
            describe_stack = []
            in_jest_block = True
            if suite_match.group(1) == "PASS":
                passed_tests.add(current_file)
            else:
                failed_tests.add(current_file)
            continue

        if not stripped:
            continue

        if not in_jest_block:
            continue

        if stripped.startswith("\u25cf") or stripped.startswith(
            _JEST_BLOCK_TERMINATORS
        ):
            in_jest_block = False
            continue

        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_jest_block = False
            continue

        test_match = _JEST_TEST_RE.match(line)
        if test_match:
            symbol = test_match.group(2)
            title = _clean_title(test_match.group(3))
            if symbol in _SKIP_SYMBOLS:
                for prefix in _JEST_SKIP_PREFIXES:
                    if title.startswith(prefix):
                        title = title[len(prefix) :].strip()
                        break
            context = describe_stack[: max(indent // 2 - 1, 0)]
            parts = ([current_file] if current_file else []) + context + [title]
            record(symbol, _SEP.join(part for part in parts if part))
        else:
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


class HoverboardEraImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "node:12-bullseye"

    def image_tag(self) -> str:
        return "base-835_to_877"

    def workdir(self) -> str:
        return "base-835_to_877"

    def files(self) -> list[File]:
        return []

    def _merged_env_block(self) -> str:
        assignments = [
            line[len("ENV ") :]
            for line in self.global_env.splitlines()
            if line.startswith("ENV ")
        ]
        assignments.extend(_TOOLCHAIN_ENV)
        return DockerfileEnhancer._ENV_BLOCK + "".join(
            " \\\n    " + assignment for assignment in assignments
        )

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", self._merged_env_block())
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
            .replace("__CLEAR_ENV__", self.clear_env)
        )


class HoverboardEraImageDefault(Image):
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
        return HoverboardEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_CMD__", _TEST_CMD)
            .replace("__UNION_GATE__", _DEPS_GATE.replace("__LABEL__", "UNION_DEPS_OK"))
            .replace("__BASE_GATE__", _DEPS_GATE.replace("__LABEL__", "DEPS_OK"))
            .replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
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
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__HARDENING__", Image._HARDENING_BLOCK)
            .replace("__CLEAR_ENV__", self.clear_env)
        )


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _INTERVAL_NAME)
class HOVERBOARD_835_TO_877(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return HoverboardEraImageDefault(self.pr, self._config)

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
        return hoverboard_era_parse_log(test_log)
