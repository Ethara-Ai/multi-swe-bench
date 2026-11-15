import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "vega"
_REPO = "vega"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"vega_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like vega_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# npm workspaces monorepo (packages/*), lerna 9 builds, tape tests. Root
# package.json engines.node ">=22.0.0"; CI matrix node 22/23/24.
NODE_IMAGE = "node:22.23.4-bookworm"

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

# Lerna drives the builds through Nx: no daemon, no cloud, no terminal UI.
_NX_ENV = "export NX_DAEMON=false NX_NO_CLOUD=true NX_TUI=false NX_SKIP_NX_CACHE=true\n"

_TAPE_REPORT_MJS = r"""// Runs ONE tape test file and prints one `STATUS <file> > <test name>` line per tape
// test (subtests nested as `parent > child`). A test is FAILED if any of its
// assertions failed, SKIPPED if tape skipped it, PASSED otherwise. Assertion messages
// are not part of the name: tests in this repo put computed values in them.
// Usage (cwd = the package directory, like its `npm test`):
//   node /home/tape-report.mjs <package-dir-relative-to-repo> <test-file>
import { createRequire } from 'module';
import path from 'path';
import { pathToFileURL } from 'url';

const [pkgDir, file] = process.argv.slice(2);
const prefix = pkgDir + '/' + file;

const require = createRequire(path.join(process.cwd(), 'package.json'));
const tape = (await import(pathToFileURL(require.resolve('tape')).href)).default;

const tests = new Map();
const order = [];
let loadError = null;

function clean(s) {
    return String(s).replace(/\s+/g, ' ').trim();
}

function fullName(id) {
    const parts = [];
    for (let t = tests.get(id); t; t = t.parent === undefined ? null : tests.get(t.parent)) {
        parts.unshift(clean(t.name));
    }
    return parts.join(' > ');
}

const stream = tape.createStream({ objectMode: true });
stream.on('data', row => {
    if (row.type === 'test') {
        tests.set(row.id, { name: row.name, parent: row.parent, skip: !!(row.skip || row.todo), failed: false, msgs: [] });
        order.push(row.id);
    } else if (row.type === 'assert') {
        const t = tests.get(row.test);
        if (t && !row.ok && !row.skip && !row.todo) {
            t.failed = true;
            if (t.msgs.length < 5) t.msgs.push(clean(row.name) + (row.operator ? ' [' + row.operator + ']' : ''));
        }
    }
});
let reported = false;
function report() {
    if (reported) return;
    reported = true;
    const seen = new Map();
    let passed = 0, failed = 0, skipped = 0;
    function emit(status, name, msgs) {
        const n = (seen.get(name) || 0) + 1;
        seen.set(name, n);
        if (n > 1) name += ' #' + n;
        if (status === 'PASSED') passed++;
        else if (status === 'FAILED') failed++;
        else skipped++;
        process.stdout.write(status + ' ' + name + '\n');
        for (const m of msgs || []) process.stdout.write('    ! ' + m + '\n');
    }
    if (loadError !== null) emit('FAILED', prefix + ' > <file failed to load>', [loadError]);
    for (const id of order) {
        const t = tests.get(id);
        const status = t.failed ? 'FAILED' : t.skip ? 'SKIPPED' : 'PASSED';
        emit(status, prefix + ' > ' + fullName(id), t.msgs);
    }
    if (passed + failed + skipped === 0) emit('FAILED', prefix + ' > <file reported no tests>');
    process.stdout.write('tape-report: ' + prefix + ' passed=' + passed + ' failed=' + failed +
                         ' skipped=' + skipped + '\n');
}
stream.on('end', report);
// A file that throws before registering a test may never end tape's stream.
process.on('exit', report);

try {
    await import(pathToFileURL(path.resolve(file)).href);
} catch (e) {
    loadError = String((e && e.stack) || e).split('\n').slice(0, 4).join(' | ');
    process.exitCode = 1;
}
"""

_SCENE_COMPARE_MJS = r"""// packages/vega/test/scene-test.js in comparison mode, without touching the repo.
// At these commits scene-test.js ships with GENERATE_SCENES = true, i.e. it rewrites
// every golden in test/scenegraphs/ instead of comparing (upstream turned comparison
// on in #4305). This runner keeps the test's exact procedure -- same spec list and
// wordcloud filter, same seeded RNG, same concurrent forEach, same text metrics,
// same vega.sceneEqual -- and reports one result per spec instead of writing files.
// One deviation: the loader is pinned to file mode, so a spec that fetches remote data
// (basemap) fails the same way with or without network instead of depending on it;
// such specs are reported SKIPPED.
// Usage (cwd = packages/vega, TZ=America/Los_Angeles like its `npm test`):
//   node /home/vega-scene-compare.mjs
import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';

const PREFIX = 'packages/vega/test/scene-test.js (golden compare)';
const vega = await import(pathToFileURL(path.resolve('index.js')).href);

const specdir = process.cwd() + '/test/specs-valid/';
const testdir = process.cwd() + '/test/scenegraphs/';
const loader = vega.loader({ baseURL: 'test/', mode: 'file' });
const specs = JSON.parse(fs.readFileSync('test/specs-valid.json', 'utf8'))
    .filter(spec => spec !== 'wordcloud');

const remote = new Set(specs.filter(name => {
    try {
        return /"url"\s*:\s*"https?:/.test(fs.readFileSync(specdir + name + '.vg.json', 'utf8'));
    } catch (e) {
        return false;
    }
}));

vega.setRandom(vega.randomLCG(123456789));
vega.textMetrics.canvas(false);

const results = new Map();
let count = specs.length;

function report() {
    let passed = 0, failed = 0, skipped = 0;
    const seen = new Set();
    for (const name of specs) {
        if (seen.has(name)) continue;
        seen.add(name);
        const line = PREFIX + ' > scene: ' + name;
        if (remote.has(name)) {
            skipped++;
            process.stdout.write('SKIPPED ' + line + '\n    ! loads remote data (needs network)\n');
            continue;
        }
        const r = results.get(name) || { ok: false, why: 'no result' };
        if (r.ok) passed++;
        else failed++;
        process.stdout.write((r.ok ? 'PASSED ' : 'FAILED ') + line + '\n');
        if (!r.ok) process.stdout.write('    ! ' + r.why + '\n');
    }
    process.stdout.write('vega-scene-compare: passed=' + passed + ' failed=' + failed + ' skipped=' + skipped + '\n');
}

if (count === 0) report();

specs.forEach(async function(name) {
    try {
        const file = testdir + name + '.json',
              spec = JSON.parse(fs.readFileSync(specdir + name + '.vg.json')),
              runtime = vega.parse(spec),
              view = new vega.View(runtime, {
                  loader: loader,
                  renderer: 'none'
              }).finalize();

        await view.runAsync();

        const actual = view.scenegraph().toJSON(2);
        const expect = fs.readFileSync(file) + '';
        const isEqual = vega.sceneEqual(JSON.parse(actual), JSON.parse(expect));
        results.set(name, { ok: !!isEqual, why: 'scenegraph differs from test/scenegraphs/' + name + '.json' });
    } catch (err) {
        results.set(name, { ok: false, why: String((err && err.message) || err).split('\n')[0] });
    } finally {
        if (--count === 0) report();
    }
});
"""

_TSC_REPORT_MJS = r"""// Turns the output of the repo's `npm run typecheck` (root `tsc`) into one result
// per vega-typings file: FAILED if tsc reported an error in it, PASSED otherwise.
// Usage (cwd = repo root): node /home/tsc-report.mjs <tsc-output-file> <tsc-exit-code>
import fs from 'fs';
import path from 'path';

const [outFile, rcArg] = process.argv.slice(2);
const PREFIX = 'typecheck (tsc)';
const rc = Number(rcArg || 0);
const out = fs.existsSync(outFile) ? fs.readFileSync(outFile, 'utf8') : '';

const errored = new Set();
for (const line of out.split('\n')) {
    const m = /^(\S[^(]*?)\(\d+,\d+\): error TS\d+/.exec(line);
    if (m) errored.add(m[1].replace(/\\/g, '/'));
}

function walk(dir) {
    if (!fs.existsSync(dir)) return [];
    const files = [];
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.posix.join(dir, ent.name);
        if (ent.isDirectory()) files.push(...walk(p));
        else if (/\.tsx?$/.test(ent.name)) files.push(p);
    }
    return files;
}

const files = new Set([
    ...walk('packages/vega-typings/types'),
    ...walk('packages/vega-typings/tests'),
    ...errored
]);

let passed = 0, failed = 0;
for (const f of [...files].sort()) {
    const ok = !errored.has(f);
    if (ok) passed++;
    else failed++;
    process.stdout.write((ok ? 'PASSED ' : 'FAILED ') + PREFIX + ' > ' + f + '\n');
}
if (rc !== 0 && errored.size === 0) {
    failed++;
    process.stdout.write('FAILED ' + PREFIX + ' > <tsc exited ' + rc + ' without file diagnostics>\n');
}
process.stdout.write('tsc-report: passed=' + passed + ' failed=' + failed + '\n');
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


def _packages(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({m.group(1) for f in files if (m := re.match(r"packages/([^/]+)/", f))})


def _test_cmd(pr: PullRequest) -> str:
    packages = " ".join(_packages(pr))
    return (
        _NX_ENV
        + "rm -f /tmp/vega-test-out.txt /tmp/vega-build.log /tmp/vega-tsc.txt\n"
        "\n"
        "# Rebuild every package (each package's `pretest` builds it): the tests import\n"
        "# their own package from source, but every other vega-* package through its\n"
        "# build/ bundle, so a patched source only reaches them after a rebuild.\n"
        "if ! node_modules/.bin/lerna run build --concurrency 4 > /tmp/vega-build.log 2>&1; then\n"
        "    tail -60 /tmp/vega-build.log\n"
        '    echo "lerna run build failed" >&2\n'
        "    exit 1\n"
        "fi\n"
        'echo "lerna run build: ok"\n'
        "\n"
        "# The packages the test patch touches, each with all of its tape test files\n"
        "# (its `npm test` glob test/**/*-test.js, run from the package directory with\n"
        "# the TZ its test script sets). packages/vega/test/scene-test.js ships with\n"
        "# GENERATE_SCENES = true (it rewrites the goldens instead of comparing), so the\n"
        "# vega package's scenes run through the comparing runner instead; vega-typings\n"
        "# is checked by the repo's typecheck (root tsc).\n"
        "{\n"
        f"for pkg in {packages}; do\n"
        '    dir="packages/$pkg"\n'
        '    [ -d "$dir" ] || continue\n'
        "    tz=$(node -e \"const s = (require('./$dir/package.json').scripts || {}).test || ''; "
        "const m = /\\\\bTZ=(\\\\S+)/.exec(s); process.stdout.write(m ? m[1] : '')\")\n"
        '    if [ -d "$dir/test" ]; then\n'
        '        for f in $(cd "$dir" && find test -type f -name \'*-test.js\' | LC_ALL=C sort); do\n'
        '            if [ "$dir/$f" = "packages/vega/test/scene-test.js" ]; then continue; fi\n'
        "            rc=0\n"
        '            (cd "$dir" && env ${tz:+TZ=$tz} node /home/tape-report.mjs "$dir" "$f") || rc=$?\n'
        '            if [ "$rc" -ne 0 ]; then echo "tape-report: $dir/$f exited with code $rc"; fi\n'
        "        done\n"
        "    fi\n"
        '    if [ "$pkg" = "vega" ]; then\n'
        "        rc=0\n"
        '        (cd "$dir" && env ${tz:+TZ=$tz} node /home/vega-scene-compare.mjs) || rc=$?\n'
        '        if [ "$rc" -ne 0 ]; then echo "vega-scene-compare exited with code $rc"; fi\n'
        "    fi\n"
        '    if [ "$pkg" = "vega-typings" ]; then\n'
        "        rc=0\n"
        "        node_modules/.bin/tsc > /tmp/vega-tsc.txt 2>&1 || rc=$?\n"
        "        cat /tmp/vega-tsc.txt\n"
        '        node /home/tsc-report.mjs /tmp/vega-tsc.txt "$rc"\n'
        "    fi\n"
        "done\n"
        "} 2>&1 | tee /tmp/vega-test-out.txt\n"
        "\n"
        "if ! grep -qE '^(PASSED|FAILED|SKIPPED) ' /tmp/vega-test-out.txt; then\n"
        '    echo "no test reported a result" >&2\n'
        "    exit 1\n"
        "fi\n"
    )


class VegaImageBase(Image):
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

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class VegaImageDefault(Image):
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
        return VegaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo

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
            "# Result reporters the run scripts use; written outside the repo.\n"
            "cat > /home/tape-report.mjs <<'__TAPE_REPORT_EOF__'\n"
            f"{_TAPE_REPORT_MJS}"
            "__TAPE_REPORT_EOF__\n"
            "cat > /home/vega-scene-compare.mjs <<'__SCENE_COMPARE_EOF__'\n"
            f"{_SCENE_COMPARE_MJS}"
            "__SCENE_COMPARE_EOF__\n"
            "cat > /home/tsc-report.mjs <<'__TSC_REPORT_EOF__'\n"
            f"{_TSC_REPORT_MJS}"
            "__TSC_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# The install CI runs (.github/workflows/test.yml: npm ci). The root postinstall\n"
            "# only rsyncs vega-datasets into docs/data (tracked docs files, no test reads\n"
            "# them), so it is switched off the way scripts/postinstall.sh provides.\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            "export DISABLE_POSTINSTALL_SCRIPT=1\n"
            "# amd64 cross-built on an arm64 host runs under QEMU user-mode emulation, where\n"
            "# nx's native (napi-rs) binary segfaults -- its post-install and every lerna run.\n"
            "# There the install skips lifecycle scripts and runs only canvas's (its prebuilt\n"
            "# binary download); native arm64/amd64 builds install exactly as CI does.\n"
            "emulated=0\n"
            'if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then emulated=1; fi\n'
            "for attempt in 1 2 3; do\n"
            '    if [ "$emulated" = 1 ]; then\n'
            "        npm ci --ignore-scripts && npm rebuild canvas && break || true\n"
            "    else\n"
            "        npm ci && break || true\n"
            "    fi\n"
            '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            "\n"
            "# Build every package once (the run scripts rebuild after patching).\n"
            f"{_NX_ENV}"
            'if [ "$emulated" = 1 ]; then\n'
            '    echo "prepare: amd64 under QEMU emulation; skipping the warm-up lerna build (nx cannot run here; the run scripts rebuild every package before testing)"\n'
            "    # vega-util has no workspace dependencies and builds with tsc alone (no nx), so\n"
            "    # build just it: the tape gate below then runs a real test from its build/.\n"
            "    (cd packages/vega-util && npm run build)\n"
            "else\n"
            "    node_modules/.bin/lerna run build --concurrency 4\n"
            "fi\n"
            "\n"
            "# --- 3. gate ---\n"
            "# The three reporters; tape, the native canvas addon, jsdom, ajv, tsc; the\n"
            "# bundles the tests load (vega, vega-scenegraph, the JSON schema), vega-cli's bin;\n"
            "# the tape reporter on a real test file, and the scene runner on a real spec.\n"
            "test -s /home/tape-report.mjs\n"
            "test -s /home/vega-scene-compare.mjs\n"
            "test -s /home/tsc-report.mjs\n"
            'if [ "$emulated" = 1 ]; then\n'
            '    echo "gate: amd64 under QEMU emulation; nx cannot build here, so the built bundles and the scene runner are checked on native builds only"\n'
            "    node --input-type=module -e \"for (const m of ['tape', 'canvas', 'jsdom', 'ajv', "
            "'ajv-formats', 'pngjs', '@blazediff/core']) "
            "console.log(m + ' -> ' + import.meta.resolve(m))\"\n"
            "else\n"
            "    node --input-type=module -e \"for (const m of ['tape', 'canvas', 'jsdom', 'ajv', "
            "'ajv-formats', 'pngjs', '@blazediff/core', 'vega', 'vega-scenegraph']) "
            "console.log(m + ' -> ' + import.meta.resolve(m))\"\n"
            "fi\n"
            "node -e \"const { createCanvas } = require('canvas'); "
            "console.log('canvas ok', createCanvas(2, 2).toDataURL().length)\"\n"
            "node_modules/.bin/tsc --version\n"
            'if [ "$emulated" != 1 ]; then\n'
            "    test -s packages/vega/build/vega.module.js\n"
            "    test -s packages/vega/build/vega-schema.json\n"
            "    test -s packages/vega-scenegraph/build/vega-scenegraph.js\n"
            "fi\n"
            "test -x packages/vega-cli/bin/vg2svg\n"
            "(cd packages/vega-util && node /home/tape-report.mjs packages/vega-util "
            "test/array-test.js) > /tmp/tape-gate.txt\n"
            "cat /tmp/tape-gate.txt\n"
            "grep -q '^PASSED packages/vega-util/test/array-test.js > ' /tmp/tape-gate.txt\n"
            "if grep -q '^FAILED ' /tmp/tape-gate.txt; then exit 1; fi\n"
            'if [ "$emulated" != 1 ]; then\n'
            "    (cd packages/vega && TZ=America/Los_Angeles node /home/vega-scene-compare.mjs) "
            "> /tmp/scene-gate.txt\n"
            "    grep -q '^vega-scene-compare: passed=' /tmp/scene-gate.txt\n"
            "    grep -q '^PASSED .* > scene: bar$' /tmp/scene-gate.txt\n"
            "fi\n"
            "rm -f /tmp/tape-gate.txt /tmp/scene-gate.txt\n"
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
            "git clean -fdq\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fdq\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
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

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
class VEGA_4302_TO_4295(Instance):
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
        return VegaImageDefault(self.pr, self._config)

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

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
