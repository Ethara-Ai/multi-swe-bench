from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

_INTERVAL_NAME = "gatsby_34337_to_34337"

PR_LOW = 34337
PR_HIGH = 34337

_NODE_BASE = "node:14-bullseye-slim"

_PREACT_VERSION = "10.6.4"
_PREACT_SHA512 = (
    "5b2a2c33ba7119c9dd53c858d0e4252dde78b4e53eaa61b8e505e3d9d018acbd"
    "751e8c94fc4cce49396926c8ab6c1af5416ee5202c52b1524999c994825a2f1d"
)

_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def _gold_test_paths(test_patch: str) -> tuple[list[str], list[str]]:
    text = (test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    all_paths = {m.group(2) for m in _DIFF_GIT_RE.finditer(text)}
    existing = {p for p in get_modified_files(test_patch or "")}
    created = all_paths - existing
    return sorted(existing), sorted(created)


def _restore_gold_tests(test_patch: str, base_sha: str) -> str:
    existing, created = _gold_test_paths(test_patch)
    every = sorted(set(existing) | set(created))
    if not every:
        return ""

    lines = ['echo "=== reward-hacking guard: re-establishing gold test files ==="']
    if existing:
        quoted = " ".join(shlex.quote(p) for p in existing)
        lines.append(f"git checkout {shlex.quote(base_sha)} -- {quoted}")
    if created:
        lines.append(f"rm -f {' '.join(shlex.quote(p) for p in created)}")
    includes = " ".join(f"--include={shlex.quote(p)}" for p in every)
    lines.append(f"git apply --whitespace=nowarn {includes} /home/test.patch")
    return "\n".join(lines) + "\n"


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


_SELECT_TESTS = r"""
CHANGED=$(cat /home/test.patch /home/fix.patch 2>/dev/null \
  | sed -n -e 's|^diff --git a/\(.*\) b/\(.*\)$|\1\n\2|p' | sort -u)

SELECTED=$(printf '%s\n' "$CHANGED" \
  | grep -E '\.(js|jsx|ts|tsx|mjs|cjs)$' \
  | grep -v -E '(^|/)__snapshots__/' \
  | grep -E '(^|/)__tests__/|\.(test|spec)\.[a-z]+$' \
  | sort -u || true)

PATTERN=""
for candidate in $SELECTED; do
  escaped=$(printf '%s' "$candidate" | sed 's/[][().*+?^$\\|{}]/\\&/g')
  PATTERN="${PATTERN}${PATTERN:+|}/${escaped}\$"
done

printf '%s' "$PATTERN" > /home/jest_pattern.txt
echo "=== selected test path pattern: $PATTERN ==="

test -s /home/jest_pattern.txt
"""


_EXEC_TESTS = r"""
JEST_PATTERN=$(cat /home/jest_pattern.txt)
echo "=== running jest for pattern: $JEST_PATTERN ==="

STATUS=0
node ./node_modules/.bin/jest \
  --ci \
  --verbose \
  --runInBand \
  --forceExit \
  --notify=false \
  --reporters=default \
  --testPathPattern "$JEST_PATTERN" 2>&1 || STATUS=$?

echo "=== jest exited with status $STATUS ==="
exit $STATUS
"""


_INSTALL = r"""
installed=0
for attempt in 1 2 3 4 5; do
  yarn install --frozen-lockfile --ignore-scripts --ignore-engines \
    --non-interactive --no-progress --network-timeout 600000 && installed=1 || true
  if [ "$installed" -eq 1 ]; then
    break
  fi
  echo "=== install attempt ${attempt} failed, retrying in 20s ==="
  sleep 20
done
test "$installed" -eq 1
"""


_VENDOR_PREACT = (
    r"""
fetched=0
for attempt in 1 2 3 4 5; do
  curl -fsSL "https://registry.npmjs.org/preact/-/preact-__PREACT_VERSION__.tgz" \
    -o /tmp/preact.tgz && fetched=1 || true
  if [ "$fetched" -eq 1 ]; then
    break
  fi
  echo "=== preact download attempt ${attempt} failed, retrying in 20s ==="
  sleep 20
done
test "$fetched" -eq 1

echo "__PREACT_SHA512__  /tmp/preact.tgz" | sha512sum -c -

rm -rf node_modules/preact
mkdir -p node_modules/preact
tar -xzf /tmp/preact.tgz --strip-components=1 -C node_modules/preact
rm -f /tmp/preact.tgz
echo "=== vendored preact@__PREACT_VERSION__ into node_modules ==="
"""
).replace("__PREACT_VERSION__", _PREACT_VERSION).replace(
    "__PREACT_SHA512__", _PREACT_SHA512
)


_BUILD = r"""
echo "=== building packages/gatsby src -> dist ==="
if ! ( cd packages/gatsby \
         && NODE_ENV=production ../../node_modules/.bin/babel src \
              --out-dir dist \
              --source-maps \
              --ignore "**/gatsby-cli.js,src/internal-plugins/dev-404-page/raw_dev-404-page.js,**/__tests__,**/__mocks__" \
              --extensions ".ts,.js" ) > /home/build.log 2>&1; then
  echo "=== build FAILED; full output follows ==="
  cat /home/build.log
  exit 1
fi
echo "=== build ok ==="

rm -rf /usr/local/share/.cache/yarn /root/.cache/yarn /root/.npm /home/build.log
"""


_RESET = r"""
git reset --hard -q
git clean -fdq
"""


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/__REPO__

apply_patches() {
    if git apply --whitespace=nowarn "$@"; then
        return 0
    fi
    echo "=== MSB_PATCH_3WAY_FALLBACK: plain git apply failed, retrying with --3way ===" >&2
    git reset --hard -q
    git clean -fdq
    git apply --3way --whitespace=nowarn "$@"
}
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

__RESTORE_GOLD_TESTS__
"""


_PREPARE_SH = (
    r"""#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/__REPO__

git init -q .
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/__ORG__/__REPO__.git"

fetched=0
for attempt in 1 2 3 4 5; do
  if git fetch --depth 1 --no-tags origin __BASE_SHA__; then
    fetched=1
    break
  fi
  echo "=== fetch attempt ${attempt} failed, retrying in 20s ==="
  sleep 20
done
test "$fetched" -eq 1

git checkout --detach __BASE_SHA__
git reset --hard -q
git clean -fdxq
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
echo "=== HEAD pinned at $(git rev-parse HEAD) ==="
"""
    + _INSTALL
    + _VENDOR_PREACT
    + _BUILD
    + r"""
node -e "require.resolve('preact/compat'); require.resolve('preact/compat/server'); require.resolve('gatsby/dist/utils/fast-refresh-module'); require.resolve('@prefresh/webpack'); require.resolve('@pmmmwh/react-refresh-webpack-plugin'); require('jest-extended'); require('jest-serializer-path'); console.log('DEPS_OK: runtime deps resolve')"
node ./node_modules/.bin/jest --version
echo "DEPS_OK: jest cli runs"
"""
    + _SELECT_TESTS
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

RUN sed -i "/-security/d;/-updates/d" /etc/apt/sources.list \
 && sed -i "s|deb.debian.org|archive.debian.org|g" /etc/apt/sources.list \
 && apt-get update -o Acquire::Check-Valid-Until=false \
 && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    build-essential \
    python3 \
 && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

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


class GatsbyBundleImageBase(Image):
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


class GatsbyBundleImageDefault(Image):
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
        return GatsbyBundleImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
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
            File(
                ".",
                "fix-run.sh",
                self._render(_FIX_RUN_SH).replace(
                    "__RESTORE_GOLD_TESTS__",
                    _restore_gold_tests(self.pr.test_patch, self.pr.base.sha),
                ),
            ),
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


def gatsby_bundle_parse_log(test_log: str) -> TestResult:
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


@Instance.register("gatsbyjs", _INTERVAL_NAME)
class GATSBY_34337_TO_34337(Instance):
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
        return GatsbyBundleImageDefault(self.pr, self._config)

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
        return gatsby_bundle_parse_log(test_log)


@Instance.register("gatsbyjs", "gatsby")
class _GatsbyIntervalRouter(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if PR_LOW <= pr.number <= PR_HIGH:
            return GATSBY_34337_TO_34337(pr, config, *args, **kwargs)
        raise ValueError(
            f"gatsbyjs/gatsby PR #{pr.number} is outside {PR_LOW}-{PR_HIGH}; "
            f"set number_interval on the dataset row so it routes to its own era config."
        )


for _key in (str(PR_LOW), f"{PR_LOW}_to_{PR_HIGH}"):
    Instance._registry.setdefault(f"gatsbyjs/{_key}", _GatsbyIntervalRouter)

del _key
