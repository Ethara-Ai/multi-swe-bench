import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24.18.0-bookworm"
_MOCHA_VERSION = "10.2.0"
_BASE_TAG = "base"


_BASE_TOOLCHAIN = r"""ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates build-essential python3 coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-audit --no-fund mocha@__MOCHA_VERSION__ \
    && test "$(mocha --version)" = "__MOCHA_VERSION__"

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
export npm_config_cache=/tmp/wekan-npm-cache
export PUPPETEER_SKIP_DOWNLOAD=true

node --version
npm --version
mocha --version

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

rm -rf /tmp/wekan-npm-cache

test "$(mocha --version)" = "__MOCHA_VERSION__"
node -e "require('/home/__REPO__/package.json'); console.log('PACKAGE_OK')"
NODE_OPTIONS=--import=file:///home/wekan-resolve.mjs node --input-type=module -e "await import('chai'); await import('sinon'); console.log('DEPS_OK')"
"""


_RESOLVE_MJS = r"""import { registerHooks } from 'node:module';
import { statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = process.cwd();
const suffixes = ['', '.js', '.mjs', '.cjs', '.json', '/index.js', '/index.mjs', '/index.cjs'];

function isFile(candidate) {
  try {
    return statSync(candidate).isFile();
  } catch (error) {
    return false;
  }
}

function fallbackBase(specifier, parentURL) {
  if (specifier.startsWith('/') && !specifier.startsWith('//')) {
    return path.join(root, specifier);
  }
  if ((specifier.startsWith('./') || specifier.startsWith('../')) && parentURL && parentURL.startsWith('file:')) {
    return path.resolve(path.dirname(fileURLToPath(parentURL)), specifier);
  }
  return null;
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context);
    } catch (error) {
      const base = fallbackBase(specifier, context.parentURL);
      const found = base ? suffixes.map(suffix => base + suffix).find(isFile) : undefined;
      if (found) {
        return { url: pathToFileURL(found).href, shortCircuit: true };
      }
      throw error;
    }
  },
});
"""


_REPORTER_CJS = r"""'use strict';

module.exports = function WekanMochaReporter(runner) {
  const file = process.env.WEKAN_TEST_FILE || 'unknown';
  const seen = new Map();
  const clean = text => String(text).replace(/\s+/g, ' ').trim();
  const report = (status, runnable) => {
    const titles = typeof runnable.titlePath === 'function' ? runnable.titlePath() : [runnable.title];
    let name = file + '::' + titles.map(clean).filter(Boolean).join(' > ');
    const count = (seen.get(name) || 0) + 1;
    seen.set(name, count);
    if (count > 1) {
      name += ' #' + count;
    }
    process.stdout.write('\nWEKAN_TEST|' + status + '|' + name + '\n');
  };
  runner.on('pass', test => report('PASS', test));
  runner.on('fail', runnable => report('FAIL', runnable));
  runner.on('pending', test => report('SKIP', test));
};
"""


_TEST_BLOCK = r"""results=/tmp/wekan-results
rm -rf "$results"
mkdir -p "$results"
status=0

report() {
    printf '\nWEKAN_TEST|%s|%s\n' "$1" "$2"
}

while IFS= read -r file; do
    log="$results/node-$(printf '%s' "$file" | tr '/' '_').log"
    code=0
    timeout --kill-after=30 600 node "$file" < /dev/null > "$log" 2>&1 || code=$?
    cat "$log"
    if [ "$code" -eq 0 ]; then
        report PASS "$file"
    else
        report FAIL "$file"
        status=1
    fi
done < <(find tests -maxdepth 2 -path tests/playwright -prune -o -type f \( -name '*.test.js' -o -name '*.test.cjs' -o -name '*.test.mjs' \) -print | LC_ALL=C sort)

while IFS= read -r file; do
    log="$results/mocha-$(printf '%s' "$file" | tr '/' '_').log"
    code=0
    WEKAN_TEST_FILE="$file" NODE_OPTIONS=--import=file:///home/wekan-resolve.mjs timeout --kill-after=30 600 \
        mocha --no-config --no-package --exit --timeout 60000 --reporter /home/wekan-mocha-reporter.cjs "$file" \
        < /dev/null > "$log" 2>&1 || code=$?
    cat "$log"
    if [ "$code" -ne 0 ]; then
        status=1
        if ! grep -q '^WEKAN_TEST|FAIL|' "$log"; then
            report FAIL "$file::<suite did not run>"
        fi
    fi
done < <(find client server -type f -name '*.tests.js' | LC_ALL=C sort)

exit "$status"
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


class WekanImageBase(Image):
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
        toolchain = _BASE_TOOLCHAIN.replace("__MOCHA_VERSION__", _MOCHA_VERSION)
        sections = [
            f"FROM {self.dependency()}",
            self.global_env,
            toolchain,
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(section for section in sections if section) + "\n"


class WekanImageDefault(Image):
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
        return WekanImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__MOCHA_VERSION__", _MOCHA_VERSION)
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
            File(".", "wekan-resolve.mjs", _RESOLVE_MJS),
            File(".", "wekan-mocha-reporter.cjs", _REPORTER_CJS),
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
_RESULT_RE = re.compile(r"^WEKAN_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")


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


@Instance.register("wekan", "wekan")
class WEKAN(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return WekanImageDefault(self.pr, self._config)

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
