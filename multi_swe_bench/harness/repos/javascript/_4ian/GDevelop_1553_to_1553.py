import re
from dataclasses import replace
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# GDevelop, era "runtime" (PRs below #2000; this dataset: #1553).
#
# The dataset only touches the GDJS game engine test suite, which runs with
# karma + mocha in a headless Chromium.  The repository also contains C++
# (catch.hpp) tests and GDevelop.js jest tests, but those need an emscripten
# build of libGD.js and are not exercised by any PR in the dataset, so they are
# deliberately not run here.
#
# In this era GDJS/Runtime holds plain JavaScript that karma.conf.js loads
# directly (../Runtime/*.js) -- there is no compile step, and the 2020 toolchain
# is karma 1.7 / mocha 1.x, which CI ran on the "active LTS" Node of the time.
# Later PRs moved to TypeScript sources plus a build step and a newer Node; they
# live in the sibling era config GDevelop_4074_to_2392.py.

ERA_RUNTIME = "runtime"
ERA_RUNTIME_DIST = "runtime-dist"
ERA_NEWIDE = "newide"


def _era(pr_number: int) -> str:
    if pr_number < 2000:
        return ERA_RUNTIME
    if pr_number < 3500:
        return ERA_RUNTIME_DIST
    return ERA_NEWIDE


# ONE base image for this era, shared by every PR the era config owns.
#
# ANCHOR_BASE_SHA is the era's newest base commit.  The era currently holds a
# single PR (#1553), so the anchor is that PR's own base commit and the pin is
# exact.  Should the era gain more PRs, move the anchor to the newest one: the
# scrub in the base image keeps only the anchored commit's history, and a PR
# layer can check out its own base commit offline only while that commit is an
# ancestor of the anchor.
ANCHOR_BASE_SHA = "1193e1bbd0c7aa5f4ecedfffbf03e2518fe070b1"

# Node 14 (bullseye): the "active LTS" release GDevelop CI used when this era's
# PRs were merged, and the toolchain the era's karma 1.7 / mocha 1.x suite was
# tested against.  Bullseye also ships a multi-arch "chromium" package (amd64 +
# arm64), which the headless browser tests need.
NODE_IMAGE = "node:14-bullseye"

# One image, one tag, one Dockerfile per era config: every PR of the era inherits
# the same base tag.  Note this deliberately breaks the Dockerfile-QC item that
# expects a PR layer to read "FROM ...:base-pr-<its own number>" -- with a single
# shared base image per era there is no per-PR base tag to name.

class GDevelopRuntimeImageBase(Image):
    """Base image: Node.js + headless Chromium for the karma GDJS tests.

    One image for the whole era: one Dockerfile, one build.  It is pinned to
    ANCHOR_BASE_SHA, and each PR layer's prepare.sh then checks out that PR's own
    base commit from the history already inside the image -- no network, no ref
    restored.
    """

    def __init__(self, pr: PullRequest, config: Config):
        # The image is shared by the era, so its build arg BASE_COMMIT must be the
        # era anchor and not this particular PR's base.sha.  Only the SHA is
        # rewritten; org, repo and number stay untouched.
        self._pr = replace(pr, base=replace(pr.base, sha=ANCHOR_BASE_SHA))
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return NODE_IMAGE

    def image_tag(self) -> str:
        # One base image for the era, so the tag names the era, not a PR.  The
        # PR layers of this era all inherit this single tag.
        return "base-1553-to-1553"

    def workdir(self) -> str:
        return "base-1553-to-1553"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # chromium is multi-arch in Debian bullseye (amd64 + arm64).
        return ["chromium"]

    def extra_setup(self) -> str:
        return "\n".join(
            [
                "ENV CHROME_BIN=/usr/bin/chromium",
                "ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true",
                "ENV CI=true",
                'ENV NODE_OPTIONS="--max-old-space-size=4096"',
            ]
        )

    def dockerfile(self) -> str:
        # The pipeline's DockerfileEnhancer already sets DEBIAN_FRONTEND and
        # LANG (to the very same values) in the ENV block it injects at the top
        # of the file, so the copies the base implementation emits are dead
        # re-declarations.  Drop them to keep the generated Dockerfile readable;
        # both variables stay set for every later instruction.
        dockerfile = super().dockerfile()
        return dockerfile.replace(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8",
            "WORKDIR /home/",
        )


class GDevelopRuntimeImageDefault(Image):
    """Per-PR image: patches, helper scripts and warmed dependency caches."""

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
        return GDevelopRuntimeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _karma_runner(self) -> str:
        """Node script starting karma with a machine-readable reporter.

        The reporter prints one line per test:
            KARMA_TEST_RESULT::PASS|FAIL|SKIP::<suite path> <test description>
        The name carries the full describe() chain and no timing or count
        metadata, so the same test yields the same name in every stage.
        """
        tests_dir = f"/home/{self.pr.repo}/GDJS/tests"
        return f"""\
// Starts the GDJS karma suite with a reporter that prints one stable,
// machine-readable line per test.  Works with karma 1.x (synchronous
// parseConfig) and karma 6.x (promise based parseConfig).
var path = require('path');
var karma = require('{tests_dir}/node_modules/karma');
var karmaConfigPath = path.resolve('{tests_dir}/karma.conf.js');

var ResultReporter = function (baseReporterDecorator) {{
  baseReporterDecorator(this);

  this.onSpecComplete = function (browser, result) {{
    var suite = (result.suite || []).join(' ');
    var fullName = (suite + ' ' + result.description).trim();
    if (result.skipped) {{
      console.log('KARMA_TEST_RESULT::SKIP::' + fullName);
    }} else if (result.success) {{
      console.log('KARMA_TEST_RESULT::PASS::' + fullName);
    }} else {{
      console.log('KARMA_TEST_RESULT::FAIL::' + fullName);
    }}
  }};

  this.onRunComplete = function (browsers, results) {{
    console.log(
      'KARMA_RUN_COMPLETE::failed=' +
        (results ? results.failed : 'unknown') +
        '::success=' +
        (results ? results.success : 'unknown')
    );
  }};
}};
ResultReporter.$inject = ['baseReporterDecorator'];

var overrides = {{
  browsers: ['ChromeHeadlessNoSandbox'],
  customLaunchers: {{
    ChromeHeadlessNoSandbox: {{
      base: 'ChromeHeadless',
      flags: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
    }},
  }},
  singleRun: true,
  autoWatch: false,
  plugins: ['karma-*', {{ 'reporter:gdresult': ['type', ResultReporter] }}],
  reporters: ['gdresult'],
}};

function startServer(config) {{
  var server = new karma.Server(config, function (exitCode) {{
    process.exit(exitCode);
  }});
  server.start();
}}

var parsed;
try {{
  parsed = karma.config.parseConfig(karmaConfigPath, overrides);
}} catch (err) {{
  console.error('Karma config parse failed: ' + err.message);
  process.exit(1);
}}

if (parsed && typeof parsed.then === 'function') {{
  parsed.then(startServer).catch(function (err) {{
    console.error('Karma config parse failed: ' + err.message);
    process.exit(1);
  }});
}} else {{
  startServer(parsed);
}}
"""

    def _build_block(self) -> str:
        """Shell block compiling the engine, chosen statically from the era.

        Era "runtime" (PR < 2000) ships plain JavaScript under GDJS/Runtime and
        karma.conf.js loads it directly, so no build exists.  Both later eras
        ship TypeScript sources that GDJS/scripts/build.js compiles into the
        directory their own karma.conf.js reads (Runtime-dist, then
        newIDE/app/resources/GDJS/Runtime).  The fix patch edits those sources,
        so the build must run after patching -- and its absence is a hard error,
        never a silently skipped step that would leave a stale engine behind.
        """
        era = _era(self.pr.number)
        if era == ERA_RUNTIME:
            return "\n".join(
                [
                    "# Era 'runtime': plain JavaScript sources, karma loads",
                    "# GDJS/Runtime directly, so no build script is expected.  Run",
                    "# one if the checked-out tree has it anyway.",
                    "if [ -f GDJS/scripts/build.js ]; then",
                    "    (cd GDJS && node scripts/build.js)",
                    "fi",
                    "",
                ]
            )
        return "\n".join(
            [
                f"# Era '{era}': TypeScript sources must be compiled before karma",
                "# runs.  No '|| true' here: a broken or missing build has to fail",
                "# loudly instead of leaving a stale engine in place.",
                "if [ ! -f GDJS/scripts/build.js ]; then",
                f"    echo 'ERROR: GDJS/scripts/build.js is missing, but PR #{self.pr.number} belongs to era {era} which requires it' >&2",
                "    exit 1",
                "fi",
                "(cd GDJS && node scripts/build.js)",
                "",
            ]
        )

    def _test_script(self) -> str:
        """The single test command, shared verbatim by the three run scripts."""
        repo = self.pr.repo
        build_block = self._build_block()
        return f"""#!/bin/bash
set -eo pipefail

export CI=true
export CHROME_BIN=/usr/bin/chromium
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}

# Dependencies: the test patch can change GDJS/tests/package.json, so the
# install is repeated here.  It is a no-op when the image already warmed the
# cache.  "|| true" is allowed on installs only (native module compilation can
# fail without preventing the tests from running).
if [ -f GDJS/package.json ]; then
    (cd GDJS && npm install --no-audit --no-fund --no-save --no-package-lock) || true
fi
(cd GDJS/tests && npm install --no-audit --no-fund --no-save --no-package-lock) || true

{build_block}
echo "==== KARMA_TESTS_START ===="
# errexit is lifted only around the test runner so that a failing suite
# (expected in the test-patch stage) still reaches the end marker.  The exit
# code is preserved and re-raised below, it is not swallowed.
set +e
node /home/run_karma.js
KARMA_EXIT=$?
set -e
echo "==== KARMA_TESTS_END::exit=$KARMA_EXIT ===="

exit $KARMA_EXIT
"""

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        # The three run scripts differ only in which patches they apply; the
        # test command itself lives in one shared script, so it cannot drift.
        run_test = "bash /home/gdjs-test.sh\n"
        header = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n" f"cd /home/{repo}\n"

        check_git_changes = """#!/bin/bash
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

        prepare = f"""#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Warm the dependency caches and the engine build at the base commit.  Failures
# are non-fatal here: the run scripts repeat both steps after the patches are
# applied.  "--no-save --no-package-lock" keeps package.json and the lockfiles
# byte-identical to the base commit, so a test patch that edits
# GDJS/tests/package.json (PR #2475 does) still applies cleanly afterwards.
if [ -f GDJS/package.json ]; then
    (cd GDJS && npm install --no-audit --no-fund --no-save --no-package-lock) || true
fi
if [ -f GDJS/tests/package.json ]; then
    (cd GDJS/tests && npm install --no-audit --no-fund --no-save --no-package-lock) || true
fi
if [ -f GDJS/scripts/build.js ]; then
    (cd GDJS && node scripts/build.js) || true
fi
"""

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "run_karma.js", self._karma_runner()),
            File(".", "gdjs-test.sh", self._test_script()),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", header + run_test),
            File(
                ".",
                "test-run.sh",
                header + "git apply --whitespace=nowarn /home/test.patch\n" + run_test,
            ),
            File(
                ".",
                "fix-run.sh",
                header
                + "git apply --whitespace=nowarn /home/test.patch\n"
                + "git apply --whitespace=nowarn /home/fix.patch\n"
                + run_test,
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


@Instance.register("4ian", "GDevelop_1553_to_1553")
class GDEVELOP_1553_TO_1553(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GDevelopRuntimeImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        start_marker = "==== KARMA_TESTS_START ===="
        start_idx = log.find(start_marker)
        if start_idx != -1:
            section = log[start_idx + len(start_marker) :]
            end_idx = section.find("==== KARMA_TESTS_END")
            if end_idx != -1:
                section = section[:end_idx]

            result_re = re.compile(r"^KARMA_TEST_RESULT::(PASS|FAIL|SKIP)::(.+)$")
            for raw_line in section.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                match = result_re.match(line)
                if not match:
                    continue
                name = f"karma::{match.group(2).strip()}"
                status = match.group(1)
                if status == "PASS":
                    passed_tests.add(name)
                elif status == "FAIL":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)

        # The Firebase extension end-to-end tests drive a live Firebase backend
        # over the network.  Inside the sandbox they answer nondeterministically
        # and flip between pass and fail from one stage to the next -- observed
        # on PR #4018, where two of them passed in the test stage and failed in
        # the fix stage, tripping Report.check() rule 2 and invalidating an
        # otherwise clean instance (12 real fail-to-pass transitions).
        #
        # They are removed from all three stages by their exact suite prefix, so
        # the removal is symmetric and cannot hide a real transition; no PR in
        # this dataset touches the Firebase extension, so nothing graded is lost.
        # Every other test, including the equally environment-sensitive WebGL
        # "Light" suite, is kept: those fail consistently in all three stages and
        # therefore cancel out on their own.
        firebase_suite = "karma::Firebase extension end-to-end tests"

        def without_firebase(names: set[str]) -> set[str]:
            return {name for name in names if not name.startswith(firebase_suite)}

        passed_tests = without_firebase(passed_tests)
        failed_tests = without_firebase(failed_tests)
        skipped_tests = without_firebase(skipped_tests)

        # TestResult invariants: the three sets must be pairwise disjoint.
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
