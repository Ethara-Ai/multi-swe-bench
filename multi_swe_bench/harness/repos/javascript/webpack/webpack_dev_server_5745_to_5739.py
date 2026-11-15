import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "webpack"
_REPO = "webpack-dev-server"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"webpack_dev_server_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(
        f"{__file__}: file name must look like webpack_dev_server_<hi>_to_<lo>.py"
    )
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# engines.node ">= 22.15.0" at every base commit of the era; CI tests 22.x/24.x/26.x.
NODE_IMAGE = "node:24.22.2-bookworm"

# The browser tests drive Chrome through puppeteer. Puppeteer's own download is Chrome
# for Testing, which has no linux/arm64 build, so the shard ships Debian's chromium
# (amd64 + arm64) and the run scripts point PUPPETEER_EXECUTABLE_PATH at it.
_CHROMIUM = "/usr/bin/chromium"

# Mirrors the globs of scripts/run-tests.mjs (what `npm run test:only` runs).
_TEST_FILE_RE = re.compile(r"^test/(?:[^/]+|(?:cli|client|e2e|server)/\S+)\.test\.js$")

_SCRIPT_HEADER = (
    "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n"
    f"export PUPPETEER_SKIP_DOWNLOAD=1 PUPPETEER_EXECUTABLE_PATH={_CHROMIUM}\n\n"
)
_PREPARE_HEADER = (
    "#!/bin/bash\nset -e\n\nexport CI=true\n"
    f"export PUPPETEER_SKIP_DOWNLOAD=1 PUPPETEER_EXECUTABLE_PATH={_CHROMIUM}\n\n"
)

_NODE_TEST_REPORT_MJS = r"""const ROOT = '/home/__REPO__/';

function rel(file) {
    let f = String(file || '<unknown file>').replace(/\\/g, '/');
    if (f.startsWith('file://')) f = f.slice('file://'.length);
    return f.startsWith(ROOT) ? f.slice(ROOT.length) : f;
}

// One "PASSED|FAILED|SKIPPED <file> > <suite> > ... > <test>" line per test and suite.
// The describe/it path comes from test:start events (pre-order, per file), so it does
// not depend on the order in which node reports a suite's result relative to its
// children's results.
export default async function* nodeTestReport(source) {
    const stacks = new Map();
    const seen = new Map();
    let passed = 0, failed = 0, skipped = 0;

    for await (const event of source) {
        const data = event.data || {};
        const file = rel(data.file);
        const level = data.nesting || 0;
        if (event.type === 'test:start') {
            const stack = stacks.get(file) || [];
            stack.length = level;
            stack[level] = String(data.name);
            stacks.set(file, stack);
            continue;
        }
        if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
        const prefix = (stacks.get(file) || []).slice(0, level);
        let self = String(data.name);
        if (level === 0 && (self === String(data.file) || self === file)) self = '<file failed to run>';
        let status = event.type === 'test:fail' ? 'FAILED' : 'PASSED';
        if (status === 'PASSED' && (data.skip !== undefined || data.todo !== undefined)) status = 'SKIPPED';
        let name = [file].concat(prefix, [self]).join(' > ');
        const n = (seen.get(name) || 0) + 1;
        seen.set(name, n);
        if (n > 1) name += ' #' + n;
        if (status === 'PASSED') passed++;
        else if (status === 'FAILED') failed++;
        else skipped++;
        yield status + ' ' + name + '\n';
    }

    yield 'node-test-report: passed=' + passed + ' failed=' + failed + ' skipped=' + skipped + '\n';
}
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
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({f for f in files if _TEST_FILE_RE.match(f)})


def _post_fix_blob(pr: PullRequest, path: str) -> Optional[str]:
    """Blob id the fix patch gives `path` (from its `index <old>..<new>` line), if it
    changes that file."""
    m = re.search(
        rf"^diff --git a/{re.escape(path)} b/{re.escape(path)}\n"
        r"(?:(?!diff --git ).*\n)*?index [0-9a-f]+\.\.([0-9a-f]{7,40})\b",
        pr.fix_patch,
        re.M,
    )
    return m.group(1) if m else None


def _test_cmd(pr: PullRequest) -> str:
    files = " ".join(_test_files(pr))
    return (
        "rm -f /tmp/node-test-results.txt\n"
        "\n"
        "# A patch that rewrites package-lock.json (a dependency upgrade) needs node_modules\n"
        "# re-synced to it. This is offline: every package either lockfile names was put\n"
        "# in the npm cache by prepare.sh at build time. A no-op when the lockfile is the\n"
        "# one node_modules was installed from.\n"
        "lock_sum=$(sha256sum package-lock.json | cut -d' ' -f1)\n"
        'if [ "$lock_sum" != "$(cat node_modules/.harness-lock.sha256 2>/dev/null)" ]; then\n'
        '    echo "package-lock.json changed: re-syncing node_modules from the offline cache"\n'
        "    npm ci --ignore-scripts --offline\n"
        '    echo "$lock_sum" > node_modules/.harness-lock.sha256\n'
        "fi\n"
        "\n"
        "# CI links the package into its own node_modules (`npm link webpack-dev-server`) so\n"
        "# the CLI tests' `webpack serve` can resolve it; the same link, offline.\n"
        f"ln -sfn /home/{pr.repo} node_modules/{pr.repo}\n"
        "\n"
        "# The tests load client/ (babel + webpack build of client-src/) and dist/ (cjs build\n"
        "# of lib/), so rebuild them from the (possibly patched) sources.\n"
        "npm run build:client\n"
        "npm run build:cjs\n"
        "\n"
        "# Only the test files the test patch touches; a file the patch adds does not\n"
        "# exist before it is applied, so each stage runs the ones present at that point.\n"
        "# Same node flags as scripts/run-tests.mjs.\n"
        "files=()\n"
        f"for f in {files}; do\n"
        '    if [ -f "$f" ]; then files+=("$f"); fi\n'
        "done\n"
        'if [ "${#files[@]}" -eq 0 ]; then\n'
        '    echo "no test file of the test patch exists at this stage"\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        "# Result lines go to stdout (synchronous on a pipe): a file destination is an async\n"
        "# stream that --test-force-exit can cut short, silently dropping the last results.\n"
        "rc=0\n"
        "node --import ./scripts/node-test-setup.mjs --experimental-test-module-mocks --test "
        "--test-timeout=400000 --test-force-exit "
        "--test-reporter=/home/node-test-report.mjs --test-reporter-destination=stdout "
        "--test-reporter=spec --test-reporter-destination=stderr "
        '"${files[@]}" | tee /tmp/node-test-results.txt || rc=$?\n'
        "if ! grep -q '^node-test-report: ' /tmp/node-test-results.txt; then\n"
        '    echo "node --test exited with code $rc without a complete result report" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
    )


class WebpackDevServerImageBase(Image):
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

# Shard-wide browser for the puppeteer-driven tests: Debian's chromium (amd64 + arm64).
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates chromium \\
    && rm -rf /var/lib/apt/lists/* \\
    && if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then \\
           test -x /usr/bin/chromium && echo "chromium installed (amd64 under QEMU emulation: the emulated CPU lacks SSE3, so --version cannot run)"; \\
       else chromium --version; fi

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class WebpackDevServerImageDefault(Image):
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
        return WebpackDevServerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _warm_fix_lockfile_cache(self) -> str:
        lock_blob = _post_fix_blob(self.pr, "package-lock.json")
        if lock_blob is None:
            return ""
        pkg_blob = _post_fix_blob(self.pr, "package.json")
        pkg_src = f"git cat-file blob {pkg_blob}" if pkg_blob else "git show HEAD:package.json"
        return (
            "\n"
            "# This PR's fix is a dependency upgrade: it rewrites package-lock.json, so its\n"
            "# tests can only pass once node_modules matches the new lockfile, and the run\n"
            "# scripts re-sync it offline. Fill the npm cache with that lockfile's packages:\n"
            "# its blob (named on the fix patch's `index` line) is in the full clone. This is\n"
            "# done in a scratch dir; only the npm cache changes, the tree stays at the base.\n"
            "warm_dir=$(mktemp -d)\n"
            f'git cat-file blob {lock_blob} > "$warm_dir/package-lock.json"\n'
            f'{pkg_src} > "$warm_dir/package.json"\n'
            "for attempt in 1 2 3; do\n"
            '    (cd "$warm_dir" && npm ci --ignore-scripts) && break\n'
            '    echo "prepare: cache warm-up attempt $attempt failed; retrying in 15s"\n'
            '    if [ "$attempt" -eq 3 ]; then exit 1; fi\n'
            "    sleep 15\n"
            "done\n"
            'rm -rf "$warm_dir"\n'
        )

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        node_test_report = _NODE_TEST_REPORT_MJS.replace("__REPO__", repo)
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
            "# node:test reporter the run scripts use; written outside the repo.\n"
            "cat > /home/node-test-report.mjs <<'__NODE_TEST_REPORT_EOF__'\n"
            f"{node_test_report}"
            "__NODE_TEST_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# CI: npm ci, then npm run build (client/, dist/, types/).\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            "for attempt in 1 2 3; do\n"
            "    npm ci --ignore-scripts && break\n"
            '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
            '    if [ "$attempt" -eq 3 ]; then exit 1; fi\n'
            "    sleep 15\n"
            "done\n"
            "sha256sum package-lock.json | cut -d' ' -f1 > node_modules/.harness-lock.sha256\n"
            "# CI's `npm link webpack-dev-server`: the CLI tests' `webpack serve` resolves the\n"
            "# package from node_modules. Same link, without the global round trip.\n"
            f"ln -sfn /home/{repo} node_modules/{repo}\n"
            f"{self._warm_fix_lockfile_cache()}"
            "\n"
            "npm run build\n"
            "\n"
            "# --- 3. gate ---\n"
            "# The reporter exists, the built client and cjs entry load, the test runner's\n"
            "# setup module and the libraries the touched tests import resolve, and puppeteer\n"
            "# drives the distro chromium.\n"
            "test -s /home/node-test-report.mjs\n"
            "test -s client/index.js\n"
            "test -s dist/Server.js\n"
            "node -e \"require('./dist/Server.js'); console.log('dist/Server.js loads')\"\n"
            "test -s scripts/node-test-setup.mjs\n"
            f"test -L node_modules/{repo}\n"
            "test -x node_modules/.bin/webpack\n"
            "node -e \"console.log('webpack-cli resolves webpack-dev-server -> ' + "
            "require.resolve('webpack-dev-server/package.json', { paths: [require.resolve('webpack-cli')] }))\"\n"
            "node --input-type=module -e \"for (const m of ['webpack', 'puppeteer', 'expect', "
            "'jsdom', 'execa']) console.log(m + ' -> ' + import.meta.resolve(m))\"\n"
            "# Chromium refuses to start under QEMU user-mode emulation (the emulated CPU lacks\n"
            "# SSE3), so an amd64 image cross-built on an arm64 host cannot drive it at build\n"
            "# time. Detect that case (x86_64 userland over an ARM host CPU) and check the\n"
            "# chromium binary instead; native arm64/amd64 builds run the puppeteer check.\n"
            'if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then\n'
            '    echo "gate: amd64 under QEMU emulation; skipping the puppeteer chromium check"\n'
            f"    test -x {_CHROMIUM}\n"
            "else\n"
            "    node --input-type=module -e \"const { launch } = await import('puppeteer'); "
            "const b = await launch({ headless: 'new', args: ['--no-sandbox', "
            "'--disable-dev-shm-usage'] }); const p = await b.newPage(); "
            "await p.setContent('<p>gate</p>'); "
            "if ((await p.\\$eval('p', (e) => e.textContent)) !== 'gate') process.exit(1); "
            "console.log('puppeteer -> ' + (await b.version())); await b.close();\"\n"
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
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fd\n"
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
class WEBPACK_DEV_SERVER_5745_TO_5739(Instance):
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
        return WebpackDevServerImageDefault(self.pr, self._config)

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

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) (test/\S+\.test\.js(?: > .+)?)$")

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
