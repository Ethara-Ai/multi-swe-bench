from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "apollo_client_8416_to_6221"
_BASE_IMAGE = "node:14.21.3-bullseye"
_BASE_TAG = "base-8416_to_6221"
_NODE_MAJOR = "14"
_NPM_VERSION = ""

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_RESULT_RE = re.compile(r"@@ACL@@\t(PASS|FAIL|SKIP)\t(.+?)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_JEST_ROOTS = ("src/", "codegen/")
_SOURCE_EXT_RE = re.compile(r"\.[cm]?[jt]sx?$")
_TEST_NAME_RE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
_NODE_TEST_RE = re.compile(r"^eslint-local-rules/[^/]+\.test\.ts$")
_HELPER_RE = re.compile(r"(?:/fixtures/|/(?:testUtils|helpers)\.[cm]?[jt]sx?$)")
_LOCK_ADDED_RE = re.compile(r'^\+\s*"(?:version|resolved)"', re.M)
_LOCK_DIFF_RE = re.compile(r"^diff --git a/package-lock\.json .*?(?=^diff --git |\Z)", re.M | re.S)
_BINARY_NO_DATA_RE = re.compile(r"^Binary files (?:a/\S+|/dev/null) and b/(\S+) differ$", re.M)

_JEST_REPORTER_JS = r"""'use strict';
const path = require('path');
const ROOT = process.env.MSWE_ROOT || process.cwd();
const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
class MsweJestReporter {
  emit(status, parts) {
    process.stdout.write('\n@@ACL@@\t' + status + '\t' + parts.map(clean).join(' > ') + '\n');
  }
  onTestResult(test, result) {
    const rel = path.relative(ROOT, result.testFilePath).split(path.sep).join('/');
    const cfg = (test && test.context && test.context.config) || {};
    let project = cfg.displayName;
    if (project && typeof project === 'object') {
      project = project.name;
    }
    const head = project ? [rel, '[' + project + ']'] : [rel];
    const items = result.testResults || [];
    if (result.testExecError || (result.failureMessage && items.length === 0)) {
      this.emit('FAIL', head.concat(['(suite)']));
    }
    for (const a of items) {
      const parts = head.concat(a.ancestorTitles || [], [a.title]);
      if (a.status === 'passed') {
        this.emit('PASS', parts);
      } else if (a.status === 'failed') {
        this.emit('FAIL', parts);
      } else {
        this.emit('SKIP', parts);
      }
    }
  }
}
module.exports = MsweJestReporter;
"""

_JEST_GLOBALS_JS = r"""'use strict';
const path = require('path');
const m = /--config[= ]+(\S+)/.exec(process.argv[2] || '');
if (!m || !/\.c?js$/.test(m[1])) {
  process.exit(0);
}
const cfg = require(path.resolve(m[1]));
const globals = cfg && cfg.globals;
const tsJest = globals && globals['ts-jest'];
if (tsJest && tsJest.diagnostics === true) {
  tsJest.diagnostics = { warnOnly: true };
  process.stdout.write(JSON.stringify(globals));
}
"""

_NODE_TEST_REPORTER_MJS = r"""import path from 'node:path';
const ROOT = process.env.MSWE_ROOT || process.cwd();
const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
const line = (status, parts) => '\n@@ACL@@\t' + status + '\t' + parts.map(clean).join(' > ') + '\n';
export default async function* msweNodeTestReporter(source) {
  const stack = [];
  for await (const event of source) {
    const d = event.data || {};
    if (event.type === 'test:start') {
      stack.length = d.nesting;
      stack[d.nesting] = d.name;
      continue;
    }
    if (event.type !== 'test:pass' && event.type !== 'test:fail') {
      continue;
    }
    const file = process.env.MSWE_NODE_TEST_FILE || (d.file ? path.relative(ROOT, d.file).split(path.sep).join('/') : '(unknown)');
    const own = d.file && (d.name === d.file || path.resolve(ROOT, String(d.name)) === d.file) ? '(suite)' : d.name;
    const parts = [file].concat(stack.slice(0, d.nesting), [own]);
    const isSuite = d.details && d.details.type === 'suite';
    if (event.type === 'test:fail') {
      yield line('FAIL', isSuite ? parts.concat(['(suite)']) : parts);
    } else if (!isSuite) {
      yield line(d.skip || d.todo ? 'SKIP' : 'PASS', parts);
    }
  }
}
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_ENV_SH = """export CI=true
export HUSKY=0
export FORCE_COLOR=0
export NO_COLOR=1
export MSWE_ROOT=/home/__REPO__
export NODE_OPTIONS=--max-old-space-size=4096
export npm_config_unsafe_perm=true
export npm_config_audit=false
export npm_config_fund=false
export npm_config_update_notifier=false
export npm_config_progress=false
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
export CYPRESS_INSTALL_BINARY=0
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PATH="/home/__REPO__/node_modules/.bin:$PATH"
"""

_PREPARE_SH = """#!/bin/bash
set -e

cat > /home/mswe_jest_reporter.js <<'MSWE_JEST_REPORTER_EOF'
__JEST_REPORTER__MSWE_JEST_REPORTER_EOF
cat > /home/mswe_node_test_reporter.mjs <<'MSWE_NODE_TEST_REPORTER_EOF'
__NODE_TEST_REPORTER__MSWE_NODE_TEST_REPORTER_EOF
cat > /home/mswe_jest_globals.js <<'MSWE_JEST_GLOBALS_EOF'
__JEST_GLOBALS__MSWE_JEST_GLOBALS_EOF

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin "__BASE_SHA__"
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

__ENV__
echo "MSWE: Node $(node --version), npm $(npm --version)"
[ "$(node -p 'process.versions.node.split(".")[0]')" = "__NODE_MAJOR__" ]

__DEPS__
if grep -q '"iltorb"' package-lock.json; then
    npm ci --ignore-scripts || true
else
    npm ci || true
fi
git checkout -- .
git clean -fd
npm cache clean --force || true

node -e "const fs = require('fs'); for (const p of ['jest', 'ts-jest', 'typescript', 'react']) { if (!fs.existsSync('node_modules/' + p + '/package.json')) { console.error('missing ' + p); process.exit(1); } }"
TEST_CMD="$(node -p "require('./package.json').scripts.test")"
eval "$TEST_CMD --listTests" > /tmp/mswe_list_tests < /dev/null
grep -q . /tmp/mswe_list_tests
rm -f /tmp/mswe_list_tests
echo DEPS_OK
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
echo "MSWE: Node $(node --version)"
"""

_BODY_SH = """JEST_FILES=(__JEST_FILES__)
NODE_TEST_FILES=(__NODE_TEST_FILES__)

RUN_JEST=()
for f in "${JEST_FILES[@]}"; do
    [ -f "$f" ] && RUN_JEST+=("$f")
done
RUN_NODE_TEST=()
for f in "${NODE_TEST_FILES[@]}"; do
    [ -f "$f" ] && RUN_NODE_TEST+=("$f")
done

if [ "${#RUN_JEST[@]}" -eq 0 ] && [ "${#RUN_NODE_TEST[@]}" -eq 0 ]; then
    echo "MSWE: no target test files present"
    exit 0
fi

rc=0
if [ "${#RUN_JEST[@]}" -gt 0 ]; then
    TEST_CMD="$(node -p "require('./package.json').scripts.test")"
    JEST_GLOBALS=()
    MSWE_GLOBALS="$(node /home/mswe_jest_globals.js "$TEST_CMD")"
    if [ -n "$MSWE_GLOBALS" ]; then
        JEST_GLOBALS=(--globals "$MSWE_GLOBALS")
        echo "MSWE: ts-jest diagnostics set to warnOnly"
    fi
    echo "MSWE: $TEST_CMD --runTestsByPath ${RUN_JEST[*]}"
    eval "timeout --kill-after=60 5400 $TEST_CMD --ci --coverage=false --watchAll=false --maxWorkers=2 --passWithNoTests --reporters=default --reporters=/home/mswe_jest_reporter.js \\"\\${JEST_GLOBALS[@]}\\" --runTestsByPath \\"\\${RUN_JEST[@]}\\"" < /dev/null || rc=1
fi
for f in "${RUN_NODE_TEST[@]}"; do
    MSWE_NODE_TEST_FILE="$f" timeout --kill-after=60 1800 node --no-warnings --test --test-reporter=spec --test-reporter-destination=stderr --test-reporter=/home/mswe_node_test_reporter.mjs --test-reporter-destination=stdout "$f" < /dev/null || rc=1
done
exit "$rc"
"""


def _patch_files(patch: str) -> list[str]:
    return [header.group(2) for header in _DIFF_HEADER_RE.finditer(patch or "")]


def _test_targets(pr: PullRequest) -> tuple[list[str], list[str]]:
    jest_files: set[str] = set()
    node_files: set[str] = set()
    for path in _patch_files(pr.test_patch):
        if _NODE_TEST_RE.match(path):
            node_files.add(path)
            continue
        if not path.startswith(_JEST_ROOTS):
            continue
        if "/__snapshots__/" in path and path.endswith(".snap"):
            jest_files.add(path.replace("/__snapshots__/", "/")[: -len(".snap")])
            continue
        if path.endswith(".d.ts") or not _SOURCE_EXT_RE.search(path) or _HELPER_RE.search(path):
            continue
        if "/__tests__/" in path or _TEST_NAME_RE.search(path):
            jest_files.add(path)
    return sorted(jest_files), sorted(node_files)


def _apply(pr: PullRequest) -> str:
    paths = sorted(
        set(_BINARY_NO_DATA_RE.findall(pr.test_patch or ""))
        | set(_BINARY_NO_DATA_RE.findall(pr.fix_patch or ""))
    )
    excludes = "".join(f" --exclude={shlex.quote(path)}" for path in paths)
    return f"git apply --whitespace=nowarn{excludes}"


def _apply_with_fallback(pr: PullRequest, patches: str) -> str:
    apply = _apply(pr)
    steps = "\n".join(
        f"    {apply} {p} || {{ git add -A && {apply} --3way {p}; }} || {{ echo \"MSWE: cannot apply {p}\" >&2; exit 2; }}"
        for p in patches.split()
    )
    return f"if ! {apply} {patches}; then\n    git checkout -- .\n    git clean -fdq\n{steps}\nfi"


def _is_dep_file(path: str) -> bool:
    return (
        path == "package-lock.json"
        or path == ".npmrc"
        or path.startswith("patches/")
        or (path.endswith("package.json") and not path.startswith(("integration-tests/", "scripts/memory/")))
    )


def _deps(pr: PullRequest) -> str:
    patches = [
        ("/home/test.patch", sorted({p for p in _patch_files(pr.test_patch) if _is_dep_file(p)})),
        ("/home/fix.patch", sorted({p for p in _patch_files(pr.fix_patch) if _is_dep_file(p)})),
    ]
    lock_adds = any(
        _LOCK_ADDED_RE.search(diff.group(0))
        for patch in (pr.test_patch or "", pr.fix_patch or "")
        for diff in _LOCK_DIFF_RE.finditer(patch)
    )
    if not lock_adds:
        return ""
    lines = []
    for path, files in patches:
        if files:
            includes = " ".join(f"--include={shlex.quote(f)}" for f in files)
            lines.append(f"git apply --whitespace=nowarn {includes} {path}")
    return "\n".join(lines)


def _bash_array(items: list[str]) -> str:
    return " ".join(shlex.quote(item) for item in items)


class ApolloClient8416To6221ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"
        npm_pin = f"RUN npm install -g npm@{_NPM_VERSION}\n\n" if _NPM_VERSION else ""

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} {_INTERVAL} instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{DockerfileEnhancer._MITM_MOUNT}

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

{self.global_env}

RUN if ! command -v git > /dev/null || ! command -v make > /dev/null || ! command -v g++ > /dev/null || ! command -v python3 > /dev/null; then \\
        apt-get update && apt-get install -y --no-install-recommends ca-certificates git python3 make g++ \\
        && rm -rf /var/lib/apt/lists/*; \\
    fi

{npm_pin}RUN [ "$(node -p 'process.versions.node.split(".")[0]')" = {_NODE_MAJOR} ] && npm --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class ApolloClient8416To6221ImageDefault(Image):
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
        return ApolloClient8416To6221ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        jest_files, node_files = _test_targets(self.pr)
        return (
            template.replace("__JEST_REPORTER__", _JEST_REPORTER_JS)
            .replace("__NODE_TEST_REPORTER__", _NODE_TEST_REPORTER_MJS)
            .replace("__JEST_GLOBALS__", _JEST_GLOBALS_JS)
            .replace("__NODE_MAJOR__", _NODE_MAJOR)
            .replace("__ENV__", _ENV_SH)
            .replace("__DEPS__", _deps(self.pr))
            .replace("__JEST_FILES__", _bash_array(jest_files))
            .replace("__NODE_TEST_FILES__", _bash_array(node_files))
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        body = self._script(_BODY_SH)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{body}"),
            File(".", "test-run.sh", f"{header}\n{_apply_with_fallback(self.pr, '/home/test.patch')}\n\n{body}"),
            File(".", "fix-run.sh", f"{header}\n{_apply_with_fallback(self.pr, '/home/test.patch /home/fix.patch')}\n\n{body}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {image.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}

    for line in _ANSI_RE.sub("", test_log).replace("\r", "\n").split("\n"):
        match = _RESULT_RE.search(line)
        if match:
            buckets[match.group(1)].add(match.group(2))

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


@Instance.register("apollographql", _INTERVAL)
class APOLLO_CLIENT_8416_TO_6221(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ApolloClient8416To6221ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)
