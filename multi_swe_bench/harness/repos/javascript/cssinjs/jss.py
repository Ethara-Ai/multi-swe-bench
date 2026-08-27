from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase_99999_to_0(Image):
    """Base image for a cssinjs/jss PR, tagged ``base-pr-<number>``.

    Deliberately minimal: Debian bookworm + Node 16 (OpenSSL 1.1.1, so the
    repo's webpack 4 tool-chain keeps working) plus a headless Chromium, which
    the repo's karma test runner drives.  No dependency install happens here --
    the tree that gets installed against is the PR's base commit, which is
    resolved in ``prepare.sh`` on the per-PR layer.

    The ``RUN git clone`` line is the *last* instruction emitted on purpose:
    ``DockerfileEnhancer`` rewrites it into clone + WORKDIR + reset + checkout
    + history hardening + ``CMD``, so anything after it would land below the
    generated ``CMD``.
    """

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
        return "node:16-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    chromium \\
    curl \\
    fonts-liberation \\
    git \\
    xdg-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g n@9 || true

{code}

"""


class ImageDefault_99999_to_0(Image):
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
        return ImageBase_99999_to_0(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        base_sha = self.pr.base.sha

        def render(body: str) -> str:
            return (
                body.replace("[[REPO_NAME]]", repo)
                .replace("[[ORG_NAME]]", org)
                .replace("[[BASE_SHA]]", base_sha)
            )

        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                render(
                    r"""#!/bin/bash
set -e

REPO_DIR=/home/[[REPO_NAME]]
BASE_SHA=[[BASE_SHA]]

cd "$REPO_DIR"

# The base image is tagged per PR (base-pr-<number>) and DockerfileEnhancer has
# already pinned it to this exact commit and scrubbed its history -- no remote,
# no other refs.  Nothing here re-adds a remote or re-fetches: the checkout below
# must resolve from the objects already in the image, and fail loudly if it ever
# cannot, rather than quietly reaching back out to the network.
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout "$BASE_SHA"
bash /home/check_git_changes.sh

# ---------------------------------------------------------------------------
# Tool-chain detection helpers, sourced by run.sh / test-run.sh / fix-run.sh.
# Everything is derived from the checked-out tree so the same module keeps
# working for other commits of this repo (and for other JS repos).
# ---------------------------------------------------------------------------
cat > /home/jsenv.sh <<'JSENV_EOF'
#!/bin/bash
# Repo-agnostic JavaScript tool-chain detection for the mswebench harness.

export CI=true
export NODE_ENV="${NODE_ENV:-test}"
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=4096}"
export CHROME_BIN="${CHROME_BIN:-/usr/bin/chromium}"
export CHROMIUM_BIN="$CHROME_BIN"

js_detect_pm() {
    if [ -f pnpm-lock.yaml ]; then
        echo pnpm
    elif [ -f yarn.lock ]; then
        echo yarn
    elif [ -f bun.lockb ] || [ -f bun.lock ]; then
        echo bun
    else
        echo npm
    fi
}

js_has_script() {
    node -e "var s=(require('./package.json').scripts)||{};process.exit(s['$1']?0:1)" \
        > /dev/null 2>&1
}

# Only ever moves Node forward: the image ships a Node old enough for this
# repo's era, and downgrading below it would break the pre-installed tooling.
js_select_node() {
    local want="" cur=""
    if [ -f .nvmrc ]; then
        want="$(tr -dc '0-9.\n' < .nvmrc | head -n1 | cut -d. -f1)"
    fi
    if [ -z "$want" ]; then
        want="$(node -e "var e=(require('./package.json').engines)||{};var m=String(e.node||'').match(/(\d+)/);process.stdout.write(m?m[1]:'')" 2>/dev/null || true)"
    fi
    [ -n "$want" ] || return 0
    cur="$(node -p 'process.versions.node.split(".")[0]')"
    if command -v n > /dev/null 2>&1 && [ "$want" -gt "$cur" ] 2>/dev/null; then
        n install "$want" > /dev/null 2>&1 || true
    fi
}

js_install() {
    case "$(js_detect_pm)" in
        pnpm)
            corepack enable > /dev/null 2>&1 || npm install -g pnpm > /dev/null 2>&1 || true
            pnpm install --no-frozen-lockfile || pnpm install || true
            ;;
        yarn)
            yarn install --frozen-lockfile --network-timeout 600000 \
                || yarn install --network-timeout 600000 \
                || true
            ;;
        bun)
            bun install || true
            ;;
        *)
            npm ci || npm install --legacy-peer-deps || npm install --force || true
            ;;
    esac
}

# Several packages of this monorepo are consumed through their built `dist/`
# entry points, so the bundles have to be regenerated after any source patch.
js_build() {
    js_has_script build || return 0
    case "$(js_detect_pm)" in
        pnpm) pnpm run build ;;
        yarn) yarn build ;;
        bun)  bun run build ;;
        *)    npm run build ;;
    esac
}

js_karma_config() {
    local c
    for c in karma.conf.js karma.conf.cjs karma.config.js karma.conf.ci.js test/karma.conf.js tests/karma.conf.js; do
        if [ -f "$c" ]; then
            echo "$PWD/$c"
            return 0
        fi
    done
    return 1
}

js_bin() {
    if [ -x "./node_modules/.bin/$1" ]; then
        echo "./node_modules/.bin/$1"
    else
        echo "npx --no-install $1"
    fi
}

# Emits `MSWEBENCH_TEST <PASS|FAIL|SKIP> :: <suite > test>` lines, which is what
# parse_log consumes.  karma's own reporters encode the verdict as an ANSI
# colour only, which does not survive the mandatory ANSI stripping.
js_test() {
    local base_conf
    if base_conf="$(js_karma_config)"; then
        export MSWEBENCH_REPO_DIR="$PWD"
        export MSWEBENCH_KARMA_CONFIG="$base_conf"
        $(js_bin karma) start /home/karma.mswebench.js --single-run
    elif [ -f jest.config.js ] || [ -f jest.config.cjs ] || [ -f jest.config.mjs ] \
        || [ -f jest.config.ts ] || node -e "process.exit(require('./package.json').jest?0:1)" 2> /dev/null; then
        $(js_bin jest) --verbose --ci --colors=false
    elif ls vitest.config.* vite.config.* > /dev/null 2>&1; then
        $(js_bin vitest) run --reporter=verbose --no-color
    elif [ -f .mocharc.yml ] || [ -f .mocharc.json ] || [ -f .mocharc.js ]; then
        $(js_bin mocha) --reporter spec --no-colors
    else
        npm test
    fi
}
JSENV_EOF

# ---------------------------------------------------------------------------
# Test-file attribution.
#
# karma-mocha's adapter reports only `{suite, description}` -- mocha populates
# `test.file` from the `file` argument of the `pre-require` event, which in the
# browser is emitted once with `null`.  Node's mocha re-emits `pre-require` per
# file; this webpack loader reproduces that by prefixing every preprocessed
# module with a call that re-emits it for the module's own path.  The adapter
# then ships `test.file` to the reporter via `client.mocha.expose` (adapter.js
# 160-167), which is how test names get a real `packages/...js::` prefix.
#
# The marker is emitted without a trailing newline so original line numbers --
# and therefore the inline source maps used in failure output -- are unchanged.
# ---------------------------------------------------------------------------
cat > /home/mswebench-file-loader.js <<'LOADER_EOF'
/* eslint-disable */
const path = require('path')

module.exports = function (source) {
  if (this.cacheable) this.cacheable()

  const root = process.env.MSWEBENCH_REPO_DIR || process.cwd()
  let rel = path.relative(root, this.resourcePath)
  rel = rel.split(path.sep).join('/')
  if (!rel || rel.indexOf('..') === 0) rel = this.resourcePath

  const marker =
    'if(typeof window!=="undefined"&&window.__mswebenchSetFile)' +
    'window.__mswebenchSetFile(' + JSON.stringify(rel) + ');'

  // Keep a leading BOM in first position; babel rejects it mid-file.
  if (source.charCodeAt(0) === 0xfeff) {
    return source.charAt(0) + marker + source.slice(1)
  }
  return marker + source
}
LOADER_EOF

cat > /home/mswebench-mocha-hook.js <<'HOOK_EOF'
/* eslint-disable */
// Loaded after mocha.js and karma-mocha's adapter, before any test bundle.
// Re-emitting `pre-require` rebinds the BDD globals with a new `file`, exactly
// as mocha's own Node-side loader does for each file it requires.
(function (window) {
  window.__mswebenchSetFile = function (file) {
    try {
      var m = window.mocha
      if (m && m.suite && typeof m.suite.emit === 'function') {
        m.suite.emit('pre-require', window, file, m)
      }
    } catch (e) {
      /* fall back to un-prefixed names */
    }
  }
})(window)
HOOK_EOF

# ---------------------------------------------------------------------------
# Harness karma configuration.  It wraps the repo's own karma config instead of
# replacing it, so the file list, preprocessors and webpack setup stay exactly
# as upstream defines them; only the browser and the reporter are overridden.
# ---------------------------------------------------------------------------
cat > /home/karma.mswebench.js <<'KARMA_EOF'
/* eslint-disable */
// mswebench harness karma configuration.
// Wraps the repository's own karma config and swaps in a headless Chromium
// launcher plus a machine-readable reporter.
const path = require('path')

const REPO_DIR = process.env.MSWEBENCH_REPO_DIR || process.cwd()
const BASE_CONFIG = process.env.MSWEBENCH_KARMA_CONFIG || path.join(REPO_DIR, 'karma.conf.js')

function MswebenchReporter(baseReporterDecorator) {
  baseReporterDecorator(this)

  this.onSpecComplete = function (browser, result) {
    const suite = (result.suite || []).join(' > ')
    const full = (suite ? suite + ' > ' : '') + (result.description || '')
    let name = String(full).replace(/\s+/g, ' ').trim()
    if (!name) return
    // `file` arrives via client.mocha.expose; degrade to the bare suite path
    // if attribution ever fails rather than dropping the result.
    const file = result.mocha && result.mocha.file
    if (file) name = String(file).replace(/\s+/g, ' ').trim() + '::' + name
    const status = result.skipped ? 'SKIP' : result.success ? 'PASS' : 'FAIL'
    this.write('MSWEBENCH_TEST ' + status + ' :: ' + name + '\n')
  }

  this.onRunComplete = function () {
    this.write('MSWEBENCH_RUN_COMPLETE\n')
  }
}
MswebenchReporter.$inject = ['baseReporterDecorator']

module.exports = function (config) {
  const base = require(BASE_CONFIG)
  if (typeof base === 'function') {
    base(config)
  } else if (base && typeof base === 'object') {
    config.set(base)
  }

  const launchers = Object.assign({}, config.customLaunchers, {
    MswebenchChromeHeadless: {
      base: 'ChromeHeadless',
      flags: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--disable-software-rasterizer'
      ]
    }
  })

  const plugins =
    config.plugins && config.plugins.length ? config.plugins.slice() : ['karma-*']
  plugins.push({'reporter:mswebench': ['type', MswebenchReporter]})

  // Prefix every preprocessed module with its own path (see the loader).
  const webpackConf = Object.assign({}, config.webpack)
  webpackConf.module = Object.assign({}, webpackConf.module)
  webpackConf.module.rules = (webpackConf.module.rules || []).slice()
  webpackConf.module.rules.unshift({
    test: /\.jsx?$/,
    enforce: 'pre',
    exclude: /node_modules/,
    use: [{loader: '/home/mswebench-file-loader.js'}]
  })

  // Must load after mocha + karma-mocha's adapter (both unshifted by the
  // framework) and before any test bundle.
  const files = (config.files || []).slice()
  files.unshift({
    pattern: '/home/mswebench-mocha-hook.js',
    included: true,
    served: true,
    watched: false
  })

  config.set({
    basePath: REPO_DIR,
    customLaunchers: launchers,
    browsers: ['MswebenchChromeHeadless'],
    reporters: ['mswebench'],
    plugins: plugins,
    webpack: webpackConf,
    files: files,
    colors: false,
    autoWatch: false,
    singleRun: true,
    concurrency: 1,
    captureTimeout: 180000,
    browserNoActivityTimeout: 180000,
    browserDisconnectTimeout: 60000,
    browserDisconnectTolerance: 2,
    client: Object.assign({}, config.client, {
      captureConsole: false,
      // karma-mocha copies these off the mocha Test object into result.mocha.
      mocha: Object.assign({}, config.client && config.client.mocha, {
        expose: ['file']
      })
    })
  })
}
KARMA_EOF

source /home/jsenv.sh
js_select_node
js_install
js_build || true
"""
                ),
            ),
            File(
                ".",
                "run.sh",
                render(
                    """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/[[REPO_NAME]]
source /home/jsenv.sh

git checkout -- .
git clean -fd

js_build
js_test
"""
                ),
            ),
            File(
                ".",
                "test-run.sh",
                render(
                    """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/[[REPO_NAME]]
source /home/jsenv.sh

git checkout -- .
git clean -fd

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

js_build
js_test
"""
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                render(
                    """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/[[REPO_NAME]]
source /home/jsenv.sh

git checkout -- .
git clean -fd

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi

js_build
js_test
"""
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("cssinjs", "jss")
@Instance.register("cssinjs", "jss_99999_to_0")
class JSS_99999_to_0(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault_99999_to_0(self.pr, self._config)

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

        # ANSI first: karma/jest/mocha all colourise by default and every
        # pattern below anchors on literal characters.
        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        log = log.replace("\r\n", "\n").replace("\r", "\n")

        # jss declares sibling `describe()` blocks with identical titles and
        # identical `it()` titles inside them (e.g. two "comma-separated
        # values" suites in jss-plugin-default-unit), so a fully qualified name
        # is not by itself unique. Repeats are numbered in execution order,
        # which the runner keeps stable across the three stages.
        seen: dict[str, int] = {}

        def unique(base_name: str) -> str:
            count = seen.get(base_name, 0)
            seen[base_name] = count + 1
            return base_name if count == 0 else f"{base_name} #{count + 1}"

        # Primary source: the harness karma reporter installed by prepare.sh.
        # Names are `suite > nested suite > test`, carry no timing or count
        # metadata, and are therefore identical across the three stages.
        marker = re.compile(
            r"^MSWEBENCH_TEST[ \t]+(PASS|FAIL|SKIP)[ \t]+::[ \t]+(.+?)[ \t]*$",
            re.MULTILINE,
        )
        matched = False
        for m in marker.finditer(log):
            matched = True
            status = m.group(1)
            name = unique(m.group(2))
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        if not matched:
            # Fallback for the non-karma runners js_test() can select. Suite
            # context is tracked so leaf names cannot collide.
            re_suite_header = re.compile(r"^(?:PASS|FAIL)\s+(\S+.*?)(?:\s+\(\d+.*\))?$")
            re_pass = re.compile(r"^(\s*)[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
            re_fail = re.compile(r"^(\s*)[✗✘✕×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
            re_skip = re.compile(
                r"^(\s*)[○◌↻-]\s+(?:skipped\s+)?(.+?)(?:\s+\(\d+\s*m?s\))?$"
            )
            re_plain = re.compile(r"^(\s*)([A-Za-z0-9_#\"'\[(].*?)\s*$")

            suite_file = ""
            stack: list[tuple[int, str]] = []

            def qualify(indent: int, leaf: str) -> str:
                parts = [name for depth, name in stack if depth < indent]
                if suite_file:
                    parts = [suite_file] + parts
                parts.append(leaf)
                return " > ".join(p for p in parts if p)

            for raw_line in log.split("\n"):
                line = raw_line.rstrip()
                if not line.strip():
                    continue

                header = re_suite_header.match(line)
                if header:
                    suite_file = header.group(1).strip()
                    stack = []
                    continue

                m = re_pass.match(line)
                if m:
                    passed_tests.add(unique(qualify(len(m.group(1)), m.group(2).strip())))
                    continue

                m = re_fail.match(line)
                if m:
                    failed_tests.add(unique(qualify(len(m.group(1)), m.group(2).strip())))
                    continue

                m = re_skip.match(line)
                if m:
                    skipped_tests.add(unique(qualify(len(m.group(1)), m.group(2).strip())))
                    continue

                m = re_plain.match(raw_line)
                if m and raw_line.startswith(" "):
                    indent = len(m.group(1))
                    stack = [(d, n) for d, n in stack if d < indent]
                    stack.append((indent, m.group(2).strip()))

        # TestResult.__post_init__ rejects overlapping sets, so a test reported
        # twice (retries, multiple browsers) resolves to its worst outcome.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
