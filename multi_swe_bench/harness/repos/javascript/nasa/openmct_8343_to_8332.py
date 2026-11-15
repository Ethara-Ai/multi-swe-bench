import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "nasa"
_REPO = "openmct"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"openmct_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like openmct_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# package.json engines.node ">=24.14.1" and .nvmrc 24.14.1 at both base commits.
NODE_IMAGE = "node:24.22.2-bookworm"

_SPEC_RE = re.compile(r"^e2e/tests/\S+\.e2e\.spec\.js$")

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

_PW_HARNESS_CONFIG = r"""// Harness wrapper around e2e/playwright-ci.config.js (what `npm run test:e2e:ci` uses):
// same webServer (npm run start:coverage), timeouts, retries and fixtures, restricted
// to the `chrome` project, one worker, no max-failures cut-off (every listed spec runs
// to the end) and a JSON report written outside the repo for /home/pw-report.cjs.
const ROOT = '/home/__REPO__/';
const { default: ci } = await import(ROOT + 'e2e/playwright-ci.config.js');

const chrome = ci.projects.filter((project) => project.name === 'chrome');
if (chrome.length !== 1) {
  throw new Error('playwright.harness: expected one `chrome` project in playwright-ci.config.js');
}

export default {
  ...ci,
  testDir: ROOT + 'e2e/tests',
  outputDir: '/tmp/pw-test-results',
  maxFailures: 0,
  workers: 1,
  projects: chrome,
  reporter: [['list'], ['json', { outputFile: process.env.PW_JSON_REPORT || '/tmp/pw-report.json' }]]
};
"""

_PW_REPORT_CJS = r"""// Turns a Playwright JSON report into one line per test:
//   PASSED|FAILED|SKIPPED <file relative to the repo> > <describe> > ... > <title>
// "flaky" (failed, then passed on one of the config's retries) counts as PASSED, as in CI.
const fs = require('fs');
const path = require('path');

const reportPath = process.argv[2];
const ROOT = '/home/__REPO__';

if (!reportPath || !fs.existsSync(reportPath)) {
  console.log('pw-report: no JSON report at ' + reportPath + ' -- playwright produced no output');
  process.exit(0);
}

let report;
try {
  report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
} catch (e) {
  console.log('pw-report: could not parse ' + reportPath + ': ' + e.message);
  process.exit(0);
}

const rootDir = (report.config && report.config.rootDir) || ROOT;
const STATUS = { expected: 'PASSED', flaky: 'PASSED', unexpected: 'FAILED', skipped: 'SKIPPED' };
const seen = new Map();
const counts = { PASSED: 0, FAILED: 0, SKIPPED: 0 };

function rel(file) {
  return path.relative(ROOT, path.resolve(rootDir, file)).split(path.sep).join('/');
}

function emit(status, parts) {
  let name = parts.filter(Boolean).join(' > ');
  const n = (seen.get(name) || 0) + 1;
  seen.set(name, n);
  if (n > 1) name += ' #' + n;
  counts[status]++;
  console.log(status + ' ' + name);
}

function walk(suite, file, titles) {
  const here = file || suite.file;
  // A file-level suite is titled with the file name itself; describe titles follow.
  const isFileSuite = !file && suite.file && suite.title === suite.file;
  const next = suite.title && !isFileSuite ? titles.concat([suite.title]) : titles;
  for (const spec of suite.specs || []) {
    for (const test of spec.tests || []) {
      emit(STATUS[test.status] || 'FAILED', [rel(spec.file || here)].concat(next, [spec.title]));
    }
  }
  for (const child of suite.suites || []) walk(child, here, next);
}

for (const suite of report.suites || []) walk(suite, '', []);

for (const err of report.errors || []) {
  const loc = err.location ? ' in ' + rel(err.location.file) : '';
  console.log('pw-report: top-level error' + loc + ': ' + String(err.message || '').split('\n')[0]);
}

console.log('pw-report: passed=' + counts.PASSED + ' failed=' + counts.FAILED + ' skipped=' + counts.SKIPPED);
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


def _spec_files(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({f for f in files if _SPEC_RE.match(f)})


def _test_cmd(pr: PullRequest) -> str:
    specs = " ".join(_spec_files(pr))
    return (
        "rm -rf /tmp/pw-report.json /tmp/pw-test-results\n"
        "\n"
        "# Only the e2e spec files the test patch touches; a spec the patch adds does not\n"
        "# exist before it is applied, so each stage runs the ones present at that point.\n"
        "specs=()\n"
        f"for f in {specs}; do\n"
        '    if [ -f "$f" ]; then specs+=("$f"); fi\n'
        "done\n"
        'if [ "${#specs[@]}" -eq 0 ]; then\n'
        '    echo "no e2e spec of the test patch exists at this stage"\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        "# Playwright starts the app itself (webServer: npm run start:coverage on :8080).\n"
        "rc=0\n"
        "node_modules/.bin/playwright test --config /home/playwright.harness.config.mjs "
        '"${specs[@]}" || rc=$?\n'
        "if [ ! -s /tmp/pw-report.json ]; then\n"
        '    echo "playwright exited with code $rc without writing /tmp/pw-report.json" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
        "node /home/pw-report.cjs /tmp/pw-report.json\n"
    )


class OpenmctImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class OpenmctImageDefault(Image):
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
        return OpenmctImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        harness_config = _PW_HARNESS_CONFIG.replace("__REPO__", repo)
        pw_report = _PW_REPORT_CJS.replace("__REPO__", repo)
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
            "# Playwright config wrapper and JSON-report converter the run scripts use;\n"
            "# written outside the repo.\n"
            "cat > /home/playwright.harness.config.mjs <<'__PW_HARNESS_EOF__'\n"
            f"{harness_config}"
            "__PW_HARNESS_EOF__\n"
            "cat > /home/pw-report.cjs <<'__PW_REPORT_EOF__'\n"
            f"{pw_report}"
            "__PW_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# The lockfile install CI does (root + the e2e workspace).\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            "for attempt in 1 2 3; do\n"
            "    npm ci && break\n"
            '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
            '    if [ "$attempt" -eq 3 ]; then exit 1; fi\n'
            "    sleep 15\n"
            "done\n"
            "\n"
            "# The chromium build (and its system libraries) that this commit's pinned\n"
            "# @playwright/test expects. Playwright publishes it for linux amd64 and arm64.\n"
            "for attempt in 1 2 3; do\n"
            "    node_modules/.bin/playwright install --with-deps chromium && break\n"
            '    echo "prepare: playwright install attempt $attempt failed; retrying in 15s"\n'
            '    if [ "$attempt" -eq 3 ]; then exit 1; fi\n'
            "    sleep 15\n"
            "done\n"
            "rm -rf /var/lib/apt/lists/*\n"
            "\n"
            "# --- 3. gate ---\n"
            "# Both helpers exist, the playwright CLI runs, the harness config loads and lists\n"
            "# the chrome project's tests, the webpack dev-server entry the webServer command\n"
            "# uses resolves, and the installed chromium actually launches headless.\n"
            "test -s /home/playwright.harness.config.mjs\n"
            "test -s /home/pw-report.cjs\n"
            "test -x node_modules/.bin/playwright\n"
            "node_modules/.bin/playwright --version\n"
            "test -x node_modules/.bin/webpack\n"
            "node -e \"require.resolve('webpack-dev-server'); require.resolve('@playwright/test'); "
            "console.log('webpack-dev-server + @playwright/test resolve')\"\n"
            "list_count=$(node_modules/.bin/playwright test --config "
            "/home/playwright.harness.config.mjs --list | grep -c '\\[chrome\\]')\n"
            'echo "playwright --list: $list_count chrome tests"\n'
            'test "$list_count" -gt 0\n'
            "# Chromium renders pages through its own JIT/zygote machinery, which crashes\n"
            "# under QEMU user-mode emulation, so an amd64 image cross-built on an arm64 host\n"
            "# cannot open a page at build time. Detect that case (x86_64 userland over an\n"
            "# ARM host CPU) and check the installed chromium binary instead; native\n"
            "# arm64/amd64 builds launch it.\n"
            'if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then\n'
            '    echo "gate: amd64 under QEMU emulation; skipping chromium launch"\n'
            "    node -e \"const p = require('@playwright/test').chromium.executablePath(); "
            "require('fs').accessSync(p, require('fs').constants.X_OK); "
            "console.log('chromium binary: ' + p)\"\n"
            "else\n"
            "    node -e \"const { chromium } = require('@playwright/test'); "
            "(async () => { const b = await chromium.launch(); const p = await b.newPage(); "
            "await p.setContent('<p>gate</p>'); if ((await p.textContent('p')) !== 'gate') "
            "process.exit(1); console.log('chromium ' + b.version() + ' OK'); await b.close(); })()"
            ".catch((e) => { console.error(e); process.exit(1); })\"\n"
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


@Instance.register(_ORG, _ERA_FILE)
class OPENMCT_8343_TO_8332(Instance):
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
        return OpenmctImageDefault(self.pr, self._config)

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

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) (e2e/\S+ > .+)$")

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
