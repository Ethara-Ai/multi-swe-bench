import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:16-bullseye"
_NODE_HEAP = "3072"
_JEST_WORKERS = "4"
_BASE_TAG = "base-46012_to_40794"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

_PATCH_JEST_CONFIG_JS = """const fs = require('fs');
const path = require('path');

const repoDir = process.argv[2] || process.cwd();
const target = path.join(repoDir, '.jest.js');
const marker = 'const compileModules = [';
const needed = [
    '@exodus',
    'jsdom',
    '@csstools',
    '@asamuzakjp/dom-selector',
    'parse5',
    'entities',
    'cheerio',
    'domhandler',
    'domutils',
    'dom-serializer',
    'htmlparser2',
    'nwsapi',
    'tr46',
    'whatwg-url',
    'whatwg-mimetype',
    'data-urls',
    'decimal.js',
    'rrweb-cssom',
];

let source = fs.readFileSync(target, 'utf8');
if (source.indexOf(marker) === -1) {
    process.stderr.write('patch_jest_config: compileModules marker not found\\n');
    process.exit(1);
}

const additions = needed.filter((name) => source.indexOf("'" + name + "'") === -1);
if (additions.length > 0) {
    const insert = additions.map((name) => "  '" + name + "',").join('\\n');
    source = source.replace(marker, marker + '\\n' + insert);
    fs.writeFileSync(target, source);
}

process.stdout.write('patch_jest_config: added ' + additions.length + ' compile modules\\n');
"""

_EMIT_RESULTS_JS = """const fs = require('fs');
const path = require('path');

const repoDir = process.argv[2];
const reportFiles = process.argv.slice(3);
const emitted = [];

function toRelative(filePath) {
    if (!filePath) return '';
    let rel = path.relative(repoDir, filePath);
    if (!rel || rel.startsWith('..')) rel = filePath;
    return rel.split(path.sep).join('/');
}

function normalise(text) {
    return String(text === undefined || text === null ? '' : text)
        .replace(/[\\r\\n\\t]+/g, ' ')
        .replace(/\\s+/g, ' ')
        .trim();
}

function toStatus(raw) {
    if (raw === 'passed') return 'PASSED';
    if (raw === 'failed') return 'FAILED';
    return 'SKIPPED';
}

for (const reportFile of reportFiles) {
    if (!reportFile || !fs.existsSync(reportFile)) continue;
    let report;
    try {
        report = JSON.parse(fs.readFileSync(reportFile, 'utf8'));
    } catch (err) {
        continue;
    }
    const suites = Array.isArray(report.testResults) ? report.testResults : [];
    for (const suite of suites) {
        const file = toRelative(suite.name || suite.testFilePath || '');
        if (!file) continue;
        const cases = Array.isArray(suite.assertionResults) ? suite.assertionResults : [];
        if (cases.length === 0) {
            if (suite.status && suite.status !== 'passed') {
                emitted.push('TESTCASE FAILED ' + file);
            }
            continue;
        }
        for (const testCase of cases) {
            const parts = [file];
            const ancestors = Array.isArray(testCase.ancestorTitles) ? testCase.ancestorTitles : [];
            for (const ancestor of ancestors) {
                const cleaned = normalise(ancestor);
                if (cleaned) parts.push(cleaned);
            }
            const title = normalise(testCase.title || testCase.fullName || '');
            if (title) parts.push(title);
            if (parts.length < 2) continue;
            emitted.push('TESTCASE ' + toStatus(testCase.status) + ' ' + parts.join(' > '));
        }
    }
}

process.stdout.write(emitted.map((line) => line + '\\n').join(''));
process.stderr.write('emit_results: ' + emitted.length + ' test cases\\n');
"""

_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

REPO_DIR=/home/__REPO__
JEST_JSON=/home/jest-report.json
JEST_LOG=/home/jest.log

cd "$REPO_DIR"

export CI=true
export TZ=UTC
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=__NODE_HEAP__"

rm -f "$JEST_JSON" "$JEST_LOG"

JEST_PATTERNS=(
__JEST_PATTERNS__
)

echo "===== jest targets ====="
printf '%s\\n' "${JEST_PATTERNS[@]}"

(
    while true; do
        sleep 30
        echo "[heartbeat] jest still running at $(date -u +%H:%M:%S)"
    done
) &
HEARTBEAT_PID=$!

npx jest --config .jest.js --no-cache --ci --silent --maxWorkers=__JEST_WORKERS__ \\
    --json --outputFile="$JEST_JSON" "${JEST_PATTERNS[@]}" > "$JEST_LOG" 2>&1
JEST_EXIT=$?

kill "$HEARTBEAT_PID" >/dev/null 2>&1 || true
wait "$HEARTBEAT_PID" 2>/dev/null || true

echo "===== jest exit code: $JEST_EXIT ====="
tail -c 200000 "$JEST_LOG" 2>/dev/null || true

echo "===== test results ====="
node /home/emit_results.js "$REPO_DIR" "$JEST_JSON"
exit 0
"""

_PREPARE_SH = """#!/bin/bash
set -e

export CI=true
export TZ=UTC
export LC_ALL=C.UTF-8
export NPM_CONFIG_AUDIT=false
export NPM_CONFIG_FUND=false
export NODE_OPTIONS="--max-old-space-size=__NODE_HEAP__"

# Registry fetches inside the build VM drop sockets part-way through a long
# install (ERR_SOCKET_TIMEOUT / FETCH_ERROR), which fails the whole image.
# Raise npm's own per-request retry budget, then wrap the install in a
# whole-command retry for the failures npm cannot recover from itself.
export NPM_CONFIG_FETCH_RETRIES=5
export NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000
export NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000
export NPM_CONFIG_FETCH_TIMEOUT=600000

npm_install_retry() {
    attempt=1
    while [ "$attempt" -le 5 ]; do
        if npm install --legacy-peer-deps --no-audit --no-fund; then
            return 0
        fi
        echo "npm install failed (attempt ${attempt}/5), retrying in $((attempt * 30))s..."
        sleep "$((attempt * 30))"
        attempt=$((attempt + 1))
    done
    echo "npm install failed after 5 attempts" >&2
    return 1
}

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __BASE_SHA__^{commit} 2>/dev/null || git fetch --no-tags origin __BASE_SHA__
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

npm_install_retry
npm rebuild esbuild || true

npm run version
node /home/patch_jest_config.js /home/__REPO__

node --check /home/emit_results.js
node -e "require('./package.json')"
node -e "require('./.jest.js')"
node -e "
const assert = require('assert');
require.resolve('jest');
require.resolve('jest-environment-jsdom');
require.resolve('jsdom');
require.resolve('jest-canvas-mock');
require.resolve('identity-obj-proxy');
require.resolve('@ant-design/tools/lib/jest/codePreprocessor');
require.resolve('@ant-design/tools/lib/jest/demoPreprocessor');
require.resolve('@ant-design/tools/lib/jest/imagePreprocessor');
assert.ok(require('fs').existsSync('./components/version/version.ts'), 'components/version/version.ts missing');
"
npx jest --config .jest.js --listTests > /home/jest-tests.txt
test -s /home/jest-tests.txt
echo "prepare: $(wc -l < /home/jest-tests.txt) jest suites discovered"
echo DEPS_OK
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__

bash /home/run_tests.sh
"""

_APPLY_PATCH = """
# Apply a patch as robustly as the raw dataset allows.  A few entries carry
# patches generated against a slightly different commit than the recorded
# base_sha (ant-design PR 45245's package.json hunk trails stale context:
# it expects rc-textarea ~1.4.0 where the base has ~1.5.1), so a strict
# context match rejects an otherwise perfectly good patch.  Escalate only on
# failure: exact match first, 3-way next, fuzzy context last.  Every fallback
# still has to locate the changed lines, so a genuinely wrong patch fails.
apply_patch() {
    f="$1"
    git apply --whitespace=nowarn "$f" 2>/dev/null && return 0
    git apply --whitespace=nowarn --3way "$f" 2>/dev/null && return 0
    echo "strict apply failed for $f, retrying with fuzzy context..."
    patch -p1 --fuzz=3 --no-backup-if-mismatch < "$f"
}
"""


_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
__APPLY_PATCH__
cd /home/__REPO__
apply_patch /home/test.patch

bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
__APPLY_PATCH__
cd /home/__REPO__
apply_patch /home/test.patch
apply_patch /home/fix.patch

# Deps were installed from the BASE commit's package.json in prepare.sh. When the
# fix patch bumps a dependency (e.g. ant-design PR 41584 moves rc-picker
# ~3.3.4 -> ~3.5.0), the patched source calls an API that the installed version
# does not have yet.  The new props are then silently ignored, tests that passed
# at baseline start failing, and the instance is rejected as "fix patch broke a
# passing test" when the fix itself is fine.  Re-install whenever the fix patch
# actually touched package.json; it is a no-op for every other PR.
if ! git diff --quiet HEAD -- package.json; then
    echo "fix patch changed package.json, reinstalling dependencies..."
    export NPM_CONFIG_FETCH_RETRIES=5
    export NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000
    export NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000
    export NPM_CONFIG_FETCH_TIMEOUT=600000
    attempt=1
    until npm install --legacy-peer-deps --no-audit --no-fund; do
        if [ "$attempt" -ge 5 ]; then
            echo "npm install failed after 5 attempts" >&2
            exit 1
        fi
        echo "npm install failed (attempt ${attempt}/5), retrying in $((attempt * 30))s..."
        sleep "$((attempt * 30))"
        attempt=$((attempt + 1))
    done
    npm rebuild esbuild || true
fi

bash /home/run_tests.sh
"""

_PRUNE_BLOCK = """RUN set -eux; \\
    cd /home/{repo}; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --quiet; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")
_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_DIFF_FILE_RE = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$', re.MULTILINE)
_TEST_FILE_RE = re.compile(r"\.test\.(?:t|j)sx?$")
_SNAP_SUFFIX = ".snap"


def _test_targets(pr: PullRequest) -> list[str]:
    targets: set[str] = set()
    text = (pr.test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    for match in _DIFF_FILE_RE.finditer(text):
        path = match.group(2)
        if path.endswith(_SNAP_SUFFIX):
            path = path[: -len(_SNAP_SUFFIX)].replace("/__snapshots__/", "/")
        if _TEST_FILE_RE.search(path):
            targets.add(path)
    return sorted(targets)


_REGEX_META = ".^$*+?()[]{}|\\"


def _escape_regex(path: str) -> str:
    return "".join("\\" + c if c in _REGEX_META else c for c in path)


def _jest_patterns(pr: PullRequest) -> str:
    return "\n".join(f"    '{_escape_regex(t)}$'" for t in _test_targets(pr))


def _render(template: str, repo: str, base_sha: str = "", patterns: str = "") -> str:
    return (
        template.replace("__REPO__", repo)
        .replace("__BASE_SHA__", base_sha)
        .replace("__NODE_HEAP__", _NODE_HEAP)
        .replace("__JEST_WORKERS__", _JEST_WORKERS)
        .replace("__JEST_PATTERNS__", patterns)
        .replace("__APPLY_PATCH__", _APPLY_PATCH)
    )


def _disjoint(passed: set[str], failed: set[str], skipped: set[str]) -> TestResult:
    passed = passed - failed
    skipped = skipped - failed - passed
    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


class AntDesignImageBase_ANT_DESIGN_46012_TO_40794(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN set -eux; \\
    command -v git; \\
    command -v python3; \\
    command -v make; \\
    command -v gcc; \\
    test -f /etc/ssl/certs/ca-certificates.crt

ENV LC_ALL=C.UTF-8 \\
    NODE_OPTIONS="--max-old-space-size={_NODE_HEAP}" \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_FUND=false

RUN git config --global --add safe.directory '*'

RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class AntDesignImageDefault_ANT_DESIGN_46012_TO_40794(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return AntDesignImageBase_ANT_DESIGN_46012_TO_40794(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        repo = self.pr.repo
        base_sha = self.pr.base.sha
        return [
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "patch_jest_config.js", _PATCH_JEST_CONFIG_JS),
            File(".", "emit_results.js", _EMIT_RESULTS_JS),
            File(".", "prepare.sh", _render(_PREPARE_SH, repo, base_sha)),
        ]

    def graded_files(self) -> list[File]:
        repo = self.pr.repo
        patterns = _jest_patterns(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", _render(_RUN_SH, repo)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, repo)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, repo)),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, repo, patterns=patterns)),
        ]

    def files(self) -> list[File]:
        return self.prepare_files() + self.graded_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "AntDesignImageDefault_ANT_DESIGN_46012_TO_40794 dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        prepare_copy = ""
        for file in self.prepare_files():
            prepare_copy += f"COPY {file.name} /home/\n"

        graded_copy = ""
        for file in self.graded_files():
            graded_copy += f"COPY {file.name} /home/\n"

        prune_commands = _PRUNE_BLOCK.format(
            repo=self.pr.repo, sha=self.pr.base.sha
        )

        sections = [f"FROM {name}:{tag}"]
        for part in (
            self.global_env,
            prepare_copy,
            "RUN bash /home/prepare.sh",
            graded_copy,
            prune_commands,
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("ant-design", "ant_design_46012_to_40794")
class ANT_DESIGN_46012_TO_40794(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Union[str, Image]:
        return AntDesignImageDefault_ANT_DESIGN_46012_TO_40794(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

        for line in clean_log.split("\n"):
            match = _TESTCASE_RE.match(line)
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        return _disjoint(passed_tests, failed_tests, skipped_tests)
