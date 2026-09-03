from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Stack profile — every slot below was resolved by reading the repo at the three
# viable base commits, not assumed.
#
#   1 BASE_IMAGE   node:18.12.1  -- package.json "engines": {"node": "18.12.x"}
#                                   at ALL of e72abe1b / b6705aca / 09ebc191, and
#                                   .github/workflows/nodejs.yml pins
#                                   `node-version: 18` on ubuntu-22.04.
#   2 APT_PKGS     none          -- the full `node` image derives from
#                                   buildpack-deps and already ships git,
#                                   ca-certificates, curl, patch and coreutils.
#                                   Asserted at build time instead (see below).
#   3 INSTALL_CMD  npm ci        -- package-lock.json is lockfileVersion 3, and
#                                   node:18.12.1 ships npm 8, which consumes it
#                                   natively. Same command CI runs.
#   4 TEST_CMD     jest 29       -- NOT `npm test`. package.json defines
#                                   "test": "npm run lint && npm run build && jest",
#                                   which would run eslint and a full webpack
#                                   production build before a single test. Lint
#                                   state is not what we are grading (PR #2413 is
#                                   literally an automated-lint PR), and a lint
#                                   failure would abort the stage before jest
#                                   ever ran. The repo's own "jest" script is the
#                                   bare runner; we invoke the binary directly.
#   5 REPORT_FLAG  --json --outputFile=
#   6 REPORT_FILE  /tmp/jest-report.json
#   7 NAME_SHAPE   <repo-relative spec path> > <ancestorTitles...> > <title>
#
# SLOT 7 IS LOAD-BEARING, FOR TWO INDEPENDENT REASONS.
#
# (a) Duplicate leaf titles. src/__tests__/utility/pointfacade.ts carries 25 leaf
#     tests under only 19 distinct titles — six titles appear twice, because the
#     spec repeats the same assertion name under different describe blocks:
#
#         "return [creature] if {x, y} has creature"   (getCreaturesAt, x2)
#         "return [] if {x, y} has no creature"        (getCreaturesAt, x2)
#         "return [Trap] if {x, y} has Trap"           (getTrapsAt,     x2)
#         "return [] if {x, y} has no Trap"            (getTrapsAt,     x2)
#         "return [Drop] if {x, y} has Drop"           (getDropsAt,     x2)
#         "return [] if {x, y} has no Drop"            (getDropsAt,     x2)
#
#     That file IS the gold test file for PR #2270. A leaf-title-only key would
#     collapse 25 results onto 19 names and silently under-credit the instance.
#     With ancestorTitles included, full-path collisions across every spec file
#     in the repo measure ZERO.
#
# (b) The path prefix arms report._test_name_matches_files' JS/TS branch, which
#     tests `test_name.startswith(file + " > ")`. Report.check step 6 routes a
#     (run=NONE, test=NONE, fix=PASS) test to n2p_tests ONLY when
#     _touched_by_test_patch() returns true; otherwise it lands in
#     fix_patch_authored_candidates and earns nothing. PRs #2270 and #2344 are
#     entirely n2p, so without the path prefix both instances would grade to
#     zero credited tests despite building and running perfectly.
# ---------------------------------------------------------------------------

REPO_DIR = "/home/AncientBeast"
JEST_REPORT = "/tmp/jest-report.json"

# ---------------------------------------------------------------------------
# LAYER SPLIT — base clones, PR layer hardens. (House convention, 2026-09-02.)
#
#   BASE  : infrastructure + toolchain asserts + `git clone`, then CMD. Nothing
#           after the clone. No BASE_COMMIT, no checkout, no history scrub, so
#           ONE base image serves every PR of this repo.
#   PR    : COPY the seven files -> RUN prepare.sh (checkout + install) -> then
#           the git-stripping / hardening block, as Dockerfile RUN directives.
#
# The mechanism that makes this possible is the `# syntax` directive at the top
# of the base file. DockerfileEnhancer.enhance() returns the raw string UNCHANGED
# the moment it sees one (image.py), which is what stops
# _inject_final_sanitize from appending its own
# `git checkout --detach "${BASE_COMMIT}"` + gc-prune block — an injection that
# would pin the shared base to a single PR's commit and break every other PR with
# `fatal: reference is not a tree`. Same trick as typescript/tldraw and
# typescript/remix_run, and their comments say the same thing.
#
# The price of skipping the enhancer is that the base must WRITE ITS OWN
# infrastructure: the syntax directive, TARGETARCH/REPO_URL, the six proxy ARGs,
# CA_CERT_PATH, the single ENV block, the OCI labels and the CA symlink farm.
# All of that is hand-written below, in the reference Dockerfile's order, so
# Dockerfile-QC D1-D8 still pass.
#
# Hardening lives in the PR Dockerfile (ImageDefault._harden), anchored on HEAD
# rather than ${BASE_COMMIT}, because prepare.sh has already checked out this
# PR's commit by the time it runs. That is also what destroys every commit newer
# than this PR's base, so no image ships a later PR's merged fix.
# ---------------------------------------------------------------------------

# --maxWorkers=1 keeps timing deterministic. A test that flips PASS(test) ->
#   FAIL(fix) invalidates the whole instance under Report.check step 2, so
#   trading wall-clock for flake resistance is the right side of that bet.
# --forceExit guards against a leaked handle hanging the stage after the last
#   suite: creature.ts pulls in jquery, phaser-ce and Game, none of which are
#   written for a headless teardown. jest writes --outputFile inside runCLI's
#   processResults, BEFORE the CLI acts on forceExit, so the report is always on
#   disk by the time the process is killed.
# --ci is set for parity with a CI run; the repo has no snapshots for it to gate.
TEST_CMD = (
    "./node_modules/.bin/jest --ci --forceExit --maxWorkers=1 "
    f"--json --outputFile={JEST_REPORT}"
)


# The jest JSON -> canonical marker translator. It is WRITTEN BY prepare.sh via a
# quoted heredoc rather than shipped as its own File(), because the agreed folder
# layout for an image directory is exactly: build_image.log, Dockerfile,
# check_git_changes.sh, fix-run.sh, fix.patch, prepare.sh, run.sh, test-run.sh,
# test.patch. prepare.sh runs `node --check` and `test -s` on it immediately
# after writing, so a truncated or mangled heredoc is a BUILD failure rather than
# three silently empty graded stages.
JEST_REPORT_JS = r"""// Turn jest's JSON report into one canonical line per test:
//
//     MSB-TEST-RESULT|<PASSED|FAILED|SKIPPED>|<repo-relative path> > <describes...> > <title>
//
// The pipe-delimited prefix is used instead of a bare "PASSED <name>" so that no
// line of jest's own console output can ever be mistaken for a result.
//
// Names carry ancestorTitles because src/__tests__/utility/pointfacade.ts has six
// duplicate leaf titles (see SLOT 7 at the top of the config), and are prefixed
// with the repo-relative path because report._test_name_matches_files' JS/TS
// branch tests `startswith(file + " > ")` -- which is what lets the two n2p-only
// PRs (#2270, #2344) be credited at all.
const fs = require('fs');
const path = require('path');

const REPORT_FILE = '/tmp/jest-report.json';
const REPO_ROOT = '/home/AncientBeast';

let report;
try {
  report = JSON.parse(fs.readFileSync(REPORT_FILE, 'utf8'));
} catch (e) {
  console.log('MSB-TEST-SUMMARY|raw=0|files=0|error=' + String(e && e.message));
  process.exit(0);
}

const STATUS = {
  passed: 'PASSED',
  failed: 'FAILED',
  pending: 'SKIPPED',
  skipped: 'SKIPPED',
  todo: 'SKIPPED',
  disabled: 'SKIPPED'
};

function relative(name) {
  const p = String(name || '');
  if (p.indexOf(REPO_ROOT + '/') === 0) return p.slice(REPO_ROOT.length + 1);
  return path.relative(REPO_ROOT, p).split(path.sep).join('/');
}

let raw = 0;
let files = 0;
let emptySuites = 0;

for (const file of report.testResults || []) {
  files++;
  const rel = relative(file.name);
  const results = file.assertionResults || [];

  if (results.length === 0) {
    // A suite that reported no assertions did not run -- a require/parse error at
    // collection time. This is the EXPECTED test-stage shape for #2270 and #2344,
    // whose gold specs import a module the fix patch creates: the import throws,
    // jest enumerates nothing, and every test in the file reads NONE -> PASS,
    // i.e. n2p. The file-level marker keeps that visible in the log.
    if (file.status !== 'passed') {
      emptySuites++;
      console.log('MSB-TEST-RESULT|FAILED|' + rel + ' > <suite reported no tests>');
    }
    continue;
  }

  for (const t of results) {
    raw++;
    const status = STATUS[t.status] || 'SKIPPED';
    const name = rel + ' > ' + (t.ancestorTitles || []).concat([t.title || '']).join(' > ');
    console.log('MSB-TEST-RESULT|' + status + '|' + name);
  }
}

// `raw` is the number of assertionResults jest reported BEFORE the parser
// de-duplicates into a set. Comparing it against the parsed count is what closes
// Config-QC 4A (name collisions) with a measurement rather than an argument.
console.log(
  'MSB-TEST-SUMMARY|raw=' + raw + '|files=' + files + '|emptySuites=' + emptySuites
);
"""


class AncientBeastImageBase(Image):
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
        return "node:18.12.1"

    def image_tag(self) -> str:
        # ONE tag for the whole repo. Safe unconditionally: this image carries no
        # repository and no commit, so there is nothing for the dedupe race to
        # get wrong. See the note above.
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # HAND-WRITTEN IN FULL, on purpose.
        #
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this string VERBATIM. That is the whole point: it stops
        # _inject_final_sanitize from appending its checkout + gc-prune block,
        # which would pin this SHARED base to one PR's BASE_COMMIT and break
        # every other PR. See the layer-split note at the top of the file.
        #
        # Because the enhancer is skipped, everything it would normally supply is
        # written out below by hand, in the reference Dockerfile's order:
        # syntax directive, pinned FROM, TARGETARCH/REPO_URL, the six proxy ARGs,
        # CA_CERT_PATH, one ENV block, OCI labels, then the CA symlink farm
        # BEFORE the first network call.
        #
        # Nothing follows the clone except CMD — no WORKDIR into the repo, no
        # checkout, no scrub. Those belong to the PR layer.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org, repo = self.pr.org, self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

# No apt layer, deliberately. The full `node` image derives from buildpack-deps
# and already ships git, ca-certificates, curl, patch and coreutils — D10
# explicitly permits a minimal or absent apt block for official node images, and
# node:18.12.1 is Debian bullseye whose archives Image._get_apt_update_command
# would not rewrite anyway. Assert the tools instead so a missing one fails at
# build time rather than midway through a graded run.
RUN set -eux; \\
    command -v git; \\
    command -v curl; \\
    command -v patch; \\
    node --version; \\
    npm --version; \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class AncientBeastImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return AncientBeastImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # EXACTLY the seven files the agreed image-directory layout allows.
        # Anything else a stage needs is written at build time by prepare.sh and
        # verified there, so the shipped directory stays:
        #   build_image.log  Dockerfile  check_git_changes.sh  fix-run.sh
        #   fix.patch  prepare.sh  run.sh  test-run.sh  test.patch
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
# Assert the working tree is pristine. `git reset --hard` restores tracked files
# but does not remove stray untracked ones, and the Dockerfile's HEAD/refs asserts
# only prove WHICH commit is checked out -- a dirty tree satisfies all of them.
# Also re-proves that prepare.sh's dependency pre-seed left no source change.
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd {repo_dir}
git reset --hard
git checkout --detach {sha}
bash /home/check_git_changes.sh

cat > /home/jest-report.js <<'MSBJSEOF'
{jest_report_js}
MSBJSEOF
test -s /home/jest-report.js
node --check /home/jest-report.js
echo "prepare: jest-report.js written and syntax-checked"

npm ci --no-audit --no-fund --loglevel=error \\
    || npm ci --no-audit --no-fund --ignore-scripts --loglevel=error \\
    || npm install --no-audit --no-fund --ignore-scripts --loglevel=error \\
    || true

if grep -q '^diff --git a/package\\.json' /home/fix.patch; then
    echo "prepare: pre-seeding dependencies from fix.patch manifests"
    if git apply --check --whitespace=nowarn \\
            --include='package.json' --include='package-lock.json' \\
            /home/fix.patch 2>/dev/null; then
        git apply --whitespace=nowarn \\
            --include='package.json' --include='package-lock.json' \\
            /home/fix.patch
        npm install --no-audit --no-fund --loglevel=error || true
        git checkout -- package.json package-lock.json
        echo "prepare: dependency pre-seed done, manifests reverted"
    else
        echo "prepare: fix.patch manifests do not apply in isolation; skipping pre-seed"
    fi
else
    echo "prepare: fix.patch touches no manifest; no pre-seed needed"
fi

bash /home/check_git_changes.sh

test -x ./node_modules/.bin/jest
test -f jest.config.js

node --version
npm --version
./node_modules/.bin/jest --version
""".format(
                    repo_dir=REPO_DIR,
                    sha=self.pr.base.sha,
                    jest_report_js=JEST_REPORT_JS,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# Baseline: no patches. See the exit-status contract note in test-run.sh; all
# three scripts are byte-identical apart from the patch application.
set -eo pipefail
export CI=true

cd {repo_dir}
rm -f {report}
git reset --hard --quiet 2>/dev/null || true

set +e
{test_cmd}
JEST_STATUS=$?
set -e

if [ ! -s {report} ]; then
    echo "FATAL: jest wrote no JSON report (exit $JEST_STATUS) -- the runner never started" >&2
    exit 1
fi

node /home/jest-report.js
""".format(repo_dir=REPO_DIR, report=JEST_REPORT, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
# EXIT-STATUS CONTRACT. A failing suite is the EXPECTED state of this stage -- the
# whole point is that the gold tests fail before the fix. Under a bare `set -e`
# jest's non-zero exit would abort before the report is read; under a bare
# `|| true` a runner that never started would look like a clean sweep of zero
# tests. So: capture the status explicitly, then gate on the report file.
set -eo pipefail
export CI=true

cd {repo_dir}
rm -f {report}
git reset --hard --quiet 2>/dev/null || true

# Verified against the real base trees before this config was written: test.patch
# applies clean at every PR's base commit, and none of the dataset's patches
# carries a binary hunk -- checked for both `GIT binary patch` payloads and the
# payload-less `Binary files ... differ` form, which is the nastier case because
# `git apply` is ATOMIC and one such hunk silently drops every text hunk with it.
git apply --whitespace=nowarn /home/test.patch

set +e
{test_cmd}
JEST_STATUS=$?
set -e

if [ ! -s {report} ]; then
    echo "FATAL: jest wrote no JSON report (exit $JEST_STATUS) -- the runner never started" >&2
    exit 1
fi

node /home/jest-report.js
""".format(repo_dir=REPO_DIR, report=JEST_REPORT, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd {repo_dir}
rm -f {report}
git reset --hard --quiet 2>/dev/null || true

# test.patch FIRST, then fix.patch -- the order Report.check() assumes. Applied as
# two invocations rather than one so a failure names which patch failed; verified
# clean in both orders against the real base trees.
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch

set +e
{test_cmd}
JEST_STATUS=$?
set -e

if [ ! -s {report} ]; then
    echo "FATAL: jest wrote no JSON report (exit $JEST_STATUS) -- the runner never started" >&2
    exit 1
fi

node /home/jest-report.js
""".format(repo_dir=REPO_DIR, report=JEST_REPORT, test_cmd=TEST_CMD),
            ),
        ]

    def _harden(self) -> str:
        """Git-history stripping for the PR image, applied AFTER prepare.sh has
        checked out THIS PR's base commit -- so the commit to KEEP is the current
        HEAD. Mirrors the harness Image._HARDENING_BLOCK, anchored on HEAD rather
        than ${BASE_COMMIT}.

        This is also the leak control. The base image cloned the repository at its
        default branch, so before this runs the tree contains every commit in the
        repo -- including this PR's own merged fix. Deleting every ref and
        expiring the reflog makes everything unreachable from HEAD, and
        prune + repack destroy it.
        """
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

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

{self._harden()}

{self.clear_env}
"""


@Instance.register("FreezingMoon", "AncientBeast")
class AncientBeast(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AncientBeastImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Only the canonical markers jest-report.js emits are matched — never
        # jest's own console output — so a summary line can never be counted as a
        # test. The name group is greedy to the end of line so a title containing
        # "|" survives intact.
        result_re = re.compile(r"^MSB-TEST-RESULT\|(PASSED|FAILED|SKIPPED)\|(.+)$")

        for raw in log.splitlines():
            m = result_re.match(raw.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2).strip()
            if not name:
                continue
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A name may live in exactly one bucket, and failure wins. If two raw
        # entries ever collapsed onto one key with disagreeing statuses, this
        # ordering makes the collapse understate credit — it can never
        # manufacture a false pass.
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
