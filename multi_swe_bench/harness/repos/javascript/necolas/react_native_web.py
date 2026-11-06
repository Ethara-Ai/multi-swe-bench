import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22.23.2-bookworm"
_BASE_TAG = "base"
_JEST_NODE_OPTIONS = (
    "--experimental-vm-modules --disable-warning=ExperimentalWarning "
    "--max-old-space-size=4096"
)


_BASE_TOOLCHAIN = r"""ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates build-essential python3 coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/"""


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

mkdir -p /home/__REPO__
cd /home/__REPO__

git init -q
git config --local advice.detachedHead false
git remote add origin https://github.com/__ORG__/__REPO__.git
fetched=0
for attempt in 1 2 3 4 5; do
    if git fetch --depth 1 --no-tags origin __BASE_SHA__; then
        fetched=1
        break
    fi
    echo "prepare: git fetch attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
test "$fetched" -eq 1
git checkout --detach __BASE_SHA__
git reset --hard
git clean -fdx
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

export CI=true
export HUSKY=0
export npm_config_cache=/tmp/rnw-npm-cache
export PUPPETEER_SKIP_DOWNLOAD=true
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

node --version
npm --version

installed=0
for attempt in 1 2 3; do
    npm ci --ignore-scripts --no-audit --no-fund || true
    if [ -f node_modules/.package-lock.json ]; then
        installed=1
        break
    fi
    echo "prepare: npm ci attempt ${attempt} did not complete; retrying in 15s" >&2
    sleep 15
done
test "$installed" -eq 1

rm -rf /tmp/rnw-npm-cache

test -x node_modules/.bin/jest
listed=$(NODE_OPTIONS="__JEST_NODE_OPTIONS__" node_modules/.bin/jest --listTests)
test -n "$listed"
node -e "for (const dep of ['jest', 'jest-environment-jsdom', 'babel-jest', '@testing-library/react', 'react', 'react-dom', 'react-native-web/package.json']) require.resolve(dep, {paths: ['/home/__REPO__']}); console.log('DEPS_OK')"
"""


_RESULTS_JS = r"""'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = '/home/__REPO__';

function emit(status, name) {
  process.stdout.write('\nRNW_TEST|' + status + '|' + name + '\n');
}

function clean(text) {
  return String(text).replace(/\s+/g, ' ').trim();
}

let report;
try {
  report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
} catch (e) {
  emit('FAIL', '<jest produced no report>');
  process.exit(0);
}

for (const suite of report.testResults || []) {
  const file = path.relative(ROOT, suite.name);
  const tests = suite.assertionResults || [];
  if (tests.length === 0) {
    if (suite.status === 'failed') {
      emit('FAIL', file + '::<suite failed to run>');
    }
    continue;
  }
  const seen = new Map();
  for (const test of tests) {
    const titles = (test.ancestorTitles || []).concat(test.title).map(clean).filter(Boolean);
    let name = file + '::' + titles.join(' > ');
    const count = (seen.get(name) || 0) + 1;
    seen.set(name, count);
    if (count > 1) {
      name += ' #' + count;
    }
    emit(test.status === 'passed' ? 'PASS' : test.status === 'failed' ? 'FAIL' : 'SKIP', name);
  }
}
"""


_TEST_BLOCK = r"""results=/tmp/rnw-results
rm -rf "$results"
mkdir -p "$results"
status=0

NODE_OPTIONS="__JEST_NODE_OPTIONS__" timeout --kill-after=60 3600 \
    node_modules/.bin/jest --ci --runInBand --json --outputFile="$results/jest.json" \
    < /dev/null || status=$?
node /home/rnw-results.cjs "$results/jest.json"

exit "$status"
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export NODE_ENV=test

cd /home/__REPO__

__TEST_BLOCK__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export NODE_ENV=test

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch

__TEST_BLOCK__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export NODE_ENV=test

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_BLOCK__"""


class ReactNativeWebImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        sections = [
            f"FROM {self.dependency()}",
            self.global_env,
            _BASE_TOOLCHAIN,
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(section for section in sections if section) + "\n"


class ReactNativeWebImageDefault(Image):
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
        return ReactNativeWebImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__JEST_NODE_OPTIONS__", _JEST_NODE_OPTIONS)
            .replace("__ORG__", self.pr.org)
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
            File(".", "rnw-results.cjs", self._render(_RESULTS_JS)),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        hardening = (
            Image._HARDENING_BLOCK.replace(" --aggressive", "")
            .replace('"${BASE_COMMIT}"', self.pr.base.sha)
            .strip("\n")
        )
        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency().image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__HARDENING__", hardening)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(r"^RNW_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}

    text = _ANSI_RE.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n")
    for line in text.splitlines():
        match = _RESULT_RE.match(line.strip())
        if match:
            buckets[match.group(1)].add(match.group(2))

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


@Instance.register("necolas", "react-native-web")
class REACT_NATIVE_WEB(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ReactNativeWebImageDefault(self.pr, self._config)

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
