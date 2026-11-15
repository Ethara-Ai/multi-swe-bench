import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "mrdoob"
_REPO = "three.js"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"three_js_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like three_js_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# .github/workflows/ci.yml runs the unit tests on Node 24 at both base commits, and the
# locked puppeteer@25.5.0 requires node >=22.12.0 (package-lock.json).
NODE_IMAGE = "node:24.22.2-bookworm"

# The QUnit suites under test/unit only run in a browser (test/unit/puppeteer.unit.js +
# UnitTests*.html). Puppeteer's own Chrome-for-Testing download has no linux/arm64 build,
# so the browser is Debian's chromium package (amd64 + arm64) and puppeteer is driven
# with executablePath.
CHROMIUM_PATH = "/usr/bin/chromium"

_TEST_FILE_RE = re.compile(r"^test/unit/.+\.tests\.js$")

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

# Offline QUnit runner. The repo's own runner (test/unit/puppeteer.unit.js) loads QUnit
# from cdn.jsdelivr.net, only prints totals, and runs whole aggregate pages. This one
# serves the repo over loopback, serves QUnit from a local npm install of the version the
# repo's test pages pin, blocks every non-loopback request, imports each requested test
# file on its own (a file that fails to import is reported, the rest still run), and
# prints one stable `PASSED|FAILED|SKIPPED <file> > <module path> > <test>` line per test.
_QUNIT_RUNNER_MJS = r"""import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';

const [rootArg, ...files] = process.argv.slice(2);
if (!rootArg || files.length === 0) {
    console.error('usage: node qunit-runner.mjs <root> <test file>...');
    process.exit(2);
}
const ROOT = path.resolve(rootArg);
const QUNIT_JS = '/home/qunit-assets/node_modules/qunit/qunit/qunit.js';
const CHROMIUM = process.env.QUNIT_CHROMIUM || '__CHROMIUM__';
const RUN_TIMEOUT_MS = 20 * 60 * 1000;
const puppeteer = createRequire('/home/__REPO__/package.json')('puppeteer');

const MIME = {
    '.js': 'text/javascript', '.mjs': 'text/javascript', '.cjs': 'text/javascript',
    '.json': 'application/json', '.html': 'text/html', '.css': 'text/css',
    '.wasm': 'application/wasm', '.png': 'image/png', '.jpg': 'image/jpeg',
    '.svg': 'image/svg+xml', '.txt': 'text/plain', '.ico': 'image/x-icon'
};

const PAGE = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><link rel="icon" href="data:,"></head>
<body><div id="qunit"></div><div id="qunit-fixture"></div>
<script src="/__qunit__/qunit.js"></script>
<script>
QUnit.config.autostart = false;
QUnit.config.reorder = false;
QUnit.config.testTimeout = 30000;
window.__qunitResults = [];
window.__qunitAsserts = {};
window.__qunitLoadErrors = [];
window.__qunitFileOf = {};
window.__qunitDone = false;
QUnit.log((d) => {
    if (d.result) return;
    (window.__qunitAsserts[d.testId] = window.__qunitAsserts[d.testId] || []).push(String(d.message || 'assertion failed') +
        (d.actual !== undefined || d.expected !== undefined ? ' (actual: ' + String(d.actual) + ', expected: ' + String(d.expected) + ')' : ''));
});
QUnit.testDone((d) => {
    window.__qunitResults.push({ testId: d.testId, module: d.module, name: d.name, failed: d.failed, skipped: !!d.skipped, todo: !!d.todo });
});
QUnit.done(() => { window.__qunitDone = true; });
</script>
<script type="importmap">
{ "imports": {
    "three": "/build/three.module.js",
    "three/webgpu": "/build/three.webgpu.js",
    "three/tsl": "/build/three.tsl.js",
    "three/addons/": "/examples/jsm/"
} }
</script>
<script type="module">
const files = JSON.parse(decodeURIComponent(location.hash.slice(1)));
const seen = new Set();
function claim(file) {
    for (const m of QUnit.config.modules) {
        for (const t of m.tests || []) {
            if (seen.has(t.testId)) continue;
            seen.add(t.testId);
            window.__qunitFileOf[t.testId] = file;
        }
    }
}
for (const file of files) {
    try {
        await import('/' + file);
    } catch (e) {
        window.__qunitLoadErrors.push({ file, message: String((e && e.stack) || e) });
    }
    claim(file);
}
QUnit.start();
</script>
</body></html>
`;

const server = http.createServer((req, res) => {
    const pathname = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
    if (pathname === '/__qunit__/runner.html') {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(PAGE);
        return;
    }
    let file;
    if (pathname === '/__qunit__/qunit.js') {
        file = QUNIT_JS;
    } else {
        file = path.join(ROOT, path.normalize(pathname));
        if (file !== ROOT && !file.startsWith(ROOT + path.sep)) file = null;
    }
    let body = null;
    try {
        if (file && fs.statSync(file).isFile()) body = fs.readFileSync(file);
    } catch (e) {
        body = null;
    }
    if (body === null) {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('not found');
        return;
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream' });
    res.end(body);
});

function rel(file) {
    return file.replace(/\\/g, '/').replace(/^\/+/, '');
}

async function main() {
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const port = server.address().port;
    const profile = fs.mkdtempSync('/tmp/qunit-profile-');
    const browser = await puppeteer.launch({
        executablePath: CHROMIUM,
        headless: true,
        args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--hide-scrollbars'],
        userDataDir: profile,
        protocolTimeout: 0
    });
    let out;
    try {
        const page = await browser.newPage();
        page.on('console', (m) => console.error('browser ' + m.type() + ': ' + m.text()));
        page.on('pageerror', (e) => console.error('browser pageerror: ' + ((e && e.message) || e)));
        await page.setRequestInterception(true);
        page.on('request', (r) => {
            const url = r.url();
            if (url.startsWith('http://127.0.0.1:' + port + '/') || url.startsWith('data:') || url.startsWith('blob:')) {
                r.continue();
            } else {
                console.error('qunit-runner: blocked external request ' + url);
                r.abort();
            }
        });
        const list = files.map(rel);
        await page.goto('http://127.0.0.1:' + port + '/__qunit__/runner.html#' + encodeURIComponent(JSON.stringify(list)), { waitUntil: 'load' });
        await page.waitForFunction(() => window.__qunitDone === true, { timeout: RUN_TIMEOUT_MS, polling: 500 });
        out = await page.evaluate(() => ({
            results: window.__qunitResults,
            asserts: window.__qunitAsserts,
            loadErrors: window.__qunitLoadErrors,
            fileOf: window.__qunitFileOf
        }));
    } finally {
        await browser.close();
        server.close();
        fs.rmSync(profile, { recursive: true, force: true });
    }

    const seen = new Map();
    let passed = 0, failed = 0, skipped = 0;
    function emit(status, name) {
        const n = (seen.get(name) || 0) + 1;
        seen.set(name, n);
        if (n > 1) name += ' #' + n;
        if (status === 'PASSED') passed++;
        else if (status === 'FAILED') failed++;
        else skipped++;
        console.log(status + ' ' + name);
    }
    for (const err of out.loadErrors) {
        emit('FAILED', err.file + ' > <file failed to load>');
        console.error('  load error: ' + err.message.split('\n')[0]);
    }
    for (const r of out.results) {
        const file = out.fileOf[r.testId] || '<global>';
        const name = [file, r.module, r.name].filter((s) => s !== undefined && s !== '').join(' > ');
        let status = 'PASSED';
        if (r.skipped || r.todo) status = 'SKIPPED';
        else if (r.failed > 0) status = 'FAILED';
        emit(status, name);
        if (status === 'FAILED') {
            for (const msg of (out.asserts[r.testId] || []).slice(0, 5)) console.error('  assertion: ' + msg.split('\n')[0]);
        }
    }
    console.log('qunit-runner: passed=' + passed + ' failed=' + failed + ' skipped=' + skipped);
}

main().then(() => process.exit(0), (e) => {
    console.error('qunit-runner: ' + ((e && e.stack) || e));
    process.exit(1);
});
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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


def _test_files(pr: PullRequest) -> list[str]:
    files = set()
    for a, b in re.findall(r"^diff --git a/(\S+) b/(\S+)$", pr.test_patch, re.M):
        files.update(f for f in (a, b) if _TEST_FILE_RE.match(f))
    return sorted(files)


def _binary_excludes(pr: PullRequest) -> str:
    # Binary hunks the dataset carries without content ("Binary files ... differ", abbreviated
    # index) cannot be applied by git; none of them is read by a QUnit test (the SPZ tests
    # build their buffers inline), so they are excluded from `git apply`.
    paths = set()
    for chunk in re.split(r"^(?=diff --git )", pr.fix_patch + "\n" + pr.test_patch, flags=re.M):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", chunk)
        if m and re.search(r"^Binary files .* differ$", chunk, re.M):
            paths.update({m.group(1), m.group(2)})
    return "".join(f" --exclude='{p}'" for p in sorted(paths))


def _test_cmd(pr: PullRequest) -> str:
    files = " ".join(_test_files(pr))
    return (
        "# Only the QUnit files the test patch touches; a file the patch adds (or deletes)\n"
        "# exists only in some stages, so each stage runs the ones present at that point.\n"
        "test_files=()\n"
        f"for f in {files}; do\n"
        '    if [ -f "$f" ]; then test_files+=("$f"); fi\n'
        "done\n"
        "\n"
        'if [ "${#test_files[@]}" -gt 0 ]; then\n'
        f'    node /home/qunit-runner.mjs /home/{pr.repo} "${{test_files[@]}}"\n'
        "else\n"
        '    echo "no QUnit test file of the test patch exists at this stage"\n'
        "fi\n"
    )


class ThreeJsImageBase(Image):
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
        return NODE_IMAGE

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

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

# Shard-wide toolchain: a browser for the QUnit suites (Debian chromium ships for amd64
# and arm64; Chrome for Testing has no linux/arm64 build) plus git/ca-certificates.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates git chromium fonts-liberation \\
    && rm -rf /var/lib/apt/lists/* \\
    && if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then \\
           test -x {CHROMIUM_PATH} && echo "chromium installed (amd64 under QEMU emulation: the emulated CPU lacks SSE3, so --version cannot run)"; \\
       else {CHROMIUM_PATH} --version; fi

RUN git config --global --add safe.directory '*'

WORKDIR /home/

# Full-history clone, kept intact: no checkout and no scrub here, both are per-PR.
RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class ThreeJsImageDefault(Image):
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
        return ThreeJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        runner = _QUNIT_RUNNER_MJS.replace("__REPO__", repo).replace("__CHROMIUM__", CHROMIUM_PATH)

        return (
            f"{_PREPARE_HEADER}"
            "# --- 1. pin ---\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            f'test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- 2. provision ---\n"
            "# QUnit runner the run scripts use; written outside the repo.\n"
            "cat > /home/qunit-runner.mjs <<'__QUNIT_RUNNER_EOF__'\n"
            f"{runner}"
            "__QUNIT_RUNNER_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# The repo's lockfile, without puppeteer's Chrome-for-Testing download (no arm64\n"
            "# build; the base image's chromium is used instead).\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            "export PUPPETEER_SKIP_DOWNLOAD=true\n"
            "for attempt in 1 2 3; do\n"
            "    npm ci && break || true\n"
            '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            "\n"
            "# The test pages load QUnit from a CDN; install the exact version they pin so the\n"
            "# runner can serve it offline.\n"
            "qunit_version=$(grep -o 'npm/qunit@[0-9][0-9.]*' test/unit/UnitTests.html | head -1 | cut -d@ -f2)\n"
            'echo "QUnit version pinned by test/unit/UnitTests.html: $qunit_version"\n'
            'test -n "$qunit_version"\n'
            "mkdir -p /home/qunit-assets\n"
            "for attempt in 1 2 3; do\n"
            '    npm install --prefix /home/qunit-assets --no-save --no-package-lock "qunit@$qunit_version" && break || true\n'
            '    echo "prepare: qunit install attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            "\n"
            "# --- 3. gate ---\n"
            "# Puppeteer and the QUnit build resolve, the importmap targets exist, and the runner\n"
            "# drives chromium end to end on a throwaway suite.\n"
            "test -s /home/qunit-runner.mjs\n"
            "test -s /home/qunit-assets/node_modules/qunit/qunit/qunit.js\n"
            'grep -q "QUnit $qunit_version" /home/qunit-assets/node_modules/qunit/qunit/qunit.js\n'
            "test -s build/three.module.js\n"
            "test -s build/three.webgpu.js\n"
            "test -s build/three.tsl.js\n"
            "node -e \"const p = require('puppeteer'); console.log('puppeteer -> ' + require.resolve('puppeteer') + ' ' + typeof p.launch)\"\n"
            "# Chromium refuses to start under QEMU user-mode emulation (the emulated CPU lacks\n"
            "# SSE3), so an amd64 image cross-built on an arm64 host cannot drive it at build\n"
            "# time. Detect that case (x86_64 userland over an ARM host CPU) and check the\n"
            "# chromium binary instead; native arm64/amd64 builds run the end-to-end check.\n"
            'if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then\n'
            '    echo "gate: amd64 under QEMU emulation; skipping the chromium end-to-end run"\n'
            f"    test -x {CHROMIUM_PATH}\n"
            "else\n"
            "    rm -rf /tmp/qunit-gate && mkdir -p /tmp/qunit-gate\n"
            "    printf \"QUnit.module('Gate', () => { QUnit.test('ok', (assert) => { assert.ok(true); }); });\\n\" "
            "    > /tmp/qunit-gate/gate.tests.js\n"
            "    node /home/qunit-runner.mjs /tmp/qunit-gate gate.tests.js | tee /tmp/qunit-gate/out.txt\n"
            "    grep -qx 'PASSED gate.tests.js > Gate > ok' /tmp/qunit-gate/out.txt\n"
            "    rm -rf /tmp/qunit-gate\n"
            "fi\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{_test_cmd(self.pr)}"
        )

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fd\n"
            f"git apply --whitespace=nowarn{_binary_excludes(self.pr)} /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fd\n"
            f"git apply --whitespace=nowarn{_binary_excludes(self.pr)} /home/test.patch /home/fix.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

RUN cd /home/{self.pr.repo} && test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


_LOAD_FAILURE_MARKERS = frozenset(
    {"<suite failed to run>", "<file failed to load>", "<file failed to run>"}
)


def _add_file_results(passed: set[str], failed: set[str], skipped: set[str]) -> None:
    """Add one result per test file, named by the bare file path.

    A test file that cannot load before the fix (it imports code the fix adds) is
    reported under a placeholder such as `<file> > <file failed to load>`, while after
    the fix its tests pass under their own names, so no single name links the stages.
    The file-level result does: FAILED when the file failed to load, when any of its
    tests failed, or when it ran no test at all (node:test then reports one passing
    entry named after the file itself); PASSED when its tests passed; SKIPPED when
    every test in it was skipped.
    """
    files: dict[str, dict] = {}
    for status, names in (("passed", passed), ("failed", failed), ("skipped", skipped)):
        for name in list(names):
            file, sep, rest = name.partition(" > ")
            if not sep or file.startswith("<"):
                continue
            st = files.setdefault(
                file,
                {"passed": 0, "failed": 0, "skipped": 0, "load_failed": False, "empty": False},
            )
            if rest in _LOAD_FAILURE_MARKERS:
                st["load_failed"] = True
            elif rest == file and status == "passed":
                st["empty"] = True
                names.discard(name)
            else:
                st[status] += 1
    for file, st in files.items():
        if st["load_failed"] or st["failed"] or (st["empty"] and not st["passed"]):
            failed.add(file)
        elif st["passed"]:
            passed.add(file)
        elif st["skipped"]:
            skipped.add(file)


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class THREE_JS_34222_TO_34202(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO}#{pr.number} is outside {_ERA_FILE} (#{_ERA_LO}-#{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ThreeJsImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) (\S.*)$")

        for line in log.splitlines():
            m = result_re.match(line.rstrip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        _add_file_results(passed_tests, failed_tests, skipped_tests)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
