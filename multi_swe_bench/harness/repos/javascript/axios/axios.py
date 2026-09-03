import dataclasses
import os
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_VERSION = "10.24.1"
KARMA_CHROME_LAUNCHER_VERSION = "2.2.0"
# INVARIANT: every base.sha in this dataset must be an ancestor of this commit.
# The single shared base image is pinned here and its history is scrubbed down to
# HEAD's ancestry, so a base.sha outside that ancestry cannot be checked out by
# prepare.sh. Re-verify with `git merge-base --is-ancestor <base.sha> <this>`
# before adding a PR whose base commit is newer than #345's.
DATASET_HEAD_SHA = "120e8f5557975668ba2bbc2ac78b0dee3998e4e9"


def _arch_suffix() -> str:
    # `MSB_ARCH_SUFFIX=arm64` selects the per-arch image tag for multi-arch
    # builds, so an arm64 run does not overwrite the amd64 images in the local
    # daemon. Same mechanism as repos/c/DarkFlippers/unleashed_firmware.py:552.
    suffix = os.environ.get("MSB_ARCH_SUFFIX", "")
    return f"-{suffix}" if suffix else ""
# karma.conf.js at base sha 4bbde9ae lists the `sinon` framework and depends on
# karma-sinon, but never declares `sinon` itself (it only reaches
# devDependencies at 093593cb). karma-sinon then dies with
# "Cannot find module 'sinon'" and the whole suite reports zero tests, so the
# base image backfills it when, and only when, the commit is missing it.
SINON_FALLBACK_VERSION = "1.17.3"

# Every PR in this dataset (#123..#345, Oct-2015..Jun-2016, axios v0.7.0..v0.12.0)
# builds with `grunt test` -> [eslint, nodeunit, karma:single, ts]. Only the
# nodeunit and karma steps emit per-test names, and only those two are targeted
# by the datasets's test patches, so the run scripts drive them directly instead
# of going through grunt (grunt aborts the whole chain on the first task
# failure, which would hide every later suite in the test/fix stages).
KARMA_CI_CONFIG = r"""// Deterministic CI wrapper around the repo's own karma.conf.js.
// Loaded via `karma start /home/karma.ci.js`; the repo tree is never modified.
process.env.CHROME_BIN = process.env.CHROME_BIN || '/usr/bin/chromium';

var baseConf = require('/home/axios/karma.conf.js');

function CIReporter(baseReporterDecorator) {
  baseReporterDecorator(this);
  var self = this;

  self.onSpecComplete = function (browser, result) {
    var suite = (result.suite || []).join(' > ');
    var name = suite ? suite + ' > ' + result.description : result.description;
    var status = result.skipped ? 'SKIP' : (result.success ? 'PASS' : 'FAIL');
    self.write('KARMA_TEST|' + status + '|' + name + '\n');
  };

  self.onRunComplete = function () {
    self.write('KARMA_RUN_COMPLETE\n');
  };

  self.onBrowserError = function (browser, error) {
    self.write('KARMA_BROWSER_ERROR|' + error + '\n');
  };
}
CIReporter.$inject = ['baseReporterDecorator'];

module.exports = function (config) {
  baseConf(config);

  config.set({
    basePath: '/home/axios',
    customLaunchers: {
      ChromeHeadlessCI: {
        base: 'ChromeHeadless',
        flags: [
          '--no-sandbox',
          '--disable-setuid-sandbox',
          '--disable-gpu',
          '--disable-dev-shm-usage',
          '--headless',
          '--remote-debugging-port=9222'
        ]
      }
    },
    singleRun: true,
    autoWatch: false,
    colors: false,
    captureTimeout: 180000,
    browserNoActivityTimeout: 180000,
    browserDisconnectTimeout: 30000,
    // 0 = never re-run the suite after a disconnect. A retry re-emits every
    // spec name, which was measured to turn 80 results into 240 and would make
    // the three stages disagree on both counts and names.
    browserDisconnectTolerance: 0
  });

  // Must be the first `included` file: it runs after karma's client installs
  // its own window.onerror but before any spec bundle is evaluated.
  config.files.unshift({
    pattern: '/home/karma-onerror-shim.js',
    included: true,
    served: true,
    watched: false
  });

  // Assigned directly rather than through config.set(): karma merges arrays
  // index-wise, so set() would leave stale trailing entries behind (the 2016
  // karma.conf defaults to ['Firefox','Chrome','Safari','Opera']).
  config.browsers = ['ChromeHeadlessCI'];
  config.reporters = ['ci'];
  config.plugins = ['karma-*', { 'reporter:ci': ['type', CIReporter] }];
  config.logLevel = config.LOG_WARN;
};
"""

# A spec file whose top-level require() cannot be resolved is emitted by webpack
# as a module that THROWS at load time. karma's default window.onerror turns
# that single load-time throw into a fatal run error and reports ZERO specs -
# including every spec file that loaded fine. Downgrading it to a logged event
# lets the rest of the suite run, which is what turns a test patch that adds
# both a spec for a not-yet-existing module AND assertions in an existing spec
# into real fail-to-pass results instead of none-to-pass. Measured on PR #308's
# test stage: 0 results before, 120 pass / 2 fail after.
KARMA_ONERROR_SHIM = r"""(function (global) {
  global.onerror = function (message, source, lineno) {
    if (global.__karma__ && typeof global.__karma__.log === 'function') {
      global.__karma__.log('warn', [
        'KARMA_LOAD_ERROR: ' + message + ' @ ' + source + ':' + lineno
      ]);
    }
    return true;
  };
})(window);
"""

# karma 0.13.x calls `socketServer.sockets.sockets.forEach(...)` on teardown.
# socket.io >= 1.5 turned that Array into a plain object, so teardown throws
# AFTER the run finished: every exit code became 7 and reporter output risked
# truncation. This shim only normalises the teardown value; no test semantics.
KARMA_TEARDOWN_SHIM = r"""var fs = require('fs');

var target = process.argv[2];
if (!target || !fs.existsSync(target)) {
  console.log('fix-karma: nothing to patch at ' + target);
  process.exit(0);
}

var src = fs.readFileSync(target, 'utf8');
var MARKER = '__KARMA_CI_SOCKETS_SHIM__';

if (src.indexOf(MARKER) !== -1) {
  console.log('fix-karma: already patched');
  process.exit(0);
}

var needle = 'var sockets = socketServer.sockets.sockets';
if (src.indexOf(needle) === -1) {
  console.log('fix-karma: pattern not found, leaving file untouched');
  process.exit(0);
}

var shim =
  needle +
  '; /* ' + MARKER + ' */ ' +
  'if (sockets && typeof sockets.forEach !== "function") { ' +
  'sockets = Object.keys(sockets).map(function (k) { return sockets[k] }) }';

fs.writeFileSync(target, src.replace(needle, shim));
console.log('fix-karma: patched ' + target);
"""

# Identical body in run.sh / test-run.sh / fix-run.sh so the three stages always
# execute the same tests. `|| VAR=$?` is not `|| true`: both exit codes are
# echoed and re-raised by the final `exit`, so a runner that fails to start
# still surfaces as a non-zero stage with zero parsed tests.
TEST_COMMANDS = """export CI=true
export CHROME_BIN=/usr/bin/chromium

KARMA_RC=0
node ./node_modules/karma/bin/karma start /home/karma.ci.js --single-run || KARMA_RC=$?

NODEUNIT_RC=0
echo "NODEUNIT_BEGIN"
if [ -d test/unit ]; then
    node ./node_modules/.bin/nodeunit --reporter default $(find test/unit -name '*.js' | sort) || NODEUNIT_RC=$?
else
    echo "no nodeunit suite at this commit"
fi
echo "NODEUNIT_END"

echo "STAGE_EXIT karma=$KARMA_RC nodeunit=$NODEUNIT_RC"
exit $(( KARMA_RC != 0 || NODEUNIT_RC != 0 ))
"""


class ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        # build_dataset passes BASE_COMMIT from self.pr.base.sha, so rebasing the
        # PR onto DATASET_HEAD_SHA is what makes one shared image hold every
        # instance's base commit (see the DATASET_HEAD_SHA invariant above).
        self._pr = dataclasses.replace(
            pr, base=dataclasses.replace(pr.base, sha=DATASET_HEAD_SHA)
        )
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "debian:bookworm"

    def image_tag(self) -> str:
        return f"base{_arch_suffix()}"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "chromium",
            "fonts-liberation",
            "xz-utils",
            "procps",
        ]

    def dockerfile(self) -> str:
        # Overrides Image.dockerfile() solely to keep a SINGLE ENV block. The
        # default emits its own `ENV DEBIAN_FRONTEND` + `ENV LANG` after
        # `WORKDIR /home/` (image.py:236-238), which duplicates two keys the
        # DockerfileEnhancer ENV block already sets and yields three ENV sections.
        # CHROME_BIN/CI are dropped here rather than added: all three run scripts
        # export both, and karma.ci.js carries its own CHROME_BIN fallback.
        base_img = self.dependency()
        packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ] + self.extra_packages()
        apt_command = self._get_apt_update_command(" \\\n    ".join(packages), base_img)

        sections = [f"FROM {base_img}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append("WORKDIR /home/")
        sections.append(apt_command)
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}')
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")
        sections.append(self._HARDENING_BLOCK)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}{_arch_suffix()}"

    def workdir(self) -> str:
        # Deliberately NOT suffixed: gen_report parses the instance dir name with
        # int(name[3:]) (gen_report.py:359), so "pr-123-arm64" would raise and the
        # instance would be silently dropped from the report. Per-arch runs are
        # separated by --workdir instead.
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                "karma.ci.js",
                KARMA_CI_CONFIG,
            ),
            File(
                ".",
                "karma-onerror-shim.js",
                KARMA_ONERROR_SHIM,
            ),
            File(
                ".",
                "fix-karma.js",
                KARMA_TEARDOWN_SHIM,
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
                """#!/bin/bash
set -e

if ! command -v node > /dev/null 2>&1; then
    case "$(uname -m)" in
        x86_64) NODE_ARCH=x64 ;;
        aarch64|arm64) NODE_ARCH=arm64 ;;
        *) NODE_ARCH=x64 ;;
    esac
    # Retries are not optional here: this download runs once per PR image inside
    # the buildx container builder, and under multi-arch (QEMU) load it has been
    # observed to fail transiently with both "Could not resolve host" (curl 6)
    # and "HTTP/2 stream not closed cleanly" (curl 18), which aborts the build.
    curl -fsSL --retry 5 --retry-delay 3 --retry-connrefused --http1.1 \\
        "https://nodejs.org/dist/v{node}/node-v{node}-linux-$NODE_ARCH.tar.xz" -o /tmp/node.tar.xz
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner
    rm -f /tmp/node.tar.xz
fi
node -v
npm -v

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install --ignore-scripts --no-audit --no-fund --no-package-lock || true
npm install --no-save --ignore-scripts --no-audit --no-fund --no-package-lock \\
    karma-chrome-launcher@{launcher} || true

if [ ! -d node_modules/sinon ]; then
    npm install --no-save --ignore-scripts --no-audit --no-fund --no-package-lock \\
        sinon@{sinon} || true
fi

# The installs above carry `|| true` because native-module compile failures are
# expected and non-fatal. That also swallows a genuine network failure, which
# yields an image with no node_modules and a silent (0,0,0) TestResult at every
# stage. Fail the image build loudly instead.
test -f node_modules/karma/bin/karma

node /home/fix-karma.js /home/{pr.repo}/node_modules/karma/lib/server.js
""".format(
                    pr=self.pr,
                    node=NODE_VERSION,
                    launcher=KARMA_CHROME_LAUNCHER_VERSION,
                    sinon=SINON_FALLBACK_VERSION,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

{tests}""".format(pr=self.pr, tests=TEST_COMMANDS),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

{tests}""".format(pr=self.pr, tests=TEST_COMMANDS),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{tests}""".format(pr=self.pr, tests=TEST_COMMANDS),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("axios", "axios")
class Axios(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

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
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Emitted by the CIReporter in /home/karma.ci.js. The suite path is
        # baked into the name, so leaf `it()` descriptions cannot collide, and
        # no timing or count metadata is captured.
        re_karma = re.compile(r"^KARMA_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")
        # nodeunit's `default` reporter: one line per test, no metadata.
        re_nodeunit_pass = re.compile(r"^\s*[\u2714\u2713]\s+(\S.*?)\s*$")
        re_nodeunit_fail = re.compile(r"^\s*[\u2716\u2717\u00d7]\s+(\S.*?)\s*$")

        karma_buckets = {
            "PASS": passed_tests,
            "FAIL": failed_tests,
            "SKIP": skipped_tests,
        }

        in_nodeunit = False
        for line in clean_log.splitlines():
            stripped = line.strip()

            if stripped == "NODEUNIT_BEGIN":
                in_nodeunit = True
                continue
            if stripped == "NODEUNIT_END":
                in_nodeunit = False
                continue

            karma_match = re_karma.match(line)
            if karma_match:
                karma_buckets[karma_match.group(1)].add(f"karma > {karma_match.group(2)}")
                continue

            if not in_nodeunit:
                continue

            nodeunit_pass = re_nodeunit_pass.match(line)
            if nodeunit_pass:
                passed_tests.add(f"nodeunit > {nodeunit_pass.group(1)}")
                continue

            nodeunit_fail = re_nodeunit_fail.match(line)
            if nodeunit_fail:
                failed_tests.add(f"nodeunit > {nodeunit_fail.group(1)}")

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# Routing aliases for the emitted rows' `number_interval`.
#
# The raw records carry no `number_interval`, so every PR routes on "axios/axios"
# at build time. The OUTPUT row differs: the Dataset.build wrapper installed by
# repos/python/langchain_ai_langgraph/langgraph.py:69-86 is not org/repo-scoped,
# and its `or str(pr.number)` fallback (line 81) stamps number_interval="123",
# "160", ... onto every single-PR row this tree emits. Feeding that row back in
# makes Instance.create() (instance.py:42-43) look up "axios/123" and raise
# "Instance 'axios/123' is not registered", so the shipped dataset could not be
# evaluated.
#
# Registering the bare numbers as aliases is the idiom this tree already uses for
# exactly this situation - see typescript/coder/code_server.py:531-552,
# golang/go_playground/validator.py:405-411 and cpp/capnproto/capnproto.py:434-441.
# It is preferred over blanking the field in the artifact, which the next
# regeneration would silently undo. No collision: the only other "axios/*" keys
# are "axios/axios", "axios/axios_275_to_160" and "axios/axios_3852_to_1655".
_NUMBER_INTERVAL_ALIASES = ["123", "160", "167", "308", "345"]

for _interval in _NUMBER_INTERVAL_ALIASES:
    Instance.register("axios", _interval)(Axios)
