import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "usebruno"
_REPO = "bruno"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"bruno_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like bruno_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# .nvmrc v22.12.0; CI (.github/actions/common/setup-node-deps) uses v22.17.0.
NODE_IMAGE = "node:22.23.4-bookworm"

# What CI's e2e job apt-installs to run the Electron app under Xvfb
# (.github/workflows/tests-linux.yml), with bookworm's package names.
_ELECTRON_PACKAGES = (
    "libglib2.0-0 libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libgtk-3-0 "
    "libasound2 libgbm1 libxss1 libxtst6 xvfb xauth gsettings-desktop-schemas dbus-x11"
)

# Libraries CI builds before the e2e run (setup-node-deps "Build libraries" step).
_BUILD_LIBRARIES = (
    "npm run build:graphql-docs\n"
    "npm run build:bruno-query\n"
    "npm run build:bruno-common\n"
    "npm run sandbox:bundle-libraries --workspace=packages/bruno-js\n"
    "npm run build:bruno-converters\n"
    "npm run build:bruno-requests\n"
    "npm run build:schema-types\n"
    "npm run build:bruno-filestore\n"
    "npm run build:bruno-sqlite\n"
)

_SPEC_RE = re.compile(r"^tests/\S+\.spec\.ts$")

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

# The container runs as root, and Chromium (inside Electron) refuses to start as root
# without --no-sandbox. Playwright's _electron.launch resolves the binary through the
# `electron` package, which honours ELECTRON_OVERRIDE_DIST_PATH; this wrapper is what
# that path points at.
_ELECTRON_WRAPPER_DIR = "/usr/local/lib/electron-harness"

_ELECTRON_WRAPPER = """#!/bin/sh
exec /home/__REPO__/node_modules/electron/dist/electron --no-sandbox "$@"
"""

_PW_HARNESS_CONFIG = r"""// Harness wrapper around the repo's playwright.config.ts (what `npm run test:e2e` uses):
// same web servers (bruno-app dev server + bruno-tests server), retries and fixtures,
// restricted to the `default` project, one worker, no max-failures cut-off, and a JSON
// report written outside the repo for /home/pw-report.cjs.
import repo from '/home/__REPO__/playwright.config';

const ROOT = '/home/__REPO__/';

const project = (repo.projects || []).filter((p) => p.name === 'default');
if (project.length !== 1) {
  throw new Error('playwright.harness: expected one `default` project in playwright.config.ts');
}

const servers = Array.isArray(repo.webServer) ? repo.webServer : [repo.webServer];

export default {
  ...repo,
  testDir: ROOT + 'tests',
  outputDir: '/tmp/pw-test-results',
  workers: 1,
  maxFailures: 0,
  projects: project.map((p) => ({ ...p, testDir: ROOT + 'tests' })),
  webServer: servers.map((server) => ({ ...server, cwd: ROOT })),
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
        "# Playwright starts both web servers itself and launches the Electron app, which\n"
        "# needs a display and a session bus: the same xvfb-run + dbus-run-session wrapper\n"
        "# CI uses (.github/actions/tests/run-e2e-tests).\n"
        f"export ELECTRON_OVERRIDE_DIST_PATH={_ELECTRON_WRAPPER_DIR}\n"
        "rc=0\n"
        "xvfb-run -a dbus-run-session -- node_modules/.bin/playwright test "
        '--config /home/playwright.harness.config.ts "${specs[@]}" || rc=$?\n'
        "if [ ! -s /tmp/pw-report.json ]; then\n"
        '    echo "playwright exited with code $rc without writing /tmp/pw-report.json" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
        "node /home/pw-report.cjs /tmp/pw-report.json\n"
    )


class BrunoImageBase(Image):
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

# Shard-wide runtime for the Electron e2e suite: the GTK/NSS/ALSA libraries Electron
# links against, plus Xvfb and a D-Bus session (CI's e2e job installs the same set).
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates {_ELECTRON_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class BrunoImageDefault(Image):
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
        return BrunoImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        harness_config = _PW_HARNESS_CONFIG.replace("__REPO__", repo)
        pw_report = _PW_REPORT_CJS.replace("__REPO__", repo)
        wrapper = _ELECTRON_WRAPPER.replace("__REPO__", repo)
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
            "# Playwright config wrapper, JSON-report converter and the --no-sandbox Electron\n"
            "# launcher the run scripts use; all written outside the repo.\n"
            "cat > /home/playwright.harness.config.ts <<'__PW_HARNESS_EOF__'\n"
            f"{harness_config}"
            "__PW_HARNESS_EOF__\n"
            "cat > /home/pw-report.cjs <<'__PW_REPORT_EOF__'\n"
            f"{pw_report}"
            "__PW_REPORT_EOF__\n"
            f"mkdir -p {_ELECTRON_WRAPPER_DIR}\n"
            f"cat > {_ELECTRON_WRAPPER_DIR}/electron <<'__ELECTRON_EOF__'\n"
            f"{wrapper}"
            "__ELECTRON_EOF__\n"
            f"chmod +x {_ELECTRON_WRAPPER_DIR}/electron\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# CI's install (setup-node-deps: npm ci --legacy-peer-deps; the electron package\n"
            "# downloads the Electron build for this architecture), the libraries it builds\n"
            "# before the e2e run, and the bruno-tests collection deps (run-e2e-tests).\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            "for attempt in 1 2 3; do\n"
            "    npm ci --legacy-peer-deps && break\n"
            '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
            '    if [ "$attempt" -eq 3 ]; then exit 1; fi\n'
            "    sleep 15\n"
            "done\n"
            f"{_BUILD_LIBRARIES}"
            "for attempt in 1 2 3; do\n"
            "    npm ci --prefix packages/bruno-tests/collection && break\n"
            '    echo "prepare: collection npm ci attempt $attempt failed; retrying in 15s"\n'
            '    if [ "$attempt" -eq 3 ]; then exit 1; fi\n'
            "    sleep 15\n"
            "done\n"
            "\n"
            "# --- 3. gate ---\n"
            "# Every helper exists, the Electron binary for this architecture runs through the\n"
            "# wrapper, the built libraries the app imports are present, Xvfb + D-Bus wrappers\n"
            "# exist, and the harness config loads and lists the default project's tests.\n"
            "test -s /home/playwright.harness.config.ts\n"
            "test -s /home/pw-report.cjs\n"
            f"test -x {_ELECTRON_WRAPPER_DIR}/electron\n"
            "test -x node_modules/.bin/playwright\n"
            "node_modules/.bin/playwright --version\n"
            f"ELECTRON_OVERRIDE_DIST_PATH={_ELECTRON_WRAPPER_DIR} node -e "
            "\"const p = require('electron'); require('fs').accessSync(p, require('fs').constants.X_OK); "
            "console.log('electron -> ' + p)\"\n"
            "ELECTRON_RUN_AS_NODE=1 node_modules/electron/dist/electron -e "
            "\"console.log('electron ' + process.versions.electron + ' runs')\"\n"
            "for pkg in bruno-common bruno-query bruno-converters bruno-requests bruno-filestore "
            "bruno-graphql-docs bruno-schema-types bruno-sqlite; do\n"
            '    test -d "packages/$pkg/dist" || { echo "missing packages/$pkg/dist"; exit 1; }\n'
            "done\n"
            "test -d packages/bruno-tests/collection/node_modules\n"
            "command -v xvfb-run\n"
            "command -v dbus-run-session\n"
            "list_count=$(node_modules/.bin/playwright test --config "
            "/home/playwright.harness.config.ts --list | grep -c '\\[default\\]')\n"
            'echo "playwright --list: $list_count default-project tests"\n'
            'test "$list_count" -gt 0\n'
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
class BRUNO_8617_TO_8617(Instance):
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
        return BrunoImageDefault(self.pr, self._config)

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

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) (tests/\S+ > .+)$")

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
