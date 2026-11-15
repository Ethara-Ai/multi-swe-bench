import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "GoogleChrome"
_REPO = "web-vitals"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"web_vitals_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like web_vitals_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# No engines field; .nvmrc says lts/Hydrogen (18) but WebdriverIO 9.29 and CI's
# ubuntu-latest default run on a current LTS. 22 satisfies both.
NODE_IMAGE = "node:22.23.4-bookworm"

# The e2e suite is WebdriverIO + mocha + Chrome. Chrome for Testing (what WDIO would
# download) has no linux/arm64 build, so the shard ships Debian's chromium and the
# matching chromium-driver, which exist for both amd64 and arm64.
_BROWSER_PACKAGES = "chromium chromium-driver"

_SPEC_RE = re.compile(r"^test/e2e/[^/]+-test\.js$")

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

_WDIO_HARNESS_CONF = r"""// Harness wrapper around the repo's own wdio.conf.js: same framework, timeouts and
// Chrome options, but only the Chrome capability, driven headless through Debian's
// chromium + chromedriver (no browser download at test time), only the spec files in
// WDIO_HARNESS_SPECS, and a reporter that appends one
// "PASSED|FAILED|SKIPPED <file> > <describe...> > <title>" line per test.
import fs from 'node:fs';

const ROOT = '/home/__REPO__/';
const RESULTS = process.env.WDIO_HARNESS_RESULTS || '/tmp/wdio-results.txt';

const {config: repoConfig} = await import(ROOT + 'wdio.conf.js');
const {default: WDIOReporter} = await import(
  ROOT + 'node_modules/@wdio/reporter/build/index.js'
);

function rel(file) {
  let f = String(file || '<unknown spec>');
  if (f.startsWith('file://')) f = f.slice('file://'.length);
  return f.startsWith(ROOT) ? f.slice(ROOT.length) : f;
}

class HarnessReporter extends WDIOReporter {
  constructor(options) {
    super({...options, stdout: true});
    this.file = '<unknown spec>';
    this.suites = [];
    this.seen = new Map();
  }

  onRunnerStart(runner) {
    this.file = rel((runner.specs || [])[0]);
  }

  onSuiteStart(suite) {
    this.suites.push(suite.title);
  }

  onSuiteEnd() {
    this.suites.pop();
  }

  record(status, title) {
    let name = [this.file, ...this.suites.filter(Boolean), title].join(' > ');
    const n = (this.seen.get(name) || 0) + 1;
    this.seen.set(name, n);
    if (n > 1) name += ' #' + n;
    fs.appendFileSync(RESULTS, status + ' ' + name + '\n');
  }

  onTestPass(test) {
    this.record('PASSED', test.title);
  }

  onTestFail(test) {
    this.record('FAILED', test.title);
  }

  onTestSkip(test) {
    this.record('SKIPPED', test.title);
  }

  onHookEnd(hook) {
    if (hook.error) this.record('FAILED', '<hook> ' + hook.title);
  }
}

const capabilities = repoConfig.capabilities
  .filter((cap) => cap.browserName === 'chrome')
  .map((cap) => {
    const chromeOptions = cap['goog:chromeOptions'] || {};
    return {
      ...cap,
      'goog:chromeOptions': {
        ...chromeOptions,
        binary: '/usr/bin/chromium',
        args: [
          ...(chromeOptions.args || []),
          '--headless=new',
          '--no-sandbox',
          '--disable-dev-shm-usage',
          '--window-size=1280,800',
        ],
      },
      'wdio:chromedriverOptions': {binary: '/usr/bin/chromedriver'},
    };
  });

if (capabilities.length !== 1) {
  throw new Error('wdio.harness: expected exactly one chrome capability in wdio.conf.js');
}

const specs = (process.env.WDIO_HARNESS_SPECS || '')
  .split(/\s+/)
  .filter(Boolean)
  .map((spec) => ROOT + spec);

if (specs.length === 0) {
  throw new Error('wdio.harness: WDIO_HARNESS_SPECS is empty');
}

export const config = {
  ...repoConfig,
  specs,
  exclude: [],
  capabilities,
  maxInstances: 1,
  reporters: ['spec', HarnessReporter],
};
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
        "rm -f /tmp/wdio-results.txt /tmp/web-vitals-server.log\n"
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
        "# The e2e pages load dist/, so build the (possibly patched) sources first.\n"
        "npm run build\n"
        "\n"
        "# The suite drives pages served by the repo's own test server on :9090.\n"
        "node test/server.js > /tmp/web-vitals-server.log 2>&1 &\n"
        "server_pid=$!\n"
        "trap 'if kill -0 \"$server_pid\" 2>/dev/null; then kill \"$server_pid\"; fi' EXIT\n"
        "for i in $(seq 1 60); do\n"
        "    if curl -s -o /dev/null http://localhost:9090/; then break; fi\n"
        '    if ! kill -0 "$server_pid" 2>/dev/null; then cat /tmp/web-vitals-server.log >&2; exit 1; fi\n'
        "    sleep 1\n"
        "done\n"
        "curl -s -o /dev/null http://localhost:9090/\n"
        "\n"
        "rc=0\n"
        'WDIO_HARNESS_SPECS="${specs[*]}" node_modules/.bin/wdio run /home/wdio.harness.conf.mjs || rc=$?\n'
        "if [ ! -s /tmp/wdio-results.txt ]; then\n"
        '    echo "wdio exited with code $rc without reporting any test" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
        "cat /tmp/wdio-results.txt\n"
    )


class WebVitalsImageBase(Image):
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

# Shard-wide browser for the WebdriverIO e2e suite: Debian's chromium and its
# matching chromedriver (both published for amd64 and arm64).
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl {_BROWSER_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/* \\
    && if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then \\
           test -x /usr/bin/chromium && echo "chromium installed (amd64 under QEMU emulation: the emulated CPU lacks SSE3, so --version cannot run)"; \\
       else chromium --version; fi \\
    && chromedriver --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class WebVitalsImageDefault(Image):
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
        return WebVitalsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        harness_conf = _WDIO_HARNESS_CONF.replace("__REPO__", repo)
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
            "# WebdriverIO config wrapper the run scripts use; written outside the repo.\n"
            "cat > /home/wdio.harness.conf.mjs <<'__WDIO_HARNESS_EOF__'\n"
            f"{harness_conf}"
            "__WDIO_HARNESS_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# The lockfile install CI does (`npm install` there; `npm ci` keeps it exact).\n"
            "# Chrome for Testing is never downloaded: the harness points WDIO at the distro\n"
            "# chromium + chromedriver installed in the base image.\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            "for attempt in 1 2 3; do\n"
            "    npm ci && break\n"
            '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
            '    if [ "$attempt" -eq 3 ]; then exit 1; fi\n'
            "    sleep 15\n"
            "done\n"
            "\n"
            "# --- 3. gate ---\n"
            "# The library builds (tsc + rollup, what the e2e pages load), the wdio CLI and\n"
            "# reporter base class resolve, the harness config loads and yields exactly one\n"
            "# chrome capability, and headless chromium + chromedriver start.\n"
            "test -s /home/wdio.harness.conf.mjs\n"
            "test -x node_modules/.bin/wdio\n"
            "test -s node_modules/@wdio/reporter/build/index.js\n"
            "node_modules/.bin/wdio --version\n"
            "npm run build\n"
            "test -s dist/web-vitals.js\n"
            "WDIO_HARNESS_SPECS=test/e2e/onTTFB-test.js node --input-type=module -e "
            "\"const {config} = await import('/home/wdio.harness.conf.mjs'); "
            "if (config.capabilities.length !== 1 || config.specs.length !== 1) process.exit(1); "
            "console.log('wdio harness config OK: ' + config.capabilities[0].browserName)\"\n"
            "# Chromium refuses to start under QEMU user-mode emulation (the emulated CPU lacks\n"
            "# SSE3), so an amd64 image cross-built on an arm64 host cannot run it at build\n"
            "# time. Detect that case (x86_64 userland over an ARM host CPU) and check the\n"
            "# chromium binary instead; native arm64/amd64 builds run the headless check.\n"
            'if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then\n'
            '    echo "gate: amd64 under QEMU emulation; skipping the headless chromium check"\n'
            "    test -x /usr/bin/chromium\n"
            "else\n"
            "    chromium --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage "
            "--dump-dom 'data:text/html,<p>gate</p>' | grep -q '<p>gate</p>'\n"
            "fi\n"
            "chromedriver --version\n"
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
class WEB_VITALS_776_TO_308(Instance):
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
        return WebVitalsImageDefault(self.pr, self._config)

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

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) (test/\S+ > .+)$")

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
