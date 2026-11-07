import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "openwhispr_2815_to_1982"
_BASE_TAG = "base-1982_to_2815"
_NODE_IMAGE = "node:24.22.1-bookworm"
_APT_PACKAGES = [
    "git",
    "ca-certificates",
    "build-essential",
    "python3",
]

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
_DELETED_FILE_RE = re.compile(r"^deleted file mode ", re.MULTILINE)
_TEST_FILE_RE = re.compile(r"^test/.+\.test\.js$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(r"^MSB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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


_REPORTER_MJS = r"""const ROOT = '/home/__REPO__/';

function relative(value) {
  let path = String(value || '').replace(/\\/g, '/');
  if (path.startsWith('file://')) path = decodeURIComponent(path.slice('file://'.length));
  return path.startsWith(ROOT) ? path.slice(ROOT.length) : path;
}

function clean(value) {
  return String(value === undefined || value === null ? '' : value).replace(/\s+/g, ' ').trim();
}

export default async function* msbNodeTestReporter(source) {
  const levelsByFile = new Map();
  const seen = new Map();
  const counts = { PASSED: 0, FAILED: 0, SKIPPED: 0 };

  function unique(name) {
    const count = (seen.get(name) || 0) + 1;
    seen.set(name, count);
    return count > 1 ? name + ' #' + count : name;
  }

  function* flush(node, prefix) {
    const own = node.isFile ? prefix : prefix.concat([node.name]);
    const name = unique((node.isFile ? prefix.concat(['<file>']) : own).join(' > '));
    counts[node.status] += 1;
    yield 'MSB_RESULT ' + node.status + ' ' + name + '\n';
    for (const child of node.children) {
      yield* flush(child, own);
    }
  }

  for await (const event of source) {
    if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
    const data = event.data || {};
    const rawName = String(data.name === undefined || data.name === null ? '' : data.name);
    const file = relative(data.file || rawName) || '<unknown file>';
    const level = Number(data.nesting) || 0;
    const isFile = relative(rawName) === file;
    let status = event.type === 'test:fail' ? 'FAILED' : 'PASSED';
    if (data.skip !== undefined || data.todo !== undefined) status = 'SKIPPED';
    if (!levelsByFile.has(file)) levelsByFile.set(file, []);
    const levels = levelsByFile.get(file);
    const children = levels[level + 1] || [];
    levels.length = level + 1;
    const node = { name: clean(rawName), isFile, status, children };
    if (level === 0) {
      levels.length = 0;
      yield* flush(node, [file]);
    } else {
      if (!levels[level]) levels[level] = [];
      levels[level].push(node);
    }
  }

  yield 'MSB_SUMMARY passed=' + counts.PASSED + ' failed=' + counts.FAILED + ' skipped=' + counts.SKIPPED + '\n';
}
"""


_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __SHA__
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

export CI=true
export HUSKY=0
export NODE_OPTIONS=--max-old-space-size=4096
export npm_config_fund=false
export npm_config_audit=false
export npm_config_update_notifier=false

node --version
npm --version

installed=0
for attempt in 1 2 3; do
    npm ci --ignore-scripts || true
    if [ -f node_modules/.package-lock.json ] && [ -x node_modules/.bin/tsx ]; then
        installed=1
        break
    fi
    echo "prepare: npm ci attempt ${attempt} did not complete; retrying in 30s" >&2
    sleep 30
done
test "$installed" -eq 1

npm rebuild better-sqlite3

rm -rf /tmp/msb-gate
mkdir -p /tmp/msb-gate
cat > /tmp/msb-gate/gate.test.js <<'__MSB_GATE__'
const test = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');

const repoRequire = createRequire('/home/__REPO__/package.json');

test('gate', () => {
  const React = repoRequire('react');
  const server = repoRequire('react-dom/server');
  assert.equal(server.renderToString(React.createElement('b', null, 'ok')), '<b>ok</b>');
  const Database = repoRequire('better-sqlite3');
  const db = new Database(':memory:');
  assert.equal(db.prepare('select 41 + 1 as n').get().n, 42);
  db.close();
  assert.equal(typeof repoRequire('electron'), 'string');
});
__MSB_GATE__
ELECTRON_OVERRIDE_DIST_PATH=/tmp REQUIRE_DB_TESTS=1 node --import tsx --test \
    --test-reporter=spec --test-reporter-destination=stdout \
    --test-reporter=/home/msb_node_test_reporter.mjs --test-reporter-destination=/tmp/msb-gate/report.txt \
    /tmp/msb-gate/gate.test.js
cat /tmp/msb-gate/report.txt
grep -q '^MSB_RESULT PASSED .* > gate$' /tmp/msb-gate/report.txt
grep -q '^MSB_SUMMARY passed=1 failed=0 skipped=0$' /tmp/msb-gate/report.txt
rm -rf /tmp/msb-gate
echo DEPS_OK
"""


_RUN_ENV = r"""export CI=true
export HUSKY=0
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export ELECTRON_OVERRIDE_DIST_PATH=/tmp
export REQUIRE_DB_TESTS=1"""


_TEST_BLOCK = r"""results=/tmp/msb-results
rm -rf "$results"
mkdir -p "$results"

declared=(__TEST_FILES__)
if [ "${#declared[@]}" -eq 0 ]; then
    echo "=== MSB: test.patch adds or changes no test/**/*.test.js file ==="
    exit 1
fi

files=()
for f in "${declared[@]}"; do
    if [ -f "$f" ]; then
        files+=("$f")
    fi
done

if [ "${#files[@]}" -eq 0 ]; then
    echo "=== MSB: none of the test.patch test files exists at this stage ==="
    exit 0
fi

rc=0
timeout --kill-after=60 3600 node --import tsx --test --test-timeout=120000 --test-force-exit \
    --test-reporter=spec --test-reporter-destination=stdout \
    --test-reporter=/home/msb_node_test_reporter.mjs --test-reporter-destination="$results/report.txt" \
    "${files[@]}" || rc=$?

if ! grep -q '^MSB_SUMMARY ' "$results/report.txt" 2>/dev/null; then
    for f in "${files[@]}"; do
        echo "MSB_RESULT FAILED $f > <no node:test report>"
    done
    echo "=== MSB: node --test exited with status $rc without a report ==="
    exit "$(( rc == 0 ? 1 : rc ))"
fi

cat "$results/report.txt"
exit "$rc"
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__RUN_ENV__

cd /home/__REPO__

__TEST_BLOCK__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__RUN_ENV__

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch

__TEST_BLOCK__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__RUN_ENV__

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_BLOCK__"""


def _test_files(pr: PullRequest) -> list[str]:
    files = set()
    for chunk in re.split(r"(?m)^(?=diff --git )", pr.test_patch or ""):
        header = _DIFF_HEADER_RE.match(chunk)
        if not header or _DELETED_FILE_RE.search(chunk):
            continue
        if _TEST_FILE_RE.match(header.group(2)):
            files.add(header.group(2))
    return sorted(files)


class Openwhispr2815To1982ImageBase(Image):
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
        packages = " \\\n        ".join(_APT_PACKAGES)
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

RUN apt-get update && apt-get install -y --no-install-recommends \\
        {packages} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Openwhispr2815To1982ImageDefault(Image):
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
        return Openwhispr2815To1982ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        test_files = " ".join(f"'{path}'" for path in _test_files(self.pr))
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__RUN_ENV__", _RUN_ENV)
            .replace("__TEST_FILES__", test_files)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
            File(".", "msb_node_test_reporter.mjs", self._render(_REPORTER_MJS)),
        ]

    def dockerfile(self) -> str:
        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha).strip("\n")
        return f"""FROM {self.dependency().image_full_name()}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}
"""


@Instance.register("OpenWhispr", "openwhispr")
@Instance.register("OpenWhispr", _INTERVAL)
class OPENWHISPR_2815_TO_1982(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Openwhispr2815To1982ImageDefault(self.pr, self._config)

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

        clean_log = _ANSI_RE.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n")
        for line in clean_log.split("\n"):
            match = _RESULT_RE.match(line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
