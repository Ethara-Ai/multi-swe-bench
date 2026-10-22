import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_dispatcher import (
    AntDesignDispatcher,
)

_NODE_IMAGE = "node:16-bullseye"
_NODE_HEAP = "4096"

_ERA_SLUG = (
    re.split(r"[\\/]", __file__)[-1].removesuffix(".py").removeprefix("ant_design_")
)
_BASE_TAG = f"base-{_ERA_SLUG}"
_ERA_HIGH, _ERA_LOW = (int(n) for n in _ERA_SLUG.split("_to_"))

_RESULTS_MARKER = "===== test results ====="

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PICK_TOOLS_JS = """const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const repoDir = process.argv[2] || process.cwd();
const nm = path.join(repoDir, 'node_modules');

function readPkg(mod) {
    try {
        return JSON.parse(fs.readFileSync(path.join(nm, mod, 'package.json'), 'utf8'));
    } catch (err) {
        return null;
    }
}

function majorOf(range) {
    if (!range) return null;
    const digits = String(range).replace(/[^0-9.]/g, '').split('.')[0];
    return digits === '' ? null : digits;
}

const jestPkg = readPkg('jest');
if (!jestPkg) {
    process.stderr.write('pick_tools: jest not installed; nothing to reconcile\\n');
    process.exit(0);
}
const jestMajor = majorOf(jestPkg.version);

const babelJestPkg = readPkg('babel-jest');
const babelJestMajor = babelJestPkg ? majorOf(babelJestPkg.version) : null;
const toolsPkg = readPkg('@ant-design/tools');
const installedTools = toolsPkg ? toolsPkg.version : 'absent';

if (babelJestMajor === jestMajor) {
    process.stderr.write(
        'pick_tools: jest=' + jestMajor + ' babel-jest=' + babelJestMajor +
        ' (@ant-design/tools@' + installedTools + ') already aligned\\n'
    );
    process.exit(0);
}

let range = null;
try {
    const rootPkg = JSON.parse(fs.readFileSync(path.join(repoDir, 'package.json'), 'utf8'));
    range = (rootPkg.devDependencies || {})['@ant-design/tools'];
} catch (err) {
    range = null;
}
if (!range) {
    process.stderr.write('pick_tools: @ant-design/tools not declared; skipping\\n');
    process.exit(0);
}

let raw = '';
for (let attempt = 1; attempt <= 3 && !raw; attempt += 1) {
    try {
        raw = execFileSync(
            'npm',
            ['view', '@ant-design/tools@' + range, 'version', 'dependencies.babel-jest', '--json'],
            { cwd: repoDir, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], maxBuffer: 1 << 26 }
        ).trim();
    } catch (err) {
        raw = '';
    }
}
if (!raw) {
    process.stderr.write('pick_tools: npm view unavailable; keeping installed version\\n');
    process.exit(0);
}

let parsed;
try {
    parsed = JSON.parse(raw);
} catch (err) {
    process.stderr.write('pick_tools: unparseable npm view output; keeping installed version\\n');
    process.exit(0);
}

const entries = Array.isArray(parsed) ? parsed : [parsed];
let pick = '';
for (const entry of entries) {
    const declared =
        entry['dependencies.babel-jest'] ||
        (entry.dependencies && entry.dependencies['babel-jest']);
    if (!declared) continue;
    if (majorOf(declared) === jestMajor) pick = entry.version;
}

process.stderr.write(
    'pick_tools: jest=' + jestMajor + ' babel-jest=' + babelJestMajor +
    ' range=' + range + ' installed=' + installedTools + ' pick=' + (pick || 'none') + '\\n'
);
if (pick) process.stdout.write(pick);
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

_PREPARE_SH = """#!/bin/bash
set -e

__WRITE_ASSETS__

export CI=true
export TZ=UTC
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export NODE_OPTIONS="--max-old-space-size=__NODE_HEAP__"
export NPM_CONFIG_AUDIT=false
export NPM_CONFIG_FUND=false
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

pin_toolchain() {
    TOOLS_PICK="$(node /home/pick_tools.js /home/__REPO__)" || TOOLS_PICK=""

    if [ -d node_modules/enzyme ]; then
        echo "pin_toolchain: enzyme present -> pinning cheerio@1.0.0-rc.10"
        find node_modules -path "*/node_modules/cheerio" -type d -exec rm -rf {} + 2>/dev/null || true
        rm -rf node_modules/cheerio node_modules/.package-lock.json
        if [ -n "$TOOLS_PICK" ]; then
            npm install --no-save --legacy-peer-deps \\
                cheerio@1.0.0-rc.10 "@ant-design/tools@$TOOLS_PICK" || true
        else
            npm install --no-save --legacy-peer-deps cheerio@1.0.0-rc.10 || true
        fi
        find node_modules -name "parse5-parser-stream" -type d -exec rm -rf {} + 2>/dev/null || true
    elif [ -n "$TOOLS_PICK" ]; then
        echo "pin_toolchain: pinning @ant-design/tools@$TOOLS_PICK"
        npm install --no-save --legacy-peer-deps "@ant-design/tools@$TOOLS_PICK" || true
    else
        echo "pin_toolchain: installed toolchain already consistent (no-op)"
    fi
}

pin_toolchain

for attempt in 1 2 3; do
    if npx jest --version >/dev/null 2>&1; then break; fi
    echo "jest CLI unresolvable after pinning; healing the tree (attempt ${attempt}/3)"
    npm install --legacy-peer-deps --no-audit --no-fund || true
    pin_toolchain
done

npm run version

node --check /home/emit_results.js
node -e "
require.resolve('jest');
require.resolve('@ant-design/tools/lib/jest/codePreprocessor');
require.resolve('@ant-design/tools/lib/jest/demoPreprocessor');
require.resolve('@ant-design/tools/lib/jest/imagePreprocessor');
"
npx jest --config .jest.js --listTests > /home/jest-tests.txt
test -s /home/jest-tests.txt
echo "prepare: $(wc -l < /home/jest-tests.txt) jest suites discovered"

SMOKE_JSON=/home/smoke-report.json
SMOKE_FILES="$(head -3 /home/jest-tests.txt)"
SMOKE_EXIT=0
npx jest --config .jest.js --no-cache --ci --runInBand --testTimeout=120000 \\
    --json --outputFile="$SMOKE_JSON" --runTestsByPath $SMOKE_FILES \\
    > /home/smoke.log 2>&1 || SMOKE_EXIT=$?
echo "prepare: smoke jest exit ${SMOKE_EXIT}"
if [ ! -s "$SMOKE_JSON" ]; then
    echo "prepare: jest produced no report; the runner is broken" >&2
    tail -c 20000 /home/smoke.log >&2
    exit 1
fi
SMOKE_TOTAL="$(node -e "
const r = require('$SMOKE_JSON');
process.stdout.write(String((r.numTotalTests || 0)));
")"
if [ "${SMOKE_TOTAL:-0}" -lt 1 ]; then
    echo "prepare: jest executed 0 test cases across $SMOKE_FILES" >&2
    tail -c 20000 /home/smoke.log >&2
    exit 1
fi
echo "prepare: smoke run executed ${SMOKE_TOTAL} test cases"
node /home/emit_results.js /home/__REPO__ "$SMOKE_JSON" > /dev/null
rm -f "$SMOKE_JSON" /home/smoke.log
echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail

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

JEST_TARGETS=(
__JEST_TARGETS__
)

EXISTING=()
for target in "${JEST_TARGETS[@]}"; do
    if [ -f "$target" ]; then
        EXISTING+=("$target")
    fi
done

echo "===== jest scope: ${#EXISTING[@]} of ${#JEST_TARGETS[@]} target files present ====="
printf '%s\\n' "${JEST_TARGETS[@]}"

if [ "${#EXISTING[@]}" -eq 0 ]; then
    echo "__RESULTS_MARKER__"
    echo "no target test file exists at this stage"
    exit 0
fi

rm -f "$JEST_JSON" "$JEST_LOG"

JEST_EXIT=0
npx jest --config .jest.js --no-cache --ci --runInBand --testTimeout=120000 \\
    --json --outputFile="$JEST_JSON" --runTestsByPath "${EXISTING[@]}" \\
    > "$JEST_LOG" 2>&1 || JEST_EXIT=$?

echo "===== jest exit code: ${JEST_EXIT} ====="
if [ -f "$JEST_LOG" ]; then
    tail -c 200000 "$JEST_LOG"
fi

test -s "$JEST_JSON"

echo "__RESULTS_MARKER__"
node /home/emit_results.js "$REPO_DIR" "$JEST_JSON"
"""

def _heredoc(path: str, body: str, delimiter: str) -> str:
    if any(line.strip() == delimiter for line in body.split("\n")):
        raise ValueError(f"heredoc delimiter {delimiter} collides with body of {path}")
    return f"cat > {path} <<'{delimiter}'\n{body.rstrip(chr(10))}\n{delimiter}"


_WRITE_ASSETS_SH = "\n\n".join(
    [
        _heredoc("/home/pick_tools.js", _PICK_TOOLS_JS, "MSWB_PICK_TOOLS_EOF"),
        _heredoc("/home/emit_results.js", _EMIT_RESULTS_JS, "MSWB_EMIT_RESULTS_EOF"),
        _heredoc("/home/run_tests.sh", _RUN_TESTS_SH, "MSWB_RUN_TESTS_EOF"),
        "chmod +x /home/run_tests.sh",
        "node --check /home/pick_tools.js",
        "node --check /home/emit_results.js",
        "bash -n /home/run_tests.sh",
    ]
)

_APPLY_PATCH_FN = """apply_patch() {
    patch_file="$1"
    git apply --whitespace=nowarn "$patch_file" && return 0
    echo "apply_patch: strict apply failed for ${patch_file}, retrying 3-way"
    git apply --3way --whitespace=nowarn "$patch_file"
}"""

_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__

bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

__APPLY_PATCH_FN__

cd /home/__REPO__
apply_patch /home/test.patch

bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

__APPLY_PATCH_FN__

cd /home/__REPO__
apply_patch /home/test.patch
apply_patch /home/fix.patch

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


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _jest_targets_block(pr: PullRequest) -> str:
    return "\n".join(f"    {_shell_quote(target)}" for target in _test_targets(pr))


def _render(
    template: str, repo: str, base_sha: str = "", jest_targets: str = ""
) -> str:
    return (
        template.replace("__WRITE_ASSETS__", _WRITE_ASSETS_SH)
        .replace("__REPO__", repo)
        .replace("__BASE_SHA__", base_sha)
        .replace("__NODE_HEAP__", _NODE_HEAP)
        .replace("__JEST_TARGETS__", jest_targets)
        .replace("__APPLY_PATCH_FN__", _APPLY_PATCH_FN)
        .replace("__RESULTS_MARKER__", _RESULTS_MARKER)
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


class AntDesignImageBase_ANT_DESIGN_41482_TO_28924(Image):
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

        infra = DockerfileEnhancer._infrastructure_block(self, image_name, True)

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{infra}
ENV NODE_OPTIONS="--max-old-space-size={_NODE_HEAP}" \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_FUND=false

RUN set -eux; \\
    command -v git; \\
    command -v python3; \\
    command -v make; \\
    command -v gcc; \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class AntDesignImageDefault_ANT_DESIGN_41482_TO_28924(Image):
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
        return AntDesignImageBase_ANT_DESIGN_41482_TO_28924(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                _render(
                    _PREPARE_SH,
                    repo,
                    self.pr.base.sha,
                    jest_targets=_jest_targets_block(self.pr),
                ),
            ),
        ]

    def graded_files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", _render(_RUN_SH, repo)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, repo)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, repo)),
        ]

    def files(self) -> list[File]:
        return self.prepare_files() + self.graded_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "AntDesignImageDefault_ANT_DESIGN_41482_TO_28924 dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        prepare_copy = "".join(
            f"COPY {file.name} /home/\n" for file in self.prepare_files()
        )
        graded_copy = "".join(
            f"COPY {file.name} /home/\n" for file in self.graded_files()
        )

        prune = _PRUNE_BLOCK.format(repo=self.pr.repo, sha=self.pr.base.sha)

        sections = [f"FROM {name}:{tag}", f'ARG BASE_COMMIT="{self.pr.base.sha}"']
        for part in (
            self.global_env,
            prepare_copy,
            "RUN bash /home/prepare.sh",
            graded_copy,
            prune,
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("ant-design", "ant-design")
@Instance.register("ant-design", "ant_design_41482_to_28924")
class ANT_DESIGN_41482_TO_28924(Instance):

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _ERA_LOW <= pr.number <= _ERA_HIGH:
            return super().__new__(cls)
        return AntDesignDispatcher(pr, config, *args, **kwargs)

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> str | Image:
        return AntDesignImageDefault_ANT_DESIGN_41482_TO_28924(self.pr, self._config)

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

        if _RESULTS_MARKER in clean_log:
            clean_log = clean_log.rsplit(_RESULTS_MARKER, 1)[1]

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