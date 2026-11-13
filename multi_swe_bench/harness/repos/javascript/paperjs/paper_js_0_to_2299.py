"""paperjs/paper.js repo config -- Yarn 3 + gulp + QUnit era.

Era: base commits that still run the tests with gulp-qunits (QUnit 1.23 in Node,
on jsdom + node-canvas) and install with the committed Yarn 3.2.4 release
(.yarn/releases/yarn-3.2.4.cjs, nodeLinker: node-modules, yarn.lock).
Test files are test/tests/<Name>.js, included from test/tests/load.js.
The switch to pnpm (e923fb183) and to Vitest (57db7ff26, *.test.js files) happened
in October 2026; base commits after that use paper_js_2300_to_99999.

The interval name is nominal: paper.js merges old PR numbers onto new develop
commits, so the era is decided by the base commit's toolchain, and each record's
`number_interval` field names the era file explicitly. PR in this era: #2156.

Layout (per-PR base):
  * ImageBase   -- node:24 (the version CI uses) + fonts-liberation (the text
                   bounds tests expect Arial metrics) + node-canvas build deps
                   (fallback if no prebuilt binary is found). Clones paper.js,
                   fetches BASE_COMMIT by SHA when the clone has no ref to it
                   (#2156's base commit is on no branch), pins it, and the default
                   hardening block scrubs history.
  * ImageDefault -- copies the patches and scripts and runs prepare.sh
                   (pin check, yarn install, acorn.min.js, hard gate).

Test command: the repo's own QUnit suite exactly as `gulp test:node` runs it
(cwd test/, require prepro/lib/node.js, paper = dist/paper-full.js, which on the
develop branch is a symlink to src/load.js and so loads the sources, then
test/load.js), driven by /home/qunit-runner.js instead of gulp-qunits, because
gulp-qunits only prints failed assertions and a total. The runner prints one
`MSWE_TEST <PASS|FAIL|SKIP> <module> > <test>` line per QUnit test.
It sets QUnit.config.testTimeout, so an async test whose callback throws fails
with "Test timed out" instead of silently ending the run.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:24.22.2-bookworm"
YARN = "node .yarn/releases/yarn-3.2.4.cjs"
RUNNER = "/home/qunit-runner.js"

_ENV_EXPORTS = (
    "export CI=true\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n"
    "export YARN_ENABLE_IMMUTABLE_INSTALLS=false\n"
    "export YARN_ENABLE_TELEMETRY=0\n"
    "export YARN_ENABLE_PROGRESS_BARS=false\n"
    "export HUSKY_SKIP_INSTALL=1\n"
)

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

_QUNIT_RUNNER = r"""'use strict';
// Runs paper.js's QUnit suite (test/load.js) in Node the way gulp-qunits'
// NodeRunner does (same requires, same globals, cwd test/), but reports every
// test: "MSWE_TEST <PASS|FAIL|SKIP> <module> > <test name>".
const path = require('path');
const repo = path.resolve(process.argv[2] || process.cwd());
const req = require('module').createRequire(path.join(repo, 'package.json'));
const QUnit = req('qunitjs');
Object.assign(global, QUnit);
global.QUnit = QUnit;

process.chdir(path.join(repo, 'test'));

// An async test whose callback throws never calls done(); without a timeout
// QUnit waits forever and Node exits silently once the event loop drains.
// With one, QUnit fails that test ("Test timed out") and runs the rest.
if (!QUnit.config.testTimeout) QUnit.config.testTimeout = 30000;

let finished = false;
let current = null;
let failures = 0;
const seen = Object.create(null);
const messages = Object.create(null);

QUnit.testStart(function(d) {
    current = d.module + ' > ' + d.name;
});

QUnit.log(function(d) {
    if (!d.result) {
        const key = d.module + ' > ' + d.name;
        (messages[key] = messages[key] || []).push(
            (d.message || '') + (d.expected !== undefined
                ? ', expected: ' + d.expected + ', was: ' + d.actual : ''));
    }
});

QUnit.testDone(function(d) {
    const base = d.module + ' > ' + d.name;
    // QUnit allows the same name twice in a module, and some names differ only
    // in trailing whitespace; normalize the whitespace and number the repeats
    // in run order so every test keeps a distinct, stable name.
    const clean = base.replace(/\s+/g, ' ').trim();
    const n = (seen[clean] = (seen[clean] || 0) + 1);
    const name = n > 1 ? clean + ' #' + n : clean;
    const status = d.skipped ? 'SKIP' : d.failed ? 'FAIL' : 'PASS';
    console.log('MSWE_TEST ' + status + ' ' + name);
    if (status === 'FAIL') {
        failures++;
        (messages[base] || []).forEach(function(m) {
            console.log('    assertion failed: ' + m);
        });
    }
    delete messages[base];
});

QUnit.done(function(d) {
    finished = true;
    console.log('MSWE_DONE total=' + d.total + ' passed=' + d.passed +
        ' failed=' + d.failed + ' tests_failed=' + failures);
    process.exitCode = d.failed || failures ? 1 : 0;
});

process.on('uncaughtException', function(err) {
    console.log('MSWE_ERROR ' + ((err && err.stack) || err));
    process.exit(2);
});

process.on('exit', function(code) {
    if (!finished) {
        console.log('MSWE_INCOMPLETE the run ended inside: ' + current);
        process.exitCode = code || 3;
    }
});

// The same requires as gulp/tasks/test.js passes to gulp-qunits:
Object.assign(global, req(path.join(repo, 'node_modules/prepro/lib/node.js')));
global.paper = req(path.join(repo, 'dist/paper-full.js'));
req(path.join(repo, 'test/load.js'));
QUnit.load();
"""


def _yarn_install() -> str:
    # Yarn exits 1 when any build script fails. phantomjs-prebuilt (pulled in by
    # gulp-qunits for its browser runner, which is not used) has no binary for
    # linux/arm64, so its build fails there. Tolerate exactly that failure and
    # nothing else; the gate below re-checks the packages the tests need.
    return (
        "ok=0\n"
        "for attempt in 1 2 3; do\n"
        "    rc=0\n"
        f"    {YARN} install 2>&1 | tee /tmp/yarn-install.log || rc=$?\n"
        '    if [ "$rc" = 0 ]; then ok=1; break; fi\n'
        "    # YN0009 (a build failed) is only reported after resolution, fetch and\n"
        "    # link all succeeded.\n"
        "    if grep -q 'YN0009' /tmp/yarn-install.log \\\n"
        "        && ! grep 'YN0009' /tmp/yarn-install.log | grep -qv 'phantomjs-prebuilt@'; then\n"
        "        echo 'MSWE_LOG only the unused phantomjs-prebuilt build failed'\n"
        "        ok=1; break\n"
        "    fi\n"
        '    echo "yarn install attempt $attempt failed (rc=$rc); retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
        'test "$ok" = 1\n'
    )


def _test_block() -> str:
    # Same block in run.sh, test-run.sh and fix-run.sh.
    return f"node {RUNNER} /home/paper.js\n"


class PaperJs0To2299ImageBase(Image):
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
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "fonts-liberation",
            "libcairo2-dev",
            "libpango1.0-dev",
            "libjpeg-dev",
            "libgif-dev",
            "librsvg2-dev",
            "pkg-config",
        ]

    def dockerfile(self) -> str:
        # The default per-PR Dockerfile, with one addition: some base commits
        # (e.g. #2156's) are on no branch any more, so a plain clone does not
        # contain them. Fetch the commit by SHA when it is missing.
        pin = "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}"
        fetch = (
            'RUN git cat-file -e "${BASE_COMMIT}^{commit}" 2>/dev/null \\\n'
            '    || git fetch --no-tags origin "${BASE_COMMIT}"\n'
        )
        default = super().dockerfile()
        if default.count(pin) != 1:
            raise RuntimeError("unexpected default Dockerfile layout")
        return default.replace(pin, fetch + pin)


class PaperJs0To2299ImageDefault(Image):
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
        return PaperJs0To2299ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _header(self) -> str:
        return f"#!/bin/bash\nset -eo pipefail\n\n{_ENV_EXPORTS}\ncd /home/{self.pr.repo}\n\n"

    def _prepare_sh(self) -> str:
        sha = self.pr.base.sha
        return (
            f"{self._header()}"
            "# --- Section 1: pin ---\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {sha}\n"
            f'test "$(git rev-parse HEAD)" = "{sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "test -f .yarn/releases/yarn-3.2.4.cjs\n"
            "test -f test/load.js\n"
            "\n"
            "# --- Section 2: provision ---\n"
            "node --version\n"
            f"{YARN} --version\n"
            f"{_yarn_install()}"
            "# gulp test:node runs minify:acorn first (node_modules/acorn/acorn.min.js):\n"
            f"{YARN} gulp minify:acorn\n"
            "\n"
            "# --- Section 3: gate ---\n"
            "git diff --quiet -- yarn.lock package.json\n"
            "test -s node_modules/acorn/acorn.min.js\n"
            "test -f node_modules/qunitjs/qunit/qunit.js\n"
            "node -e \"require('canvas').createCanvas(10, 10).getContext('2d'); "
            "require('jsdom'); require('resemblejs'); require('qunitjs'); "
            "require('./node_modules/prepro/lib/node.js'); "
            "require('source-map-support');\"\n"
            "test -f /usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return f"{self._header()}{_test_block()}"

    def _test_run_sh(self) -> str:
        return (
            f"{self._header()}"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_block()}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{self._header()}"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_test_block()}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "qunit-runner.js", _QUNIT_RUNNER),
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
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("paperjs", "paper_js_0_to_2299")
class PAPER_JS_0_TO_2299(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PaperJs0To2299ImageDefault(self.pr, self._config)

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
        log = log.replace("\r", "")

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # "MSWE_TEST PASS <module> > <test name>" from /home/qunit-runner.js
        test_re = re.compile(r"^MSWE_TEST (PASS|FAIL|SKIP) (.+?)\s*$")

        for line in log.splitlines():
            m = test_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "FAIL":
                failed_tests.add(name)
            elif status == "SKIP":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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
