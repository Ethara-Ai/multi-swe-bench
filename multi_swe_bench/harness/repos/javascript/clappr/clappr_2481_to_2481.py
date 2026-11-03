import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-2481_to_2481"


_BASE_DOCKERFILE = r"""FROM __BASE_IMAGE__

ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --force yarn@1.22.21 && test "$(yarn --version)" = "1.22.21"

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

node -e "for (const m of ['jest', 'jest-environment-jsdom', 'babel-jest']) require.resolve(m + '/package.json'); console.log('DEPS_OK')"
test -x node_modules/.bin/lerna
test -x node_modules/.bin/rollup
for pkg in packages/*/; do
    pkg=${pkg%/}
    [ -n "$(find "$pkg/src" -name '*.test.js' -not -path '*/node_modules/*' -print -quit 2>/dev/null)" ] || continue
    [ ! -f "$pkg/jest.config.js" ] || (cd "$pkg" && node -e "require('./jest.config.js')")
done
"""


_JEST_RESULTS_JS = r"""'use strict'

const fs = require('fs')
const path = require('path')

const [pkg, reportFile] = process.argv.slice(2)
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
  emit('FAIL', pkg + '::<jest produced no report>')
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
export NX_DAEMON=false NX_NO_CLOUD=true NX_SKIP_NX_CACHE=true

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

has_script() {
    node -e "process.exit((require('./' + process.argv[1]).scripts || {})[process.argv[2]] ? 0 : 1)" "$1" "$2"
}

has_tests() {
    [ -n "$(find "$1/src" -name '*.test.js' -not -path '*/node_modules/*' -print -quit 2>/dev/null)" ]
}

set +e

rm -rf packages/*/dist
build_rc=0
for pkg in packages/*/; do
    pkg=${pkg%/}
    has_tests "$pkg" || continue
    hooks="pretest"
    if [ -f "$pkg/src/dist.smoke.test.js" ]; then
        hooks="pretest:smoke pretest"
    fi
    for hook in $hooks; do
        if has_script "$pkg/package.json" "$hook"; then
            (cd "$pkg" && yarn run "$hook")
            rc=$?
            [ "$rc" -eq 0 ] || build_rc=$rc
        fi
    done
done
if has_script package.json build:dist; then
    yarn run build:dist
else
    node_modules/.bin/lerna run build --scope '@clappr/*' --no-bail
fi
rc=$?
[ "$rc" -eq 0 ] || build_rc=$rc

export NODE_ENV=test
mkdir -p /tmp/jest-reports
jest_rc=0
for pkg in packages/*/; do
    pkg=${pkg%/}
    has_tests "$pkg" || continue
    report="/tmp/jest-reports/$(basename "$pkg").json"
    rm -f "$report"
    (cd "$pkg" && PATH="$PWD/node_modules/.bin:/home/__REPO__/node_modules/.bin:$PATH" \
        jest src/ --ci --silent --json --outputFile="$report" --testPathIgnorePatterns=/node_modules/)
    rc=$?
    [ "$rc" -eq 0 ] || jest_rc=$rc
    node /home/jest-results.js "$pkg" "$report"
done

set -e

echo "STAGE_EXIT build=${build_rc} jest=${jest_rc}"
exit $(( build_rc != 0 || jest_rc != 0 ))
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


class CLAPPR_2481_TO_2481_ImageBase(Image):

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


class CLAPPR_2481_TO_2481_ImageDefault(Image):

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
        return CLAPPR_2481_TO_2481_ImageBase(self.pr, self._config)

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


@Instance.register("clappr", "clappr_2481_to_2481")
class CLAPPR_2481_TO_2481(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return CLAPPR_2481_TO_2481_ImageDefault(self.pr, self._config)

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
