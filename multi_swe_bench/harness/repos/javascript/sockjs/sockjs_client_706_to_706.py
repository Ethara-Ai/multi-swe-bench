import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "sockjs"
_REPO = "sockjs-client"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"sockjs_client_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like sockjs_client_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# .github/workflows/node.js.yml tests on node 18.x / 20.x / 22.x and package.json asks
# for node >=12; 22 is the newest line CI covers and the only one of the three still
# maintained when this PR was opened (Sep 2026).
NODE_IMAGE = "node:22.23.4-bookworm"

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

# Mocha reporter: one `PASSED|FAILED|SKIPPED <file> > <suite...> > <title>` line per
# test (and per failing hook), appended synchronously to $MOCHA_REPORT_FILE so the
# lines never interleave with what the tests print and survive `--exit`.
_MOCHA_REPORT_JS = r"""'use strict';
const fs = require('fs');

const ROOT = '/home/__REPO__/';
const OUT = process.env.MOCHA_REPORT_FILE;

function rel(file) {
    let f = String(file || '<unknown file>').replace(/\\/g, '/');
    return f.startsWith(ROOT) ? f.slice(ROOT.length) : f;
}

function MochaReport(runner) {
    if (!OUT) throw new Error('mocha-report: MOCHA_REPORT_FILE is not set');
    fs.writeFileSync(OUT, '');
    const seen = new Map();
    let passed = 0, failed = 0, skipped = 0;

    function emit(status, test) {
        let name = [rel(test.file)].concat(test.titlePath()).join(' > ');
        const key = status + '\u0000' + name;
        const n = (seen.get(key) || 0) + 1;
        seen.set(key, n);
        if (n > 1) name += ' #' + n;
        if (status === 'PASSED') passed++;
        else if (status === 'FAILED') failed++;
        else skipped++;
        fs.appendFileSync(OUT, status + ' ' + name + '\n');
    }

    runner.on('pass', test => emit('PASSED', test));
    runner.on('pending', test => emit('SKIPPED', test));
    runner.on('fail', (test, err) => {
        emit('FAILED', test);
        const msg = String((err && err.message) || err).split('\n')[0];
        fs.appendFileSync(OUT, '    mocha-report: ' + msg + '\n');
    });
    runner.once('end', () => {
        fs.appendFileSync(OUT, 'mocha-report: passed=' + passed + ' failed=' + failed +
                               ' skipped=' + skipped + '\n');
    });
}

module.exports = MochaReport;
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


def _install_block() -> str:
    return (
        "for attempt in 1 2 3; do\n"
        "    npm ci && break || true\n"
        '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
    )


def _test_cmd(pr: PullRequest) -> str:
    # tests/node.js is the node suite's entry point (package.json "test": "mocha
    # tests/node.js"): it starts the local SockJS echo server the end-to-end tests talk
    # to and requires every tests/lib/* module. The test patch registers its new module
    # there, so this one file is what the patch is about in every stage.
    return (
        "rm -f /tmp/mocha-results.txt\n"
        "\n"
        "rc=0\n"
        "MOCHA_REPORT_FILE=/tmp/mocha-results.txt node_modules/.bin/mocha --exit "
        "--reporter /home/mocha-report.js tests/node.js || rc=$?\n"
        "if [ ! -s /tmp/mocha-results.txt ]; then\n"
        '    echo "mocha exited with code $rc without writing /tmp/mocha-results.txt" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
        "cat /tmp/mocha-results.txt\n"
    )


class SockjsClientImageBase(Image):
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


class SockjsClientImageDefault(Image):
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
        return SockjsClientImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        mocha_report = _MOCHA_REPORT_JS.replace("__REPO__", repo)

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
            "# Result reporter the run scripts use; written outside the repo.\n"
            "cat > /home/mocha-report.js <<'__MOCHA_REPORT_EOF__'\n"
            f"{mocha_report}"
            "__MOCHA_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# The install CI runs (.github/workflows/node.js.yml: npm ci), from the lockfile\n"
            "# that ships with this commit.\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            f"{_install_block()}"
            "\n"
            "# --- 3. gate ---\n"
            "# The reporter, mocha, the runtime deps lib/ loads, the test-only deps\n"
            "# (the sockjs echo server, expect.js, proxyquire), the suite entry point, and\n"
            "# the reporter producing result lines under mocha.\n"
            "test -s /home/mocha-report.js\n"
            "test -x node_modules/.bin/mocha\n"
            "node_modules/.bin/mocha --version\n"
            "node -e \"for (const m of ['faye-websocket', 'eventsource', 'url-parse', 'debug', "
            "'inherits', 'sockjs', 'expect.js', 'proxyquire', 'serve-static']) "
            "console.log(m + ' -> ' + require.resolve(m))\"\n"
            "node -e \"require('./lib/entry')\"\n"
            "test -f tests/node.js\n"
            "mkdir -p /tmp/mocha-gate\n"
            "printf \"describe('g', function () { it('gate', function () {}); });\\n\" "
            "> /tmp/mocha-gate/gate.js\n"
            "MOCHA_REPORT_FILE=/tmp/mocha-gate/out.txt node_modules/.bin/mocha "
            "--reporter /home/mocha-report.js /tmp/mocha-gate/gate.js\n"
            "cat /tmp/mocha-gate/out.txt\n"
            "grep -qx 'PASSED /tmp/mocha-gate/gate.js > g > gate' /tmp/mocha-gate/out.txt\n"
            "rm -rf /tmp/mocha-gate\n"
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
class SOCKJS_CLIENT_706_TO_706(Instance):
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
        return SockjsClientImageDefault(self.pr, self._config)

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
