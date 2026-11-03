import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-4253_to_4253"


_BASE_DOCKERFILE = r"""FROM __BASE_IMAGE__

ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --force yarn@1.22.22 && test "$(yarn --version)" = "1.22.22"

RUN git config --global --add safe.directory '*'

__GLOBAL_ENV__

WORKDIR /home/

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


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
for attempt in 1 2 3; do
    if git fetch --depth 1 --no-tags origin __BASE_SHA__; then
        break
    fi
    echo "prepare: git fetch attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
git checkout --detach __BASE_SHA__
git reset --hard
git clean -fdx
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

export CI=true

node --version
yarn --version

for attempt in 1 2 3; do
    yarn install --frozen-lockfile --ignore-scripts --network-timeout 600000 || true
    if [ -f node_modules/.yarn-integrity ]; then
        break
    fi
    echo "prepare: yarn install attempt ${attempt} did not complete; retrying in 15s" >&2
    sleep 15
done
test -f node_modules/.yarn-integrity

if node -e "process.exit((require('./package.json').scripts || {}).postinstall ? 0 : 1)"; then
    yarn run postinstall
fi

if [ -f node_modules/canvas/package.json ] && [ ! -f node_modules/canvas/build/Release/canvas.node ]; then
    (cd node_modules/canvas && npm run install)
fi

node -e "for (const m of ['jest', 'jest-environment-jsdom', 'babel-jest', 'jest-junit']) require.resolve(m + '/package.json'); console.log('DEPS_OK')"
[ ! -f node_modules/canvas/package.json ] || test -f node_modules/canvas/build/Release/canvas.node
node -e "require('./jest.config.js')"
"""


_JEST_RESULTS_JS = r"""'use strict'

const fs = require('fs')
const path = require('path')

const reportFile = process.argv[2]
const repo = '/home/__REPO__'

function clean(text) {
  return String(text).replace(/\s+/g, ' ').trim()
}

function emit(status, name) {
  process.stdout.write('JEST_TEST|' + status + '|' + name + '\n')
}

let report
try {
  report = JSON.parse(fs.readFileSync(reportFile, 'utf8'))
} catch (e) {
  emit('FAIL', '<jest produced no report>')
  process.exit(0)
}

for (const suite of report.testResults || []) {
  const file = path.relative(repo, suite.name)
  const tests = suite.assertionResults || []
  if (tests.length === 0) {
    if (suite.status === 'failed') emit('FAIL', file + '::<suite failed to run>')
    continue
  }
  const seen = new Map()
  for (const test of tests) {
    const titles = (test.ancestorTitles || []).concat(test.title).map(clean).filter(Boolean)
    let name = file + '::' + titles.join(' > ')
    const count = (seen.get(name) || 0) + 1
    seen.set(name, count)
    if (count > 1) name += ' #' + count
    emit(test.status === 'passed' ? 'PASS' : test.status === 'failed' ? 'FAIL' : 'SKIP', name)
  }
}
"""


_TEST_BLOCK = r"""export NODE_OPTIONS=--max-old-space-size=4096

synced=0
for attempt in 1 2 3; do
    if yarn install --frozen-lockfile --ignore-scripts --prefer-offline --network-timeout 600000; then
        synced=1
        break
    fi
    echo "yarn install attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
test "$synced" -eq 1

if [ -f node_modules/canvas/package.json ] && [ ! -f node_modules/canvas/build/Release/canvas.node ]; then
    (cd node_modules/canvas && npm run install)
fi

export NODE_ENV=test

set +e

rm -f /tmp/jest-report.json
node_modules/.bin/jest --ci --silent --passWithNoTests --json --outputFile=/tmp/jest-report.json
jest_rc=$?
node /home/jest-results.js /tmp/jest-report.json

set -e

echo "STAGE_EXIT jest=${jest_rc}"
exit "$jest_rc"
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

__TEST_BLOCK__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch

__TEST_BLOCK__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_BLOCK__"""


class NOTIONNEXT_4253_TO_4253_ImageBase(Image):

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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
        )


class NOTIONNEXT_4253_TO_4253_ImageDefault(Image):

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
        return NOTIONNEXT_4253_TO_4253_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "jest-results.js", self._render(_JEST_RESULTS_JS)),
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

        hardening = (
            Image._HARDENING_BLOCK
            .replace(" --aggressive", "")
            .replace('"${BASE_COMMIT}"', self.pr.base.sha)
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
_JEST_TEST_RE = re.compile(r"^JEST_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}

    for line in _ANSI_RE.sub("", test_log).replace("\r\n", "\n").splitlines():
        match = _JEST_TEST_RE.match(line)
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


@Instance.register("notionnext-org", "NotionNext_4253_to_4253")
class NOTIONNEXT_4253_TO_4253(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return NOTIONNEXT_4253_TO_4253_ImageDefault(self.pr, self._config)

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
