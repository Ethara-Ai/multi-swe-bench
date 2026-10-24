import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NUMBER_INTERVAL = "reveal_js_3039_to_907"
REPO_DIR = "reveal.js"
BASE_TAG = "base-3039_to_907"
BASE_IMAGE = "node:14-bullseye"

_DEFAULT_PACKAGES = [
    "ca-certificates",
    "curl",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
    "chromium",
    "fonts-liberation",
]

_PUPPETEER_ENV = (
    "ENV PUPPETEER_SKIP_DOWNLOAD=true\n"
    "ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium\n"
    "ENV CHROME_BIN=/usr/bin/chromium"
)

_GIT_RESILIENCY = (
    "RUN git config --global http.version HTTP/1.1 \\\n"
    "    && git config --global http.postBuffer 1048576000 \\\n"
    "    && git config --global http.lowSpeedLimit 0 \\\n"
    "    && git config --global http.lowSpeedTime 999999 \\\n"
    "    && git config --global core.compression 0 \\\n"
    "    && git config --global submodule.fetchJobs 1"
)

_CLONE_WITH_RETRY = (
    "RUN set -eux; \\\n"
    "    for i in 1 2 3 4 5; do \\\n"
    f"        rm -rf /home/{REPO_DIR}; \\\n"
    f'        if git -C /home clone "${{REPO_URL}}" {REPO_DIR}; then break; fi; \\\n'
    '        echo "clone attempt $i failed, retrying"; sleep 15; \\\n'
    "    done; \\\n"
    f"    test -d /home/{REPO_DIR}/.git"
)

_LIGHT_HARDENING = (
    "RUN git remote remove origin 2>/dev/null || true; \\\n"
    "    git config --local fetch.recurseSubmodules false; \\\n"
    '    git config --local remote.pushDefault ""; \\\n'
    "    git config --local gc.auto 0"
)

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

_RUN_QUNIT_JS = """#!/usr/bin/env node
const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');
const http = require('http');

const TEST_DIR = 'test';
const PER_FILE_TIMEOUT_MS = 30000;
const CHROMIUM = process.env.PUPPETEER_EXECUTABLE_PATH || '/usr/bin/chromium';

(async () => {
    if (!fs.existsSync(TEST_DIR)) {
        console.log('run-qunit: no test directory found');
        process.exit(0);
    }
    const files = fs.readdirSync(TEST_DIR).filter(f => f.endsWith('.html'));
    if (files.length === 0) {
        console.log('run-qunit: no test/*.html files found');
        process.exit(0);
    }

    const MIME = {
        '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
        '.md': 'text/markdown', '.json': 'application/json', '.svg': 'image/svg+xml',
        '.png': 'image/png', '.jpg': 'image/jpeg', '.gif': 'image/gif',
        '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
    };
    const ROOT = process.cwd();
    const server = http.createServer((req, res) => {
        const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\\/+/, '');
        const full = path.resolve(ROOT, rel);
        if (!full.startsWith(ROOT) || !fs.existsSync(full) || fs.statSync(full).isDirectory()) {
            res.statusCode = 404; res.end('not found'); return;
        }
        res.setHeader('Content-Type', MIME[path.extname(full).toLowerCase()] || 'application/octet-stream');
        fs.createReadStream(full).pipe(res);
    });
    await new Promise((r) => server.listen(0, '127.0.0.1', r));
    const BASE_URL = 'http://127.0.0.1:' + server.address().port;

    let browser;
    try {
        browser = await puppeteer.launch({
            executablePath: CHROMIUM,
            args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
            headless: true,
        });
    } catch (e) {
        console.log('run-qunit: FAILED to launch chromium at ' + CHROMIUM + ': ' + e.message);
        process.exit(1);
    }

    let exitCode = 0;
    for (const file of files) {
        const relFile = TEST_DIR + '/' + file;
        const fileUrl = BASE_URL + '/' + TEST_DIR + '/' + file;
        let result;
        let page;
        try {
            page = await browser.newPage();
            page.setDefaultTimeout(PER_FILE_TIMEOUT_MS);
            await page.goto(fileUrl, { waitUntil: 'load', timeout: PER_FILE_TIMEOUT_MS });
            result = await page.evaluate((timeoutMs) => {
                return new Promise((resolve) => {
                    const deadline = Date.now() + timeoutMs - 2000;
                    (function poll() {
                        const el = document.getElementById('qunit-testresult');
                        if (el && el.textContent) {
                            const m = el.textContent.match(/(\\d+)\\s+(?:assertions?|tests?)\\s+of\\s+(\\d+)\\s+passed,\\s*(\\d+)\\s+failed/);
                            if (m) {
                                const tot = parseInt(m[2], 10);
                                const fail = parseInt(m[3], 10);
                                if (tot > 0 || fail > 0 || Date.now() > deadline) {
                                    return resolve({
                                        passed: parseInt(m[1], 10),
                                        total: tot,
                                        failed: fail,
                                    });
                                }
                            }
                        }
                        if (Date.now() > deadline) {
                            return resolve({ error: (typeof QUnit === 'undefined') ? 'QUnit_not_loaded' : 'timeout' });
                        }
                        setTimeout(poll, 100);
                    })();
                });
            }, PER_FILE_TIMEOUT_MS);
        } catch (e) {
            result = { error: e.message };
        } finally {
            if (page) { try { await page.close(); } catch (_) {} }
        }

        const failed = (result && result.failed) || 0;
        const total = (result && result.total) || 0;
        const passed = (result && result.passed) || 0;

        if (result && !result.error && failed === 0 && total > 0) {
            console.log('Testing ' + relFile + ' ..... OK (' + passed + '/' + total + ')');
        } else {
            const reason = (result && result.error) || (failed + ' failed');
            console.log('Testing ' + relFile + ' ..... FAILED (' + passed + '/' + total + ') [' + reason + ']');
            exitCode = 1;
        }
    }

    try { await browser.close(); } catch (_) {}
    try { server.close(); } catch (_) {}
    process.exit(exitCode);
})().catch((e) => {
    console.error('run-qunit: uncaught: ' + e.message);
    process.exit(1);
});
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/reveal.js
git reset --hard
git clean -fdq -e node_modules
git checkout ${BASE_COMMIT}
git clean -fdq -e node_modules
test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"

export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
export OPENSSL_CONF=/dev/null

timeout 420 npm install --ignore-scripts --no-audit --no-fund --legacy-peer-deps 2>&1 \\
    || npm install --ignore-scripts --no-audit --no-fund 2>&1 || true
npm install --no-save --no-audit --no-fund --ignore-scripts puppeteer@10.4.0 2>&1 || true

if [ -d node_modules/node-sass ]; then
    mkdir -p node_modules/node-sass/lib
    cat > node_modules/node-sass/lib/index.js <<'NODE_SASS_STUB'
module.exports = {
  info: 'node-sass stub',
  render: function (o, cb) { cb(null, { css: Buffer.from('') }); },
  renderSync: function () { return { css: Buffer.from('') }; },
  types: {},
  libsassVersion: '0.0.0'
};
NODE_SASS_STUB
    cat > node_modules/node-sass/lib/binding.js <<'NODE_SASS_BINDING'
module.exports = function () {
  return {
    render: function (o, cb) { cb(null, { css: Buffer.from('') }); },
    renderSync: function () { return { css: Buffer.from('') }; },
    libsassVersion: function () { return '0.0.0'; }
  };
};
NODE_SASS_BINDING
fi || true

test -d /home/reveal.js/node_modules
bash /home/check_git_changes.sh || true
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/reveal.js
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
export OPENSSL_CONF=/dev/null
export NODE_PATH=/home/reveal.js/node_modules
timeout -k 30 600 node /home/run-qunit.js 2>&1 || true
true
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/reveal.js
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
export OPENSSL_CONF=/dev/null
export NODE_PATH=/home/reveal.js/node_modules
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
timeout 420 npm install --ignore-scripts --no-audit --no-fund --legacy-peer-deps 2>&1 || true
npm install --no-save --no-audit --no-fund --ignore-scripts puppeteer@10.4.0 2>&1 || true
timeout -k 30 600 node /home/run-qunit.js 2>&1 || true
true
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/reveal.js
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
export OPENSSL_CONF=/dev/null
export NODE_PATH=/home/reveal.js/node_modules
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn /home/fix.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
timeout 420 npm install --ignore-scripts --no-audit --no-fund --legacy-peer-deps 2>&1 || true
npm install --no-save --no-audit --no-fund --ignore-scripts puppeteer@10.4.0 2>&1 || true
timeout -k 30 600 node /home/run-qunit.js 2>&1 || true
true
"""


def _apt_block() -> str:
    packages = " \\\n        ".join(_DEFAULT_PACKAGES)
    return (
        "RUN sed -i '/security.debian.org/d; /bullseye-security/d' /etc/apt/sources.list \\\n"
        "    && sed -i 's|http://deb.debian.org/debian|http://ftp.us.debian.org/debian|g' /etc/apt/sources.list \\\n"
        "    && apt-get update \\\n"
        "    || apt-get -o Acquire::Check-Valid-Until=false update; \\\n"
        "    apt-get install -o Acquire::Retries=10 -o Acquire::http::Timeout=120 \\\n"
        "        -y --no-install-recommends --allow-unauthenticated \\\n"
        f"        {packages} \\\n"
        "    && rm -rf /var/lib/apt/lists/*"
    )


class RevealJsImageBase(Image):
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
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            DockerfileEnhancer._infrastructure_block(self, base_img).strip(),
            "ENV LC_ALL=C.UTF-8",
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(_apt_block())
        sections.append("RUN npm install -g grunt-cli gulp-cli")
        sections.append(_PUPPETEER_ENV)
        sections.append(_GIT_RESILIENCY)
        sections.append(_CLONE_WITH_RETRY)
        sections.append(f"WORKDIR /home/{REPO_DIR}")
        sections.append(_LIGHT_HARDENING)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class RevealJsImageDefault(Image):
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
        return RevealJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        return _PUPPETEER_ENV

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "run-qunit.js", _RUN_QUNIT_JS),
            File(".", "prepare.sh", _PREPARE_SH),
            File(".", "run.sh", _RUN_SH),
            File(".", "test-run.sh", _TEST_RUN_SH),
            File(".", "fix-run.sh", _FIX_RUN_SH),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_full_name() if isinstance(base, Image) else base
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        sha = self.pr.base.sha
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        header = (
            f"FROM {base_name}\n\n"
            f'ARG BASE_COMMIT="{sha}"\n'
            "ENV BASE_COMMIT=${BASE_COMMIT}\n\n"
            f"{self.global_env}\n\n"
            f"{self.extra_setup()}\n\n"
            f"WORKDIR /home/{REPO_DIR}\n\n"
            f"{copy_commands}\n"
            f"RUN git reset --hard && git checkout {sha}\n\n"
            f"RUN bash /home/prepare.sh\n\n"
        )
        tail = f"\n\n{self.clear_env}\n\nCMD [\"/bin/bash\"]\n"
        return header + scrub + tail


@Instance.register("hakimel", NUMBER_INTERVAL)
class REVEAL_JS_3039_TO_907(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RevealJsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    _ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    _PHANTOM_OK = re.compile(r"Testing\s+(\S+)\s+(?:\.+\s*)?(?:OK|ok)\b")
    _PHANTOM_FAIL = re.compile(r"Testing\s+(\S+)\s+(?:\.+\s*)?(?:FAILED|failed|FAIL|fail)\b")
    _V3_PASS = re.compile(r"^[✔✓]\s+(\S+\.html)\s+\[(\d+)/(\d+)\]")
    _V3_FAIL = re.compile(r"^[!✘✗×✕✖]\s+(\S+\.html)\s+\[(\d+)/(\d+)\]")

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for raw in test_log.splitlines():
            line = self._ANSI.sub("", raw).strip()
            if not line:
                continue

            m = self._V3_PASS.match(line)
            if m:
                target = passed_tests if m.group(2) == m.group(3) else failed_tests
                target.add(f"file:{m.group(1)}")
                continue
            m = self._V3_FAIL.match(line)
            if m:
                failed_tests.add(f"file:{m.group(1)}")
                continue
            m = self._PHANTOM_FAIL.search(line)
            if m:
                failed_tests.add(f"file:{m.group(1)}")
                continue
            m = self._PHANTOM_OK.search(line)
            if m:
                passed_tests.add(f"file:{m.group(1)}")
                continue

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
