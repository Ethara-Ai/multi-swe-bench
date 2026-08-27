"""ing-bank/lion -- single era, PR 2108 (Node 16 / npm workspaces / @web/test-runner + Playwright).

Toolchain. The dataset carries one PR, 2108, whose base is 7e72f60cc9c3
(2023-10-23). At that commit `.nvmrc` pins `16` and `.github/workflows/verify.yml`
runs every job on `actions/setup-node@v1` with `node-version: 16.x`;
`package-lock.json` (lockfileVersion 2) resolves `playwright` and `playwright-core`
to exactly 1.28.0, and `package.json` declares no `engines` block. Those facts fix
the base image: mcr.microsoft.com/playwright:v1.28.0-focal ships Node 16.18.1,
npm 8.19.2 and the chromium-1033 build that playwright-core 1.28.0 asks for, so no
browser is ever downloaded and no apt package is ever installed.

File shape. This is the plain `<repo>.py` form registered under `ing-bank/lion`,
not a `lion_<hi>_to_<lo>.py` range file. R24 makes range files the default, but
§3.1 and §17.4 carve out exactly this case: `Instance.create()`
(instance.py:41-49) reaches a range key ONLY from a dataset row carrying
`number_interval`, and every row in ing-bank__lion_raw_dataset.jsonl leaves that
field empty. A range file would therefore have to be reached through a plain-key
alias, leaving a file whose name advertises an interval that nothing ever routes
to -- and an alias that would silently route an old PR onto a new toolchain the
day a second era is added. The plain key is the honest shape while the dataset
looks like this.

    When a second era does arrive: add `lion_<hi>_to_<lo>.py` range files, set
    `number_interval` on the dataset rows, and keep whichever era can serve
    un-intervalled rows registered here under the plain key.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        # mcr.microsoft.com/playwright:v1.28.0-focal, chosen from the lockfile rather
        # than from a node:<n> tag. Verified in the image: Node 16.18.1 (matching
        # .nvmrc "16" and the repo's own CI matrix), npm 8.19.2, Ubuntu 20.04, and
        # /ms-playwright/chromium-1033 preinstalled with every system library the
        # browser needs.
        #
        # That last part is the reason for this base and not node:16-bullseye. lion's
        # graded suite is a BROWSER suite -- @web/test-runner driving Playwright -- so
        # a bare Node image would need the whole libnss3/libatk/libgbm/libasound set
        # from apt plus a `playwright install chromium` download at build time.
        #
        # Focal reached end of standard support in April 2025, which normally argues
        # against it (R11). It is deliberate here: the browser revision is pinned by
        # the LOCKFILE, and playwright-core 1.28.0 refuses any revision but 1033, so
        # a newer playwright image would force a runtime browser download instead of
        # using the preinstalled one. The usual EOL failure mode -- `apt-get update`
        # 404ing on archived suites -- cannot occur because this Dockerfile runs no
        # apt-get at all. If a package is ever genuinely needed here, add it via a
        # newer -jammy playwright tag whose bundled revision still matches the
        # lockfile, never by pointing sources.list at the archive mirror.
        #
        # Returning a STRING is what keeps DockerfileEnhancer engaged for this image,
        # and the enhancer is what supplies the whole base contract: the syntax
        # directive, the TARGETARCH/REPO_URL/BASE_COMMIT ARGs, the proxy ARG+ENV
        # block, the CA-cert symlink farm, and the rewrite of the `git clone` below
        # into clone + `git reset --hard` + `git checkout ${BASE_COMMIT}` +
        # Image._HARDENING_BLOCK + CMD. Hand-writing any of that here would duplicate
        # what the enhancer injects.
        return "mcr.microsoft.com/playwright:v1.28.0-focal"

    def image_tag(self) -> str:
        # base-pr-<N>, not a shared "base": this tag is per-PR, which is the
        # convention across the tree (e.g. golang/gin_gonic/gin.py). Because the base
        # belongs to exactly one PR, cloning inside it is correct and R10 -- "the
        # SHARED base must not clone" -- does not apply; there is no other PR for the
        # enhancer's checkout to mispin.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        # The base stages nothing. Patches and run-scripts belong to the PR layer.
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

# npm draws progress bars and colourised output with non-ASCII characters. The
# harness decodes build output with the platform default codec (cp1252 on
# Windows), where those bytes are undefined and abort the build with
# "'charmap' codec can't decode byte ...".
ENV NPM_CONFIG_PROGRESS=false
ENV NPM_CONFIG_COLOR=false
ENV NO_COLOR=1
ENV FORCE_COLOR=0
ENV CI=true
# Raised from Node 16's ~1.5GB default heap. This is a 40-package monorepo and
# @web/test-runner keeps every group's module graph in the parent process while
# it serves them; the measured full run does not approach the default cap, so
# this is headroom against a future group being added rather than a fix for an
# observed OOM. Identical in all three stages either way.
ENV NODE_OPTIONS=--max-old-space-size=4096

# No apt-get. Verified inside the base image: git 2.25.1, npm 8.19.2, Node
# 16.18.1 and /etc/ssl/certs/ca-certificates.crt are all already present, and
# chromium-1033 ships with every shared library it needs. An apt layer here would
# add nothing and would be one more thing that can 404 on focal's archives (R11).

WORKDIR /home/

{code}

{self.clear_env}

"""


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
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # THE TEST COMMAND, defined once and interpolated into all three stage scripts
        # byte-for-byte (R3). Anything that differs between stages -- a flag, a group
        # filter, a reporter -- renames tests and makes the FAIL->PASS transition
        # invisible.
        #
        # --config node_modules/.msb/wtr-msb.config.mjs, not the repo's own
        # web-test-runner.config.mjs, for two reasons:
        #
        #   1. BROWSERS. The repo config launches firefox, chromium AND webkit, so the
        #      whole suite would run three times for no grading benefit. The shipped
        #      web-test-runner-chrome.config.mjs narrows that to chromium but still
        #      cannot report per-test names (below). Our config re-uses the repo config
        #      verbatim -- same nodeResolve, same 5000ms testFramework timeout, same
        #      per-package `groups` -- and swaps only the browser list.
        #
        #   2. PER-TEST NAMES. @web/test-runner's default reporter prints a progress
        #      bar and the suite tree only for FAILING files; a fully passing file
        #      prints nothing at all. Measured on the combobox group: 164 tests ran and
        #      the terminal showed one summary line. parse_log cannot derive f2p from
        #      that. Our config installs a reporter that walks each session's
        #      testResults tree and appends one "===TEST=== <STATUS> <id>" line per
        #      test to /tmp/msb-results.txt, which run.sh then cats.
        #
        # --coverage is deliberately NOT passed even though the repo's own
        # `test:browser` script uses it. It instruments every module and enforces
        # statement/branch/function/line thresholds that have nothing to do with this
        # PR; dropping it is identical across all three stages, so it cannot invent a
        # transition.
        #
        # No --group filter: every group in the config runs. The test patch edits three
        # SHARED suite files -- form-core/test-suites/choice-group/{ChoiceGroupMixin,
        # CustomChoiceGroupMixin}.suite.js and listbox/test-suites/ListboxMixin.suite.js
        # -- which are re-exported through packages/ui/exports/*-test-suites.js and
        # consumed by test files in checkbox-group, radio-group, select-rich, fieldset,
        # listbox, textarea and every input-* package. Scoping to combobox + form-core
        # (where the changed *.test.js files live) would silently drop any transition
        # landing in those other packages.
        #
        # CI=true is exported here as well as set as a Dockerfile ENV. The ENV alone
        # is what the harness actually runs with, but repeating it keeps each stage
        # script correct when run by hand inside the container, and it is the same
        # line in all three so it cannot skew the comparison.
        test_cmd = (
            "export CI=true\n"
            "rm -f /tmp/msb-results.txt\n"
            "rc=0\n"
            "npx web-test-runner --config node_modules/.msb/wtr-msb.config.mjs || rc=$?\n"
            'echo "===WTR_EXIT=== $rc"\n'
            # The results file is the graded artefact, so it is cat'ed unconditionally
            # and its absence is announced rather than passed off as "no tests exist".
            #
            # `|| rc=$?` is NOT `|| true`. The prohibition on `|| true` for a test
            # command exists because it swallows a runner that failed to START, which
            # then reads downstream as "this suite has no tests". Here the code is
            # captured and re-raised by `exit $rc` below, so a missing binary still
            # exits non-zero AND prints ===NO_RESULTS===, which parse_log turns into
            # 0/0/0 and Report.check() rejects. The only thing suppressed is `set -e`
            # aborting before the results are printed.
            'cat /tmp/msb-results.txt 2>/dev/null || echo "===NO_RESULTS==="\n'
            "exit $rc"
        )

        # The reporter config is written into node_modules/, which .gitignore already
        # covers, so `git status --porcelain` stays empty and the stage patches still
        # apply. Putting it anywhere tracked would dirty the work tree; putting it
        # outside /home/lion would break Node resolution of @web/test-runner-playwright
        # and the `../../web-test-runner.config.mjs` import.
        #
        # Deliberately a plain literal with no .format()/f-string substitution: the
        # repo root comes from process.cwd(), which every stage script sets by
        # cd'ing to /home/<repo> before invoking the runner. Interpolating Python
        # into a body this full of JS braces means doubling every one of them, and
        # a single missed pair is a KeyError at render time rather than a visible
        # mistake.
        wtr_config = """import fs from 'fs';
import path from 'path';
import baseConfig from '../../web-test-runner.config.mjs';
import { playwrightLauncher } from '@web/test-runner-playwright';

const ROOT = path.resolve(process.cwd());
const OUT = '/tmp/msb-results.txt';

// Collapse every whitespace run to a single space. Not cosmetic: lion writes
// multi-line it() titles (fieldset's 'has an aria-labelledby ...' embeds a
// <label> template), and a raw newline inside a name would split one test across
// several output lines, so parse_log would read a truncated name plus junk.
const clean = s => String(s).replace(/\\s+/g, ' ').trim();

// Test ids are "<repo-relative test file> > <describe> > ... > <it>". The file
// prefix is required, not cosmetic: report.py's _test_name_matches_files
// (report.py:385-395) credits a name only when it startswith("<patch file> > "),
// which is what lets a test the patch adds be classified as n2p instead of being
// dropped into fix_patch_authored_candidates as a phantom. Measured on the real
// three-stage logs: 17/17 f2p and 132/132 n2p names resolve to a test-patch file.
function walk(node, trail, file, lines) {
  for (const t of node.tests || []) {
    const name = [file, ...trail, clean(t.name)].join(' > ');
    const status = t.skipped ? 'SKIP' : t.passed ? 'PASS' : 'FAIL';
    lines.push('===TEST=== ' + status + ' ' + name);
  }
  for (const s of node.suites || []) walk(s, [...trail, clean(s.name)], file, lines);
}

const msbReporter = () => ({
  reportTestFileResults({ sessionsForTestFile, testFile }) {
    const rel = path.relative(ROOT, testFile).split(path.sep).join('/');
    const lines = [];
    for (const session of sessionsForTestFile) {
      if (session.testResults) walk(session.testResults, [], rel, lines);
      // A file that throws on import produces a session with errors and NO
      // testResults. Recording it keeps that case distinguishable from "this
      // file has no tests" when reading a stage log.
      for (const err of session.errors || []) {
        lines.push('===FILEERROR=== ' + rel + ' :: ' + clean(err.message || String(err)).slice(0, 200));
      }
    }
    if (lines.length) fs.appendFileSync(OUT, lines.join('\\n') + '\\n');
  },
});

const config = { ...baseConfig };

// Raise mocha's per-test timeout from the repo config's 5000ms. This is a
// FLAKE FIX with a measured cause, not a guess. Running the full suite under
// deliberate CPU contention (12 busy loops) reproduced 4 and then 10 failures,
// and every one carried the same message:
//
//     Timeout of 5000ms exceeded. For async tests and hooks, ensure "done()"
//     is called; ...
//
// concentrated in timing-sensitive overlay/dropdown suites (input-tel-dropdown,
// dialog, input-datepicker, accordion) that are unrelated to this PR. 5000ms is
// generous on CI metal and tight in a loaded container, and the harness runs
// instances concurrently (--max_workers), so that contention is a normal
// production condition rather than an artefact.
//
// This matters beyond noise: a flake that lands PASS at the test stage and FAIL
// at the fix stage trips Report.check() rule 2 and invalidates the whole
// instance, discarding a PR that is otherwise perfectly good.
//
// A timeout is an upper bound, not a delay: a passing test still finishes as
// fast as it ever did, so this costs nothing in the common case -- unlike
// lowering `concurrency`, which slows every run. It is applied identically in
// all three stages, so it cannot manufacture or hide a transition.
config.testFramework = {
  ...baseConfig.testFramework,
  config: {
    ...((baseConfig.testFramework && baseConfig.testFramework.config) || {}),
    timeout: '60000',
  },
};

// --disable-dev-shm-usage is load-bearing, not boilerplate. Chromium puts its
// renderer shared memory in /dev/shm, which Docker sizes at 64MB by default, and
// the harness starts instance containers without --shm-size. Exhausting it kills
// renderers mid-suite as "Target closed"/"page crashed", which would surface as
// nondeterministic failures in whichever stage happened to hit it. Verified by
// grep that playwright-core 1.28.0's chromium.js does NOT pass this flag itself,
// and verified by running the suite in a 64MB-shm container: 164 tests, zero
// crash signatures. The flag makes Chromium fall back to /tmp instead.
config.browsers = [
  playwrightLauncher({
    product: 'chromium',
    launchOptions: { args: ['--disable-dev-shm-usage'] },
  }),
];
config.reporters = [msbReporter()];
export default config;
"""

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
                "wtr-msb.config.mjs",
                wtr_config,
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# The BASE image (base-pr-{pr.number}) already cloned, checked out
# ${{BASE_COMMIT}} and ran the history scrub, so in practice the two git lines
# above assert that state rather than change it: HEAD is already detached at this
# sha and every other ref has been deleted. They are kept because this is the
# standard prepare.sh shape and because they still do the right thing -- the
# commit object is local, so no network and no surviving ref is required.

# `npm ci`, not `npm install`: package-lock.json is lockfileVersion 2 and the repo
# declares npm workspaces ("packages/*", "packages-node/*"). ci installs exactly
# the locked tree, so all three graded stages resolve identical dependencies and a
# f2p diff cannot be dependency drift. Measured: 2017 packages in ~1m.
#
# --ignore-scripts skips the root postinstall, which is
# `npx patch-package && npm run custom-elements-manifest`. Neither is needed and
# both are harmful here: there is no patches/ directory at this commit so
# patch-package only costs a network fetch, and custom-elements-manifest writes a
# custom-elements.json into every workspace package -- tracked paths that would
# dirty the work tree and break `git apply` at the test and fix stages. The
# browser suite does not read those manifests; @web/test-runner serves the
# packages' ESM sources directly via nodeResolve.
#
# `|| true` per the standard install rule. It is safe ONLY because the assertions
# below turn a failed install back into a failed build: without them, `|| true`
# would seal an image with no node_modules, whose stages report nothing and read
# downstream as "this suite has no tests" rather than as a broken image.
npm ci --ignore-scripts --no-audit --no-fund || true

# The reporter config lands under node_modules/ AFTER the install, because npm ci
# deletes and recreates node_modules wholesale.
mkdir -p node_modules/.msb
cp /home/wtr-msb.config.mjs node_modules/.msb/wtr-msb.config.mjs

# Refuse to seal an image whose graded stages could not report anything.
test -x node_modules/.bin/web-test-runner
test -x node_modules/.bin/wtr
node --input-type=module -e "import('/home/{pr.repo}/node_modules/.msb/wtr-msb.config.mjs').then(m => {{ if (!m.default.reporters || !m.default.browsers || !m.default.groups) process.exit(1); }}, () => process.exit(1))"
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}

""".format(pr=self.pr, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}

""".format(pr=self.pr, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Deliberately thin. Everything this layer needs -- the toolchain, the
        # clone, the BASE_COMMIT checkout, the history scrub, the proxy/CA trust --
        # is already earned in ImageBase and must not be re-done or undone here.
        # This image chains to an Image object, so DockerfileEnhancer returns the
        # text verbatim (R9) and injects nothing of its own; what is written below
        # is exactly what gets built.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
{prepare_commands}

{self.clear_env}

"""


@Instance.register("ing-bank", "lion")
class Lion(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Stripped once before the loop, not per line. The marker lines are cat'ed
        # from a plain file and carry no escapes, but @web/test-runner's dynamic
        # terminal writes cursor moves (ESC[2K, ESC[1A, ESC[G) around them in the
        # same stream, and one of those glued to the head of a marker line would
        # stop it matching.
        clean_log = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", test_log)

        # One line per test, emitted by the reporter in node_modules/.msb. Parsing
        # the marker file rather than the runner's own pretty output is what makes
        # this stable: wtr's default reporter prints the suite tree ONLY for files
        # that contain a failure, so a passing file is invisible and every stage
        # would under-report.
        #
        # The name carries no timing, no worker id and no absolute path, so it is
        # byte-identical across stages (R3). Verified on the real three-stage logs:
        # 0 names contain timing/count metadata, and the only baseline name absent
        # from the later stages is one the test patch deletes outright.
        result_re = re.compile(r"^===TEST=== (PASS|FAIL|SKIP) (.+)$")

        for line in clean_log.splitlines():
            m = result_re.match(line.strip())
            if not m:
                continue

            status, name = m.group(1), m.group(2).strip()
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # R2 -- the three sets must be disjoint or TestResult.__post_init__ raises
        # and takes the whole run with it. Failure wins.
        #
        # Names genuinely repeat in this repo: 9 it() titles are duplicated inside
        # their own describe block (e.g. lion-input-tel's "formats according to
        # locale"), so 2836 emitted lines collapse to 2826 buckets. Measured across
        # all three stages, the duplicate count is identical (9/9/9) and no
        # duplicated name is ever emitted with two different statuses, so the
        # collapse is deterministic and cannot manufacture or hide a transition.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
