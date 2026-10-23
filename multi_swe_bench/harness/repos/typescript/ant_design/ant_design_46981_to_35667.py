from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "ant_design_46981_to_35667"
_PR_LOW, _PR_HIGH = 35667, 46981
_REPO_DIR = "/home/ant-design"

_NODE_VERSION = "16.20.2"
_BASE_IMAGE = "node:16.20.2-bookworm"
_BASE_TAG = f"base-{_PR_HIGH}_to_{_PR_LOW}"


_DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
_TEST_FILE_RE = re.compile(r"\.test\.(?:js|jsx|ts|tsx)$")


def _test_targets(pr: PullRequest) -> list[str]:
    targets: set[str] = set()
    for old, new in _DIFF_PATH_RE.findall(pr.test_patch or ""):
        for path in (old, new):
            if path.endswith(".snap") and "/__snapshots__/" in path:
                path = path[: -len(".snap")].replace("/__snapshots__/", "/")
            if _TEST_FILE_RE.search(path):
                targets.add(path)
    if not targets:
        raise ValueError(f"ant-design PR {pr.number}: test.patch names no jest test file")
    return sorted(targets)


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


_WRITE_VERSION_JS = r"""const fs = require('fs');
const path = require('path');

const dir = path.join(process.cwd(), 'components', 'version');
let index = '';
['index.tsx', 'index.ts'].forEach(function (name) {
  const p = path.join(dir, name);
  if (!index && fs.existsSync(p)) index = fs.readFileSync(p, 'utf8');
});
if (!/from\s+['"]\.\/version['"]/.test(index)) {
  console.log('write_version: components/version imports package.json; nothing to generate');
  process.exit(0);
}
const version = JSON.parse(fs.readFileSync('package.json', 'utf8')).version;
let target = path.join(dir, 'version.ts');
if (!fs.existsSync(target)) target = path.join(dir, 'version.tsx');
fs.writeFileSync(target, "export default '" + version + "';\n");
console.log('write_version: ' + path.relative(process.cwd(), target) + ' = ' + version);
"""

_EMIT_RESULTS_JS = r"""const fs = require('fs');
const path = require('path');

const repoDir = process.argv[2];
const report = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const seen = {};
const out = [];

function norm(text) {
  return String(text === undefined || text === null ? '' : text).replace(/\s+/g, ' ').trim();
}

function emit(status, name) {
  seen[name] = (seen[name] || 0) + 1;
  if (seen[name] > 1) name = name + ' #' + seen[name];
  out.push('TESTCASE ' + status + ' ' + name);
}

(report.testResults || []).forEach(function (suite) {
  const file = path.relative(repoDir, suite.name || suite.testFilePath || '').split(path.sep).join('/');
  if (!file) return;
  const cases = suite.assertionResults || [];
  if (cases.length === 0) {
    if (suite.status !== 'passed') emit('FAILED', file + ' > <suite failed to run>');
    return;
  }
  cases.forEach(function (c) {
    const parts = [file].concat((c.ancestorTitles || []).map(norm).filter(Boolean));
    parts.push(norm(c.title || c.fullName));
    const status = c.status === 'passed' ? 'PASSED' : c.status === 'failed' ? 'FAILED' : 'SKIPPED';
    emit(status, parts.join(' > '));
  });
});

process.stdout.write(out.length ? out.join('\n') + '\n' : '');
console.error('emit_results: ' + out.length + ' test cases');
"""

_CHANGED_DEPS_JS = r"""const fs = require('fs');
const before = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const after = JSON.parse(fs.readFileSync('package.json', 'utf8'));
function deps(p) { return Object.assign({}, p.dependencies || {}, p.devDependencies || {}); }
const had = deps(before);
const has = deps(after);
Object.keys(has).forEach(function (name) {
  if (had[name] !== has[name]) console.log(name + '@' + has[name]);
});
if (process.argv[3]) {
  fs.writeFileSync(process.argv[3], Object.keys(had).filter(function (name) {
    return !(name in has);
  }).map(function (name) { return name + '\n'; }).join(''));
}
"""

_FIX_STUBS_JS = r"""const fs = require('fs');
const path = require('path');
const execFileSync = require('child_process').execFileSync;

const before = Date.parse(process.argv[2]);
const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
const declared = Object.assign({}, pkg.dependencies || {}, pkg.devDependencies || {});
const semver = require(path.resolve('node_modules/semver'));

function stubVersion(name) {
  const dir = path.join('node_modules', name);
  const manifest = path.join(dir, 'package.json');
  if (!fs.existsSync(manifest)) return null;
  const files = fs.readdirSync(dir).filter(function (f) { return f !== 'node_modules'; });
  if (files.length !== 1) return null;
  return JSON.parse(fs.readFileSync(manifest, 'utf8')).version;
}

const fixes = [];
Object.keys(declared).forEach(function (name) {
  const stub = stubVersion(name);
  if (stub === null) return;
  const meta = JSON.parse(execFileSync('npm', ['view', name, 'time', '--json'], { encoding: 'utf8' }));
  const candidates = Object.keys(meta).filter(function (v) {
    return v !== 'created' && v !== 'modified' && v !== stub && Date.parse(meta[v]) <= before;
  });
  const pick = semver.maxSatisfying(candidates, declared[name], { includePrerelease: true });
  if (!pick) throw new Error('no usable release of ' + name + '@' + declared[name]);
  console.error('fix_stubs: ' + name + '@' + stub + ' is an empty placeholder; using ' + pick);
  fixes.push(name + '@' + pick);
});
process.stdout.write(fixes.length ? fixes.join('\n') + '\n' : '');
"""

_SIDE_INSTALL_SH = """#!/bin/bash
set -euo pipefail

CUTOFF="$1"
BEFORE_FLAG=()
if [ -n "$CUTOFF" ]; then BEFORE_FLAG=(--before="$CUTOFF"); fi
shift
[ "$#" -gt 0 ] || exit 0
test -x node_modules/.bin/jest

SIDE=$(mktemp -d)
echo '{"name":"side-install","version":"0.0.0","private":true}' > "$SIDE/package.json"
ok=0
for attempt in 1 2 3; do
  if (cd "$SIDE" && npm install "${BEFORE_FLAG[@]}" --no-save --no-package-lock --ignore-scripts --legacy-peer-deps --no-audit --no-fund "$@") > "$SIDE.log" 2>&1; then
    ok=1
    break
  fi
  tail -5 "$SIDE.log"
  rm -rf "$SIDE/node_modules"
  if grep -q "ETARGET" "$SIDE.log"; then
    break
  fi
  sleep $((attempt * 15))
done
rm -f "$SIDE.log"
if [ "$ok" -ne 1 ]; then
  echo "side_install: npm install failed for: $*" >&2
  exit 1
fi

for spec in "$@"; do
  name="${spec%@*}"
  [ -n "$name" ] || name="$spec"
  test -f "$SIDE/node_modules/$name/package.json"
  if [ -f "node_modules/$name/package.json" ]; then
    SIDE="$SIDE" node -e '
const fs = require("fs");
const path = require("path");
const semver = require(path.resolve("node_modules/semver"));
const name = process.argv[1];
const oldVersion = JSON.parse(fs.readFileSync(path.join("node_modules", name, "package.json"), "utf8")).version;
const newVersion = JSON.parse(fs.readFileSync(path.join(process.env.SIDE, "node_modules", name, "package.json"), "utf8")).version;
function walk(dir, depth) {
  if (depth > 1) return;
  let entries;
  try { entries = fs.readdirSync(dir); } catch (e) { return; }
  entries.forEach(function (entry) {
    if (entry.charAt(0) === ".") return;
    if (entry.charAt(0) === "@") { walk(path.join(dir, entry), depth); return; }
    const pkgDir = path.join(dir, entry);
    let m;
    try { m = JSON.parse(fs.readFileSync(path.join(pkgDir, "package.json"), "utf8")); } catch (e) { return; }
    const range = (m.dependencies || {})[name];
    if (!range || m.name === name) return;
    if (fs.existsSync(path.join(pkgDir, "node_modules", name, "package.json"))) return;
    if (semver.satisfies(oldVersion, range, { includePrerelease: true }) &&
        !semver.satisfies(newVersion, range, { includePrerelease: true })) {
      const target = path.join(pkgDir, "node_modules", name);
      require("child_process").execFileSync("mkdir", ["-p", path.dirname(target)]);
      require("child_process").execFileSync("cp", ["-a", path.join("node_modules", name), target]);
      console.log("side_install: kept " + name + "@" + oldVersion + " for " + m.name + " (" + range + ")");
    }
  });
}
walk("node_modules", 0);
' "$name"
  fi
  rm -rf "node_modules/$name"
  mkdir -p "$(dirname "node_modules/$name")"
  cp -a "$SIDE/node_modules/$name" "node_modules/$name"
  echo "side_install: $name -> $(node -p "require('./node_modules/$name/package.json').version")"
done

SIDE="$SIDE" node -e '
const fs = require("fs");
const path = require("path");
const semver = require(path.resolve("node_modules/semver"));
const side = path.join(process.env.SIDE, "node_modules");
const roots = process.argv.slice(1).map(function (s) { return s.replace(/^(@?[^@]+)@.*$/, "$1"); });
function manifest(dir) {
  try { return JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8")); } catch (e) { return null; }
}
const seen = {};
const stale = {};
const queue = roots.slice();
while (queue.length) {
  const name = queue.shift();
  if (seen[name]) continue;
  seen[name] = true;
  const m = manifest(path.join("node_modules", name));
  if (!m) continue;
  const deps = m.dependencies || {};
  Object.keys(deps).forEach(function (dep) {
    if (fs.existsSync(path.join("node_modules", name, "node_modules", dep, "package.json"))) return;
    const have = manifest(path.join("node_modules", dep));
    const fresh = manifest(path.join(side, dep));
    if (!fresh) return;
    if (!have) {
      if (semver.satisfies(fresh.version, deps[dep], { includePrerelease: true })) {
        console.log(dep + "\\t" + dep);
        queue.push(dep);
      }
      return;
    }
    if (!semver.satisfies(have.version, deps[dep], { includePrerelease: true }) &&
        semver.satisfies(fresh.version, deps[dep], { includePrerelease: true })) {
      (stale[dep] = stale[dep] || { version: have.version, fresh: fresh.version, parents: [] }).parents.push(name);
    }
  });
}
function holdsOld(dep, oldVersion, freshVersion) {
  let hold = false;
  function scan(dir, depth) {
    if (hold || depth > 3) return;
    let entries;
    try { entries = fs.readdirSync(dir); } catch (e) { return; }
    entries.forEach(function (entry) {
      if (hold || entry.charAt(0) === ".") return;
      const dirs = entry.charAt(0) === "@"
        ? (function () { try { return fs.readdirSync(path.join(dir, entry)).map(function (e) { return path.join(entry, e); }); } catch (e) { return []; } })()
        : [entry];
      dirs.forEach(function (rel) {
        const pkgDir = path.join(dir, rel);
        const m = manifest(pkgDir);
        if (!m) return;
        if (m.name !== dep) {
          const range = Object.assign({}, m.dependencies || {}, m.peerDependencies || {})[dep];
          if (range && !fs.existsSync(path.join(pkgDir, "node_modules", dep, "package.json")) &&
              semver.satisfies(oldVersion, range, { includePrerelease: true }) &&
              !semver.satisfies(freshVersion, range, { includePrerelease: true })) hold = true;
        }
        scan(path.join(pkgDir, "node_modules"), depth + 1);
      });
    });
  }
  scan("node_modules", 0);
  return hold;
}
const top = manifest(".");
const declaredTop = Object.assign({}, top.dependencies || {}, top.devDependencies || {});
Object.keys(stale).forEach(function (dep) {
  const s = stale[dep];
  const topRange = declaredTop[dep];
  const topOk = !topRange || semver.satisfies(s.fresh, topRange, { includePrerelease: true });
  if (topOk && !holdsOld(dep, s.version, s.fresh)) {
    console.log(dep + "\\t" + dep);
  } else {
    s.parents.forEach(function (parent) { console.log(dep + "\\t" + parent + "/node_modules/" + dep); });
  }
});
' "$@" > "$SIDE.upgrade"
while IFS="$(printf '\\t')" read -r dep dest; do
  [ -n "$dep" ] || continue
  rm -rf "node_modules/$dest"
  mkdir -p "$(dirname "node_modules/$dest")"
  cp -a "$SIDE/node_modules/$dep" "node_modules/$dest"
  echo "side_install: transitive $dest -> $(node -p "require('./node_modules/$dest/package.json').version")"
done < "$SIDE.upgrade"
rm -f "$SIDE.upgrade"

for dir in "$SIDE"/node_modules/* "$SIDE"/node_modules/@*/*; do
  [ -f "$dir/package.json" ] || continue
  rel="${dir#"$SIDE"/node_modules/}"
  if [ ! -e "node_modules/$rel" ]; then
    mkdir -p "$(dirname "node_modules/$rel")"
    cp -a "$dir" "node_modules/$rel"
  fi
done
rm -rf "$SIDE"
test -x node_modules/.bin/jest

if [ -z "${SIDE_INSTALL_NESTED:-}" ]; then
  STUB_CUTOFF="${CUTOFF:-$(git log -1 --format=%cI HEAD)}"
  mapfile -t STUBS < <(node "${MSWEB_HELPERS}/fix_stubs.js" "$STUB_CUTOFF")
  if [ "${#STUBS[@]}" -gt 0 ]; then
    SIDE_INSTALL_NESTED=1 bash "${MSWEB_HELPERS}/side_install.sh" "$STUB_CUTOFF" "${STUBS[@]}"
  fi
fi
"""

_APPLY_PATCH_SH = r"""#!/bin/bash
set -uo pipefail

patch="$1"
git update-index -q --refresh

if git apply --whitespace=nowarn --check --exclude='*.png' "$patch" 2> /dev/null || \
   git apply --whitespace=nowarn --3way --check --exclude='*.png' "$patch" 2> /dev/null; then
  git apply --whitespace=nowarn --3way --exclude='*.png' "$patch"
  rc=$?
  if [ -n "$(git diff --name-only --diff-filter=U)" ]; then
    echo "MSWEB_FATAL: $patch left merge conflicts:" >&2
    git diff --name-only --diff-filter=U >&2
    exit 1
  fi
  exit $rc
fi

excludes=()
for f in $(awk '/^diff --git a\// {p=$3; sub("^a/","",p)} /^deleted file mode/ {print p}' "$patch"); do
  if ! git cat-file -e "HEAD:$f" 2> /dev/null; then
    echo "apply_patch: skipping deletion of $f (absent at base)"
    excludes+=("--exclude=$f")
  fi
done
if [ "${#excludes[@]}" -gt 0 ] && \
   git apply --whitespace=nowarn --3way --check --exclude='*.png' "${excludes[@]}" "$patch" 2> /dev/null; then
  git apply --whitespace=nowarn --3way --exclude='*.png' "${excludes[@]}" "$patch"
  exit $?
fi
echo "MSWEB_FATAL: $patch does not apply at the base commit" >&2
git apply --whitespace=nowarn --3way --check --exclude='*.png' "$patch" >&2
exit 1
"""

_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail

export CI=true
test "$(node --version)" = "v__NODE_VERSION__"
cd __REPO_DIR__

TARGETS=(
__TARGETS__
)

FOUND=()
for f in "${TARGETS[@]}"; do
  if [ -f "$f" ]; then FOUND+=("$f"); fi
done
echo "jest targets present: ${#FOUND[@]} of ${#TARGETS[@]}"
if [ "${#FOUND[@]}" -eq 0 ]; then
  echo "no target test file exists at this stage; jest not run"
  exit 0
fi

rm -f /tmp/jest-results.json /tmp/jest.log
set +e
./node_modules/.bin/jest --config .jest.js --ci --no-cache --maxWorkers=4 \\
  --json --outputFile=/tmp/jest-results.json \\
  --runTestsByPath "${FOUND[@]}" > /tmp/jest.log 2>&1
JEST_EXIT=$?
set -e

awk '/^(PASS|FAIL) |^Tests:|^Test Suites:|Cannot find module|Test suite failed to run/ && n < 400 { print; n++ }' /tmp/jest.log
echo "jest exit code: ${JEST_EXIT}"
if [ ! -s /tmp/jest-results.json ]; then
  echo "MSWEB_FATAL: jest produced no JSON report" >&2
  exit 1
fi

echo "===== test results ====="
node "${MSWEB_HELPERS}/emit_results.js" __REPO_DIR__ /tmp/jest-results.json
"""

_PREPARE_SH = """#!/bin/bash
set -euo pipefail

__HELPERS__
test "$(node --version)" = "v__NODE_VERSION__"
cd __REPO_DIR__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__BASE_SHA__"

export CI=true
export HUSKY=0
export HUSKY_SKIP_INSTALL=1
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
export npm_config_audit=false
export npm_config_fund=false
export npm_config_update_notifier=false
export npm_config_fetch_retries=5
export npm_config_fetch_retry_mintimeout=20000
export npm_config_fetch_retry_maxtimeout=120000

BEFORE="$(git log -1 --format=%cI HEAD)"
echo "prepare: resolving dependencies as of ${BEFORE} with node $(node --version), npm $(npm --version)"
cp package.json /tmp/package.pristine.json

ok=0
for attempt in 1 2 3 4; do
  if npm install --before="${BEFORE}" --no-package-lock --ignore-scripts --legacy-peer-deps --no-audit --no-fund; then
    ok=1
    break
  fi
  echo "prepare: npm install attempt ${attempt} failed; retrying"
  rm -rf node_modules
  sleep $((attempt * 15))
done
if [ "${ok}" -ne 1 ]; then
  echo "prepare: npm install failed after 4 attempts" >&2
  exit 1
fi

if [ -d node_modules/esbuild ]; then
  npm rebuild esbuild || echo "prepare: esbuild rebuild skipped"
fi

mapfile -t STUB_FIXES < <(node "${MSWEB_HELPERS}/fix_stubs.js" "${BEFORE}")
bash "${MSWEB_HELPERS}/side_install.sh" "${BEFORE}" "${STUB_FIXES[@]}"

COMPAT_DATA=node_modules/@babel/compat-data/package.json
if [ -f node_modules/@babel/preset-env/lib/polyfills/corejs3/usage-plugin.js ] && \\
   grep -q "@babel/compat-data/corejs3-shipped-proposals" node_modules/@babel/preset-env/lib/polyfills/corejs3/usage-plugin.js && \\
   ! node -e "require.resolve('@babel/compat-data/corejs3-shipped-proposals')" 2> /dev/null; then
  echo "prepare: @babel/compat-data $(node -p "require('./${COMPAT_DATA}').version") lacks a resolvable corejs3-shipped-proposals export"
  bash "${MSWEB_HELPERS}/side_install.sh" "2020-03-21T00:00:00Z" "@babel/compat-data@7.9.0"
fi

node "${MSWEB_HELPERS}/write_version.js"
cp /tmp/package.pristine.json package.json
git diff --quiet HEAD -- package.json

test -x node_modules/.bin/jest
test -z "$(node "${MSWEB_HELPERS}/fix_stubs.js" "${BEFORE}")"
if grep -qs "@babel/compat-data/corejs3-shipped-proposals" node_modules/@babel/preset-env/lib/polyfills/corejs3/usage-plugin.js; then
  node -e "require('@babel/compat-data/corejs3-shipped-proposals')"
fi
node -e "
const path = require('path');
const cfg = require('./.jest.js');
const transforms = Object.keys(cfg.transform || {}).map(function (k) { return cfg.transform[k]; });
transforms.concat(cfg.setupFiles || [], cfg.setupFilesAfterEnv || [], cfg.snapshotSerializers || [])
  .forEach(function (entry) {
    require.resolve(entry.charAt(0) === '.' ? path.resolve(entry) : entry);
  });
console.log('jest config resolves: ' + transforms.length + ' transforms');
"
node -e "
const fs = require('fs');
const idx = ['components/version/index.tsx', 'components/version/index.ts']
  .filter(function (p) { return fs.existsSync(p); }).map(function (p) { return fs.readFileSync(p, 'utf8'); })[0] || '';
if (/from\\s+['\\\"]\\.\\/version['\\\"]/.test(idx) &&
    !fs.existsSync('components/version/version.ts') && !fs.existsSync('components/version/version.tsx')) {
  throw new Error('components/version/version.ts(x) was not generated');
}
"
node --check "${MSWEB_HELPERS}/emit_results.js"
./node_modules/.bin/jest --config .jest.js --no-cache --listTests > /tmp/jest-list.txt
test "$(grep -c . /tmp/jest-list.txt)" -gt 0
echo "prepare: $(grep -c . /tmp/jest-list.txt) jest suites discovered"
echo DEPS_OK
"""

_SYNC_DEPS = """
if ! git diff --quiet HEAD -- package.json; then
  git show HEAD:package.json > /tmp/package.head.json
  node "${MSWEB_HELPERS}/changed_deps.js" /tmp/package.head.json /tmp/removed_deps.txt > /tmp/changed_deps.txt
  mapfile -t CHANGED < /tmp/changed_deps.txt
  while IFS= read -r gone; do
    [ -n "$gone" ] || continue
    echo "package.json dropped ${gone}; removing node_modules/${gone}"
    rm -rf "node_modules/${gone}"
  done < /tmp/removed_deps.txt
  if [ "${#CHANGED[@]}" -gt 0 ]; then
    BASE_DATE="$(git log -1 --format=%cI HEAD)"
    installed=0
    for days in 2 14 30; do
      BEFORE="$(node -e 'console.log(new Date(Date.parse(process.argv[1]) + Number(process.argv[2]) * 864e5).toISOString())' "${BASE_DATE}" "${days}")"
      echo "package.json changed by the patch; installing as of ${BEFORE}: ${CHANGED[*]}"
      if bash "${MSWEB_HELPERS}/side_install.sh" "${BEFORE}" "${CHANGED[@]}"; then
        installed=1
        break
      fi
    done
    if [ "${installed}" -ne 1 ]; then
      echo "MSWEB_FATAL: could not install dependencies added by the patch" >&2
      exit 1
    fi
  fi
  node "${MSWEB_HELPERS}/write_version.js"
fi
"""

_STAGE_SH = """#!/bin/bash
set -eo pipefail

export CI=true
__HELPERS__
test "$(node --version)" = "v__NODE_VERSION__"
cd __REPO_DIR__
git status --porcelain > /dev/null
__APPLY__
bash "${MSWEB_HELPERS}/run_tests.sh"
"""

_PRUNE_BLOCK = """RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f __REPO_DIR__/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

_TESTCASE_RE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (.+?)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _render(template: str, **subs: str) -> str:
    out = template.replace("__REPO_DIR__", _REPO_DIR)
    for key, value in subs.items():
        out = out.replace(f"__{key}__", value)
    return out


def _helpers(*helpers: tuple[str, str]) -> str:
    lines = [
        'MSWEB_HELPERS="$(mktemp -d /tmp/msweb-helpers.XXXXXX)"',
        "export MSWEB_HELPERS",
        "trap 'rm -rf \"${MSWEB_HELPERS}\"' EXIT",
    ]
    for name, body in helpers:
        tag = "MSWEB_EOF_" + re.sub(r"\W", "_", name).upper()
        if tag in body:
            raise ValueError(f"heredoc delimiter {tag} occurs inside {name}")
        lines.append(f"cat > \"${{MSWEB_HELPERS}}/{name}\" <<'{tag}'\n{body.rstrip(chr(10))}\n{tag}")
    return "\n".join(lines)


class AntDesignImageBase_ANT_DESIGN_46981_TO_35667(Image):
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
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
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
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    CI=true

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

WORKDIR /home/

RUN git --version && node --version && npm --version && \\
    test "$(node --version)" = "v{_NODE_VERSION}" && \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class AntDesignImageDefault_ANT_DESIGN_46981_TO_35667(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return AntDesignImageBase_ANT_DESIGN_46981_TO_35667(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        sha = self.pr.base.sha
        targets = "\n".join(f"  {shlex.quote(t)}" for t in _test_targets(self.pr))
        run_tests = ("run_tests.sh", _render(_RUN_TESTS_SH, TARGETS=targets, NODE_VERSION=_NODE_VERSION))
        emit_results = ("emit_results.js", _EMIT_RESULTS_JS)
        patch_helpers = (
            ("apply_patch.sh", _APPLY_PATCH_SH),
            ("changed_deps.js", _CHANGED_DEPS_JS),
            ("side_install.sh", _SIDE_INSTALL_SH),
            ("fix_stubs.js", _FIX_STUBS_JS),
            ("write_version.js", _WRITE_VERSION_JS),
            run_tests,
            emit_results,
        )
        test_apply = 'bash "${MSWEB_HELPERS}/apply_patch.sh" /home/test.patch\n'
        fix_apply = test_apply + 'bash "${MSWEB_HELPERS}/apply_patch.sh" /home/fix.patch\n'
        prepare = _render(
            _PREPARE_SH,
            BASE_SHA=sha,
            NODE_VERSION=_NODE_VERSION,
            HELPERS=_helpers(
                ("fix_stubs.js", _FIX_STUBS_JS),
                ("side_install.sh", _SIDE_INSTALL_SH),
                ("write_version.js", _WRITE_VERSION_JS),
                emit_results,
            ),
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(
                ".",
                "run.sh",
                _render(
                    _STAGE_SH,
                    NODE_VERSION=_NODE_VERSION,
                    APPLY="",
                    HELPERS=_helpers(run_tests, emit_results),
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _render(
                    _STAGE_SH,
                    NODE_VERSION=_NODE_VERSION,
                    APPLY=test_apply + _SYNC_DEPS,
                    HELPERS=_helpers(*patch_helpers),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _render(
                    _STAGE_SH,
                    NODE_VERSION=_NODE_VERSION,
                    APPLY=fix_apply + _SYNC_DEPS,
                    HELPERS=_helpers(*patch_helpers),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if not isinstance(image, Image):
            raise ValueError(
                "AntDesignImageDefault_ANT_DESIGN_46981_TO_35667 dependency must be an Image"
            )
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

WORKDIR {_REPO_DIR}

RUN git reset --hard
RUN git checkout {sha}

{copy_commands}
RUN bash /home/prepare.sh

{_render(_PRUNE_BLOCK, BASE_SHA=sha)}"""


_PREVIOUS_OWNER = Instance._registry.get("ant-design/ant-design")


class ANT_DESIGN_46981_TO_35667(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        if not _PR_LOW <= pr.number <= _PR_HIGH:
            raise ValueError(
                f"ant-design PR {pr.number} is outside {_INTERVAL_NAME} ({_PR_LOW}-{_PR_HIGH})"
            )
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AntDesignImageDefault_ANT_DESIGN_46981_TO_35667(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {"PASSED": passed, "FAILED": failed, "SKIPPED": skipped}

        for line in _ANSI_RE.sub("", test_log).splitlines():
            match = _TESTCASE_RE.match(line.strip())
            if match:
                buckets[match.group(1)].add(match.group(2))

        passed -= failed
        skipped -= failed
        skipped -= passed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


Instance.register("ant-design", _INTERVAL_NAME)(ANT_DESIGN_46981_TO_35667)


@Instance.register("ant-design", "ant-design")
class AntDesignRouter_ANT_DESIGN_46981_TO_35667(Instance):

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _PR_LOW <= pr.number <= _PR_HIGH or _PREVIOUS_OWNER is None:
            return ANT_DESIGN_46981_TO_35667(pr, config, *args, **kwargs)
        return _PREVIOUS_OWNER(pr, config, *args, **kwargs)
