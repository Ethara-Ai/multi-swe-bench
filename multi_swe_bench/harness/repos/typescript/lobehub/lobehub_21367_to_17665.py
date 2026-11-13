import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24-bookworm"
_INTERVAL_NAME = "lobehub_21367_to_17665"
ERA_MIN_PR = int(_INTERVAL_NAME.rsplit("_", 1)[1])
_REGISTRY = "https://registry.npmjs.org/"
_NODE_HEAP = "8192"
_SUITE_TIMEOUT = "2400"
_GATE_TIMEOUT = "900"
_DETECT_TIMEOUT = "300"

_INFRA_BLOCK = """ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=Atlantic/Reykjavik \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt
"""

_LANG_ENV = """ENV CI=true \\
    NODE_ENV=test \\
    NODE_OPTIONS=--max-old-space-size=__NODE_HEAP__ \\
    NODE_COMPILE_CACHE=/tmp/node-compile-cache \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export HUSKY=0
export CYPRESS_INSTALL_BINARY=0
export PUPPETEER_SKIP_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export npm_config_registry=__REGISTRY__

retry() {
  n=0
  until [ "$n" -ge 5 ]; do
    "$@" && return 0
    n=$((n + 1))
    echo "prepare: attempt $n failed, retrying: $*"
    sleep 3
  done
  return 1
}

cd /home/__REPO__
git config --global --add safe.directory '*'
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "${BASE_COMMIT}^{commit}" 2>/dev/null || git fetch --no-tags origin "+${BASE_COMMIT}:refs/heads/msb-base-commit"
git checkout --detach "${BASE_COMMIT}"
bash /home/check_git_changes.sh

PNPM_VERSION=$(node -p "((require('./package.json').packageManager || 'pnpm@12').split('@')[1] || '12').split('+')[0]")
retry npm install -g "pnpm@${PNPM_VERSION}" --registry=__REGISTRY__
install_deps() {
  pnpm install --no-frozen-lockfile --registry=__REGISTRY__ 2>&1 | tee -a /tmp/mswb_install.log
}
if ! retry install_deps; then
  grep -q ERR_PNPM_PATCH_FAILED /tmp/mswb_install.log || exit 1
  node /home/mswb_ws_add.js drop-patches
  retry install_deps
fi
node /home/mswb_ws_add.js add-apps | tee /tmp/mswb_ws.txt
pnpm install --no-frozen-lockfile --registry=__REGISTRY__ || true
for d in $(sed -n "s/^MSWB_WS_ADDED //p" /tmp/mswb_ws.txt); do
  test -d "$d/node_modules"
  echo "MSWB_WS_OK $d $(ls "$d/node_modules" | wc -l)"
done

test -f vitest.config.mts
node -e "require.resolve('vitest/package.json'); require.resolve('happy-dom/package.json')"
npx vitest --version
find src -name '*.test.ts' 2>/dev/null | sort > /tmp/mswb_gate_specs.txt
GATE_SPEC=$(sed -n '1p' /tmp/mswb_gate_specs.txt)
test -n "$GATE_SPEC"
timeout __GATE_TIMEOUT__ npx vitest run "$GATE_SPEC" --reporter=json \\
  --outputFile=/tmp/mswb_gate.json > /tmp/mswb_gate.log 2>&1 \\
  || { tail -n 60 /tmp/mswb_gate.log; exit 1; }
node -e "const d=JSON.parse(require('fs').readFileSync('/tmp/mswb_gate.json','utf8')); if (!(d.numTotalTests > 0)) { console.error('gate: vitest produced no test results'); process.exit(1); } console.log('gate spec tests: ' + d.numTotalTests)"
git checkout -- pnpm-workspace.yaml
echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
MODE="$1"
cd /home/__REPO__
ROOT=$(pwd)
RESULTS=/tmp/mswb_results.txt
: > "$RESULTS"

ROOT_MODE=single
if timeout __DETECT_TIMEOUT__ npx vitest list --project app > /dev/null 2>&1; then
  ROOT_MODE=projects
fi
echo "MSWB_ROOT_MODE $ROOT_MODE"

LIST=/tmp/mswb_suites.txt
: > "$LIST"
if [ "$ROOT_MODE" = "projects" ]; then
  echo "root|app|." >> "$LIST"
  echo "root|server|." >> "$LIST"
else
  echo "root|rootconfig|." >> "$LIST"
fi

CFGS=/tmp/mswb_cfgs.txt
{
  find ./packages ./apps -name 'vitest.config.ts' 2>/dev/null
  find ./packages ./apps -name 'vitest.config.mts' 2>/dev/null
  find ./packages ./apps -name 'vitest.config.js' 2>/dev/null
  find ./packages ./apps -name 'vitest.config.mjs' 2>/dev/null
} | grep -vF '/node_modules/' | grep -vF '/e2e/' | sort -u > "$CFGS"

while read -r cfg; do
  [ -z "$cfg" ] && continue
  d=$(dirname "$cfg")
  n=$({
        find "$d" -name '*.test.ts' 2>/dev/null
        find "$d" -name '*.test.tsx' 2>/dev/null
        find "$d" -name '*.spec.ts' 2>/dev/null
        find "$d" -name '*.spec.tsx' 2>/dev/null
      } | grep -vF '/node_modules/' | wc -l | tr -d ' ')
  if [ "$n" != "0" ]; then
    echo "pkg|$d|$d" >> "$LIST"
  fi
done < "$CFGS"

COUNT=$(wc -l < "$LIST" | tr -d ' ')
echo "MSWB_EXPECTED_SUITES $COUNT"
if [ "$COUNT" = "0" ]; then
  echo "MSWB_NO_SUITES"
  exit 1
fi

while IFS='|' read -r kind label dir; do
  [ -z "$label" ] && continue
  out=/tmp/mswb_$(echo "$label" | tr '/.' '__').json
  rm -f "$out"
  if [ "$kind" = "root" ]; then
    if [ "$ROOT_MODE" = "projects" ]; then
      timeout __SUITE_TIMEOUT__ npx vitest run --project "$label" --retry=2 \\
        --reporter=json --outputFile="$out" > /tmp/mswb_v.log 2>&1
    else
      timeout __SUITE_TIMEOUT__ npx vitest run --retry=2 \\
        --reporter=json --outputFile="$out" > /tmp/mswb_v.log 2>&1
    fi
  else
    ( cd "$dir" && timeout __SUITE_TIMEOUT__ npx vitest run --retry=2 \\
        --reporter=json --outputFile="$out" > /tmp/mswb_v.log 2>&1 )
  fi
  node /home/mswb_digest.js "$ROOT" "$label" "$out" > /tmp/mswb_one.txt 2>&1
  rc=$?
  cat /tmp/mswb_one.txt
  cat /tmp/mswb_one.txt >> "$RESULTS"
  if [ "$rc" != "0" ]; then
    echo "MSWB_SUITE_LOG_BEGIN $label"
    tail -n 40 /tmp/mswb_v.log
    echo "MSWB_SUITE_LOG_END $label"
  fi
done < "$LIST"

EMITTED=$(grep -c '^MSWB_RESULT ' "$RESULTS" || true)
echo "MSWB_EMITTED_RESULTS $EMITTED"
if [ "$EMITTED" = "0" ]; then
  echo "MSWB_ALL_SUITES_FAILED"
  exit 1
fi
SUITE_FAILURES=$(grep -c '^MSWB_SUITE_FAILED ' "$RESULTS" || true)
UNNAMED=$(grep -c '::SUITE_ERROR' "$RESULTS" || true)
echo "MSWB_SUITE_FAILURES $SUITE_FAILURES"
echo "MSWB_UNNAMED_SUITES $UNNAMED"
if [ "$MODE" = "strict" ] && [ "$SUITE_FAILURES" != "0" ]; then
  echo "MSWB_STRICT_UNPATCHED_SUITE_FAILURES"
  exit 1
fi
exit 0
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
cd /home/__REPO__
git reset --hard
git clean -fdq
bash /home/run_tests.sh strict
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
cd /home/__REPO__
git reset --hard
git clean -fdq
git apply --3way --whitespace=nowarn --exclude='*pnpm-lock.yaml' --exclude='*package-lock.json' /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
cd /home/__REPO__
git reset --hard
git clean -fdq
git apply --3way --whitespace=nowarn --exclude='*pnpm-lock.yaml' --exclude='*package-lock.json' /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_MSWB_WS_ADD_JS = r"""const fs = require('fs');

const file = 'pnpm-workspace.yaml';
const mode = process.argv[2] || 'add-apps';

if (!fs.existsSync(file)) {
  console.log('MSWB_WS_SKIP');
  process.exit(0);
}

const lines = fs.readFileSync(file, 'utf8').split('\n');

function indentOf(line) {
  let n = 0;
  while (n < line.length && line[n] === ' ') n++;
  return n;
}

if (mode === 'drop-patches') {
  let at = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim() === 'patchedDependencies:') {
      at = i;
      break;
    }
  }
  if (at < 0) {
    console.log('MSWB_PATCHES_NONE');
    process.exit(0);
  }
  const base = indentOf(lines[at]);
  let end = at + 1;
  const dropped = [];
  while (end < lines.length) {
    const line = lines[end];
    if (line.trim() === '') {
      end++;
      continue;
    }
    if (indentOf(line) <= base) break;
    dropped.push(line.trim());
    end++;
  }
  lines.splice(at, end - at);
  fs.writeFileSync(file, lines.join('\n'));
  console.log('MSWB_PATCHES_DROPPED ' + dropped.length);
  for (const d of dropped) console.log('  ' + d);
  process.exit(0);
}

if (!fs.existsSync('apps')) {
  console.log('MSWB_WS_SKIP');
  process.exit(0);
}

const present = new Set();
for (const line of lines) {
  const t = line.trim();
  if (t.indexOf('- ') === 0) present.add(t.slice(2).trim());
}

const missing = fs
  .readdirSync('apps', { withFileTypes: true })
  .filter((e) => e.isDirectory())
  .map((e) => 'apps/' + e.name)
  .filter((d) => fs.existsSync(d + '/package.json'))
  .filter((d) => !present.has(d));

if (missing.length === 0) {
  console.log('MSWB_WS_ALREADY_COMPLETE');
  process.exit(0);
}

let at = -1;
for (let i = 0; i < lines.length; i++) {
  if (lines[i].trim() === 'packages:') {
    at = i;
    break;
  }
}

if (at < 0) {
  console.log('MSWB_WS_NO_PACKAGES_KEY');
  process.exit(0);
}

lines.splice(at + 1, 0, ...missing.map((d) => '  - ' + d));
fs.writeFileSync(file, lines.join('\n'));
console.log('MSWB_WS_ADDED ' + missing.join(' '));
"""


_MSWB_DIGEST_JS = r"""const fs = require('fs');
const path = require('path');

const root = process.argv[2];
const label = process.argv[3];
const out = process.argv[4];

function rel(p) {
  if (!p) return label;
  let r = p;
  try {
    r = path.relative(root, p);
  } catch (e) {
    r = p;
  }
  if (!r || r.startsWith('..') || path.isAbsolute(r)) return p;
  return r.split(path.sep).join('/');
}

const seen = new Map();

function uniq(id) {
  const n = seen.get(id) || 0;
  seen.set(id, n + 1);
  return n === 0 ? id : id + ' #' + (n + 1);
}

function emit(status, id) {
  console.log('MSWB_RESULT ' + status + ' ' + uniq(id));
}

function expectedNames(file) {
  let src;
  try {
    src = fs.readFileSync(file, 'utf8');
  } catch (e) {
    return [];
  }
  if (/\b(?:describe|it|test)\.each\b/.test(src)) {
    return [];
  }
  const names = [];
  const stack = [];
  let depth = 0;
  for (const raw of src.split('\n')) {
    const line = raw.replace(/\s+$/, '');
    const m = line.match(
      /^\s*(describe|it|test)(?:\.\w+)*\s*\(\s*(['"`])([\s\S]*?)\2/
    );
    let kind = null;
    let title = null;
    if (m) {
      kind = m[1];
      title = m[3];
      if (m[2] === '`' && title.indexOf('${') !== -1) {
        return [];
      }
    }
    const opens = (line.match(/[({]/g) || []).length;
    const closes = (line.match(/[)}]/g) || []).length;
    if (kind === 'describe') {
      stack.push([title, depth]);
    } else if (kind === 'it' || kind === 'test') {
      names.push(
        stack
          .map((s) => s[0])
          .concat([title])
          .join(' > ')
      );
    }
    depth += opens - closes;
    while (stack.length && depth <= stack[stack.length - 1][1]) {
      stack.pop();
    }
  }
  return names;
}

function wholeSuiteFailed() {
  console.log('MSWB_SUITE_FAILED ' + label);
  emit('FAILED', label + '::SUITE_ERROR');
  process.exit(1);
}

if (!fs.existsSync(out)) {
  wholeSuiteFailed();
}

let report;
try {
  report = JSON.parse(fs.readFileSync(out, 'utf8'));
} catch (e) {
  wholeSuiteFailed();
}

const files = report.testResults || [];
if (files.length === 0) {
  wholeSuiteFailed();
}

let count = 0;
let broken = 0;
for (const file of files) {
  const spec = rel(file.name);
  let n = 0;
  for (const t of file.assertionResults || []) {
    const name = [...(t.ancestorTitles || []), t.title]
      .map((x) => String(x).trim())
      .filter((x) => x.length > 0)
      .join(' > ');
    const status =
      t.status === 'passed'
        ? 'PASSED'
        : t.status === 'failed'
          ? 'FAILED'
          : 'SKIPPED';
    emit(status, spec + '::' + name);
    n += 1;
    count += 1;
  }
  if (n === 0) {
    broken += 1;
    console.log('MSWB_SUITE_FAILED ' + spec);
    const names = expectedNames(file.name);
    if (names.length) {
      for (const nm of names) {
        emit('FAILED', spec + '::' + nm);
      }
    } else {
      emit('FAILED', spec + '::SUITE_ERROR');
    }
  }
}

if (count === 0) {
  process.exit(1);
}
if (broken > 0) {
  process.exit(1);
}
"""

_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")
_UNSTABLE_SPEC_RE = re.compile(
    r"^(?:"
    r"[^:]*\.perf\.test\.[a-z]+"
    r"|src/store/tool/slices/mcpStore/action\.test\.ts"
    r"|apps/server/src/routers/lambda/__tests__/integration/aiAgent/execAgent\.integration\.test\.ts"
    r"|packages/database/src/repositories/dataImporter/__tests__/index\.test\.ts"
    r")::"
)


def _render(template: str, repo: str) -> str:
    return (
        template.replace("__REPO__", repo)
        .replace("__REGISTRY__", _REGISTRY)
        .replace("__NODE_HEAP__", _NODE_HEAP)
        .replace("__SUITE_TIMEOUT__", _SUITE_TIMEOUT)
        .replace("__GATE_TIMEOUT__", _GATE_TIMEOUT)
        .replace("__DETECT_TIMEOUT__", _DETECT_TIMEOUT)
    )


def _parse_mswb_log(test_log: str) -> TestResult:
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()
    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    for line in test_log.splitlines():
        match = _RESULT_RE.match(line.strip())
        if not match:
            continue
        status, name = match.group(1), match.group(2)
        if _UNSTABLE_SPEC_RE.match(name):
            continue
        if status == "PASSED":
            passed_tests.add(name)
        elif status == "FAILED":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)
    passed_tests -= failed_tests
    skipped_tests -= failed_tests | passed_tests
    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class ImageBase(Image):
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
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = _INFRA_BLOCK.format(org=self.pr.org, repo=self.pr.repo)
        lang = _render(_LANG_ENV, self.pr.repo)
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {_NODE_IMAGE}\n\n"
            f"{infra}\n"
            f"{lang}\n"
            "RUN git --version && node --version && npm --version\n\n"
            "WORKDIR /home/\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}\n\n'
            'CMD ["/bin/bash"]\n'
        )


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _render(_PREPARE_SH, repo)),
            File(".", "run.sh", _render(_RUN_SH, repo)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, repo)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, repo)),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, repo)),
            File(".", "mswb_digest.js", _MSWB_DIGEST_JS),
            File(".", "mswb_ws_add.js", _MSWB_WS_ADD_JS),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        hardening = Image._HARDENING_BLOCK.rstrip("\n")
        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            f"{copy_commands}\n"
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\n\n'
            "RUN bash /home/prepare.sh\n\n"
            f"WORKDIR /home/{self.pr.repo}\n\n"
            f"{hardening}\n"
        )


@Instance.register("lobehub", _INTERVAL_NAME)
class LOBEHUB_21367_TO_17665(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        return _parse_mswb_log(test_log)
