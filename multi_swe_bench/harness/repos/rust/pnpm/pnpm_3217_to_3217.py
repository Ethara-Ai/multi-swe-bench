from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# ERA A -- PR #3217 (Mar 2021). jest 26 / ts-jest 26 / TypeScript 4.1.5.
#
# pnpm/pnpm spans #3217..#4317 -- 1100 PR numbers and 11 months -- so a single
# config is forbidden by Config-QC 5A. Three sibling era files cover it:
#
#     pnpm_3217_to_3217.py  #3217                          jest 26, packages/supi
#     pnpm_3806_to_3553.py  #3553 #3598 #3795 #3799 #3806   jest 27.0, packages/supi
#     pnpm_4317_to_4014.py  #4014 #4256 #4316 #4317         jest 27.4, packages/core
#
# What separates THIS era:
#   * devDependencies pin jest ^26.6.3, ts-jest ^26.4.4 and typescript 4.1.5 --
#     a full major behind every later era (verified: the container reports
#     `jest --version` = 26.6.3 at this commit)
#   * "engines": {"pnpm": ">=5 || ^0.0.0-x"}; every later base declares ">=6"
#   * the root has NO `compile-only` and NO `pretest` script yet; `test-main` is
#     `pnpm compile && pnpm lint && run-p -r verdaccio test-pkgs-main`
#   * packages/supi's own `compile` is `rimraf lib tsconfig.tsbuildinfo &&
#     tsc --build`, not the later `tsc --build && pnpm run lint -- --fix`
#   * .github/workflows/ci.yml's matrix is node 12.17 / 14 / 15 -- node 16 had
#     not shipped yet. Verified in a throwaway container that the whole chain
#     (pnpm 6.23.0 --frozen-lockfile install, tsc --build, jest 26) runs on
#     node:16.14.0 regardless, which is what lets all three eras share ONE base.
#   * the install engine still lives at packages/supi
#
# WHAT IS IDENTICAL IN ALL THREE ERAS -- and therefore why one base image serves
# them all: node:16.14.0, pnpm 6.23.0 installed with `npm i -g`, a committed
# pnpm-lock.yaml at lockfileVersion 5.3 (checked at all ten base commits), jest
# behind a `preset: "ts-jest"` root jest.config.js, and workspace packages whose
# `main` is `lib/index.js`. Measured in a throwaway node:16.14.0 container at
# #4316's base commit: `pnpm install --frozen-lockfile` = 1m16s for the whole
# 103-package workspace; `tsc --build packages/package-store` = 4m00s cold.
#
# ---------------------------------------------------------------------------
# STACK PROFILE -- every slot resolved by reading the repo at this era's base
# commits and running it, not assumed.
#
#   1 BASE_IMAGE   node:16.14.0  -- Debian bullseye, derives from buildpack-deps
#                                   so git/curl/patch/ca-certificates are already
#                                   present (asserted at build time; no apt
#                                   layer, see D10 note in the Dockerfile).
#   2 PKG_MANAGER  pnpm 6.23.0   -- pnpm-lock.yaml is lockfileVersion 5.3 at
#                                   EVERY base commit in this dataset, and era
#                                   C's package.json names pnpm@6.23.0 in its
#                                   `packageManager` field. Installed with
#                                   `npm install -g pnpm@6.23.0`.
#   3 INSTALL_CMD  pnpm install --frozen-lockfile
#   4 COMPILE_CMD  tsc --build <scoped projects>
#                                -- MANDATORY, not an optimisation. The specs
#                                   import the package under test by its
#                                   published name, e.g.
#                                     import createImportPackage from
#                                       '@pnpm/package-store/lib/storeController/createImportPackage'
#                                   which resolves through the workspace symlink
#                                   to lib/, i.e. to COMPILED JavaScript. A fix
#                                   patch that edits src/ is invisible to jest
#                                   until tsc re-emits. Confirmed end to end:
#                                   at #4316 the gold case is FAILED at the test
#                                   stage and PASSED at the fix stage only
#                                   because each stage re-runs tsc --build.
#   5 TEST_CMD     jest --ci --coverage=false --runInBand --passWithNoTests
#                       --json --outputFile=<per-package report> <gold specs>
#   6 REPORT_DIR   /tmp/msb-reports
#   7 NAME_SHAPE   <repo-relative spec path> > <ancestorTitles...> > <title>
#
# SLOT 5 -- WHY NOT THE REPO'S OWN `_test` SCRIPT. Each package's `_test` is
# either bare `jest` or
# `cross-env PNPM_REGISTRY_MOCK_PORT=<n> pnpm run test:e2e`, where `test:e2e` is
# `registry-mock prepare && run-p -r registry-mock test:jest`. Neither form can
# be handed `--json --outputFile`, and `run-p -r` tears the runner down when the
# registry process exits. The run scripts below therefore replicate exactly what
# `_test` does -- the package's own `pretest`, then @pnpm/registry-mock on the
# port that package's own package.json declares -- and drive jest directly.
# `--coverage=false` overrides the root jest.config.js's `collectCoverage: true`,
# which the repo only wants for its lcov merge.
#
# SLOT 5 -- `--passWithNoTests` IS REQUIRED, NOT DEFENSIVE. Several test patches
# ADD their spec file, so at the `run` stage jest is pointed at a path that does
# not exist yet; without the flag jest exits non-zero with "No tests found"
# BEFORE writing the JSON report, and the stage's report gate would kill the
# instance. #3553 is the extreme case -- both of its gold specs are new.
#
# SLOT 5 -- TEST SCOPE IS THE GOLD SPEC FILES, NOT THE WHOLE PACKAGE. pnpm's
# package suites are end-to-end: nearly every case under packages/supi/test
# performs a real `pnpm install` against a local mock registry. Running one such
# package in full is tens of minutes per stage, three stages per PR, ten PRs,
# two architectures. The scope is therefore exactly the spec files the test patch
# touches (SCOPE below), which keeps the whole f2p signal -- the gold tests are
# by definition in those files -- and makes p2p the other cases in the same
# files. Fixtures (test/fixtures/**) and helpers (test/utils/**) are excluded
# from the scope: they are not specs and jest's own testPathIgnorePatterns
# already drops them.
#
# SLOT 7 IS LOAD-BEARING. The repo-relative path prefix arms
# report._test_name_matches_files' JS/TS branch, which tests
# `test_name.startswith(file + " > ")`; Report.check step 6 only routes a
# (run=NONE, test=NONE, fix=PASS) test into n2p_tests when
# _touched_by_test_patch() is true. #3553's instance is entirely new-file, so
# without the prefix it would grade to zero credited tests despite building and
# running perfectly. ancestorTitles are included because pnpm's specs reuse leaf
# titles across describe blocks in the same file.
# ---------------------------------------------------------------------------

REPO_DIR = "/home/pnpm"
REPORT_DIR = "/tmp/msb-reports"
PNPM_VERSION = "6.23.0"

# Per-PR execution scope, computed from each PR's OWN base commit -- read out of
# the checked-out tree, not guessed:
#
#   test_files : the gold spec files the test patch touches, keyed by package
#                directory and relative to it. These become jest's positional
#                path patterns.
#   registry   : the PNPM_REGISTRY_MOCK_PORT that package's own `_test` script
#                declares, or None when its `_test` is bare `jest`.
#   build_dirs : tsc projects to (re)build after patching -- every package
#                directory touched by either patch that has a tsconfig.json at
#                this base sha. `tsc --build` pulls in each project's own
#                reference closure, so listing the touched projects suffices.
#   fixtures   : whether the gold specs read the repo-root `fixtures/` tree,
#                which has to be materialised by
#                `pnpm --dir=fixtures run prepareFixtures`. Only #4256 does.
SCOPE: dict[int, dict] = {
    3217: {
        "test_files": {"packages/supi": ["test/lockfile.ts"]},
        "registry": {'packages/supi': 4873},
        "build_dirs": ["packages/get-context", "packages/lockfile-file", "packages/supi"],
        "fixtures": False,
    },
}

# The jest JSON -> canonical marker translator. Written by prepare.sh through a
# quoted heredoc rather than shipped as its own File(), because the agreed image
# directory layout is exactly: build_image.log, Dockerfile, check_git_changes.sh,
# fix-run.sh, fix.patch, prepare.sh, run.sh, test-run.sh, test.patch. prepare.sh
# runs `test -s` and `node --check` on it immediately after writing, so a
# truncated or mangled heredoc is a BUILD failure rather than three silently
# empty graded stages.
JEST_REPORT_JS = r"""// Turn every jest JSON report under REPORT_DIR into one canonical line per test:
//
//     MSB-TEST-RESULT|<PASSED|FAILED|SKIPPED>|<repo-relative path> > <describes...> > <title>
//
// The pipe-delimited prefix is used instead of a bare "PASSED <name>" so that no
// line of jest's own console output -- and pnpm's specs print a great deal of it
// -- can ever be mistaken for a result.
const fs = require('fs');
const path = require('path');

const REPORT_DIR = process.argv[2] || '/tmp/msb-reports';
const REPO_ROOT = '/home/pnpm';

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

let files;
try {
  files = fs.readdirSync(REPORT_DIR).filter(function (f) { return f.endsWith('.json'); }).sort();
} catch (e) {
  console.log('MSB-TEST-SUMMARY|raw=0|reports=0|error=' + String(e && e.message));
  process.exit(0);
}

let raw = 0;
let suites = 0;
let emptySuites = 0;
let reports = 0;

for (const f of files) {
  let report;
  try {
    report = JSON.parse(fs.readFileSync(path.join(REPORT_DIR, f), 'utf8'));
  } catch (e) {
    console.log('MSB-TEST-SUMMARY|badreport=' + f + '|error=' + String(e && e.message));
    continue;
  }
  reports++;
  for (const file of report.testResults || []) {
    suites++;
    const rel = relative(file.name);
    const results = file.assertionResults || [];

    if (results.length === 0) {
      // A suite that reported no assertions did not run -- a require or compile
      // error at collection time. That is the EXPECTED test-stage shape when a
      // gold spec imports a module the fix patch creates: jest enumerates
      // nothing, every name in the file reads NONE, and the fix stage turns them
      // into n2p. This file-level marker keeps the collection failure visible in
      // the log instead of letting it look like a clean sweep of zero tests.
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
}

// `raw` is the number of assertionResults jest reported BEFORE parse_log
// de-duplicates into a set. Comparing it against the parsed count is what closes
// Config-QC 4A (name collisions) with a measurement rather than an argument.
console.log(
  'MSB-TEST-SUMMARY|reports=' + reports + '|raw=' + raw +
  '|suites=' + suites + '|emptySuites=' + emptySuites
);
"""


# The shared body of run.sh / test-run.sh / fix-run.sh. Byte-identical across the
# three stages apart from the `git apply` lines, so the same tests run in each --
# Config-QC 3B's "same test command in all 3 scripts".
STAGE_TEMPLATE = """#!/bin/bash
# EXIT-STATUS CONTRACT. A failing suite is the EXPECTED state of the test stage,
# so a bare `set -e` would abort before the reports are read; a bare `|| true` on
# jest would make a runner that never started look like a clean sweep of zero
# tests. So: capture jest's status per package, then gate on the report files
# actually existing before parsing.
set -eo pipefail
export CI=true
# @pnpm/registry-mock proxies anything that is not one of its bundled fixture
# packages to this uplink. The repo's CI points it at a local verdaccio mirror
# (PNPM_REGISTRY_MOCK_UPLINK=http://localhost:7348); there is no mirror in this
# image, so it goes straight to the public registry -- which is what the mock
# does by default when it is run outside the repo's CI.
export PNPM_REGISTRY_MOCK_UPLINK=https://registry.npmjs.org/
export npm_config_registry=https://registry.npmjs.org/

REPO={repo_dir}
REPORTS={report_dir}

cd "$REPO"
rm -rf "$REPORTS"
mkdir -p "$REPORTS"
# Restore tracked files. Untracked build output (lib/, node_modules/,
# .jest-cache, fixtures/*/node_modules) is gitignored and therefore untouched --
# deliberately: it is the warm state prepare.sh built. NEVER add -x here.
git reset --hard --quiet 2>/dev/null || true
{apply}
# Re-emit lib/ for every project either patch touched. Incremental: prepare.sh
# already built the whole closure at the base commit, so only the changed
# projects and their dependents are recompiled.
#
# tsc's exit status is captured, NOT allowed to abort the stage. A test patch
# that references a symbol the fix patch has not created yet is a COMPILE error,
# and that is precisely the state the test stage exists to observe.
set +e
./node_modules/.bin/tsc --build {build_dirs}
TSC_STATUS=$?
set -e
echo "MSB-TSC-EXIT|$TSC_STATUS"
if [ "$TSC_STATUS" -ne 0 ]; then
    # NOT fatal: at the test stage a gold spec that references a symbol only the
    # fix patch creates is SUPPOSED to fail to compile. But a non-zero tsc at the
    # FIX stage means lib/ still holds pre-fix code, the fix is invisible to jest,
    # and test/fix come out identical -- which reads as "the PR changed nothing"
    # rather than "the build broke". Say so explicitly so the cause is in the log.
    echo "MSB-TSC-FAILED|exit=$TSC_STATUS|lib/ may be STALE for this stage" >&2
fi
{extra_build}
run_pkg () {{
    local dir="$1" port="$2" slug="$3"
    shift 3
    cd "$REPO/$dir"
    # The root jest.config.js derives jest's cacheDirectory from
    # PNPM_SCRIPT_SRC_DIR, which pnpm's own script runner normally sets. Left
    # unset, requiring the config throws before a single test runs.
    export PNPM_SCRIPT_SRC_DIR="$PWD"

    # The package's own `pretest`, when it has one -- link-bins copies its
    # fixtures, package-store clears .tmp, list chains to dependencies-hierarchy.
    if node -e "var s=(require('./package.json').scripts)||{{}};process.exit(s.pretest?0:1)"; then
        pnpm run pretest || echo "MSB-PRETEST-FAILED|$dir"
    fi

    local rmpid=""
    if [ -n "$port" ]; then
        export PNPM_REGISTRY_MOCK_PORT="$port"
        local rm_bin="./node_modules/.bin/registry-mock"
        [ -x "$rm_bin" ] || rm_bin="$REPO/node_modules/.bin/registry-mock"
        "$rm_bin" prepare
        "$rm_bin" > "/tmp/registry-mock-$port.log" 2>&1 &
        rmpid=$!
        local up=0
        for _ in $(seq 1 120); do
            if curl -sf "http://localhost:$port/" > /dev/null 2>&1; then up=1; break; fi
            sleep 1
        done
        echo "MSB-REGISTRY|$dir|port=$port|up=$up"
    fi

    set +e
    "$REPO/node_modules/.bin/jest" --ci --coverage=false --runInBand \\
        --passWithNoTests --json --outputFile="$REPORTS/$slug.json" "$@"
    local st=$?
    set -e
    echo "MSB-JEST-EXIT|$dir|$st"

    if [ -n "$rmpid" ]; then
        # @pnpm/registry-mock DOES NOT DIE ON SIGTERM. Observed directly inside a
        # stage container: nine minutes after `kill`, `ps` still showed
        #     PID 46  node .../pnpm-registry-mock.js
        #     PID 62  verdaccio          (its child)
        # so a bare `wait "$rmpid"` blocks forever, run.sh never reaches
        # jest-report.js, PID 1 never exits, the container never stops, and the
        # harness waits on it indefinitely -- with the tests already finished and
        # the JSON report already on disk. Six of this dataset's ten instances
        # use the mock, so this hangs the entire run.
        #
        # Ask nicely, briefly, then SIGKILL the tree. Never block on `wait`.
        kill "$rmpid" 2>/dev/null || true
        for _ in 1 2 3; do
            kill -0 "$rmpid" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$rmpid" 2>/dev/null || true
        pkill -9 -f "pnpm-registry-mock" 2>/dev/null || true
        pkill -9 -f "verdaccio" 2>/dev/null || true
    fi
    cd "$REPO"
}}

{run_calls}

FOUND=$(ls "$REPORTS"/*.json 2>/dev/null | wc -l)
if [ "$FOUND" -ne {n_reports} ]; then
    echo "FATAL: expected {n_reports} jest report(s), found $FOUND -- the runner never started" >&2
    exit 1
fi

node /home/jest-report.js "$REPORTS"
"""

# Emitted into the stage scripts only for the PRs that need it (see SCOPE).
# packages/pnpm's own `compile` script ends in `pnpm run lint -- --fix`, which
# REWRITES source files and would leave the tree dirty; the two steps that
# actually produce dist/pnpm.cjs are run directly instead.
BUNDLE_BLOCK = """
# packages/pnpm is in this PR's build set, so the bundled CLI has to be
# regenerated too -- lib/*.js alone is not what the e2e specs execute.
pnpm --filter=pnpm run bundle || echo "MSB-BUNDLE-FAILED"
"""

FIXTURES_BLOCK = """
# This PR's gold spec reads the repo-root fixtures/ tree, whose node_modules are
# NOT committed. prepareFixtures shells out to packages/pnpm/dist/pnpm.cjs, which
# the bundle step above has just rebuilt. It is re-run per stage rather than only
# in prepare.sh because this PR's FIX patch adds a new fixture workspace, so the
# fix stage needs a fixture directory that does not exist at the base commit.
pnpm --dir=fixtures run prepareFixtures || echo "MSB-FIXTURES-FAILED"
"""

PREPARE_TEMPLATE = """#!/bin/bash
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

# pnpm 6.23.0 is what era C's package.json names in `packageManager`, and it
# reads the lockfileVersion 5.3 lockfile that every base commit in this dataset
# commits. Installed globally with npm, so no corepack dance is needed.
npm install -g pnpm@{pnpm_version} --loglevel=error || true
pnpm --version

export CI=true
export npm_config_registry=https://registry.npmjs.org/

# pnpm 6 enforces `engines.pnpm` UNCONDITIONALLY, and #4014's package.json
# declares "engines": {{"pnpm": ">=7"}} -- wrong in the repo itself, since pnpm 7
# did not exist until 2022-05 and the committed lockfile is still 5.3. Left in
# place it aborts every install with ERR_PNPM_UNSUPPORTED_ENGINE. Measured, not
# assumed: NEITHER `--config.engine-strict=false` NOR
# `npm_config_engine_strict=false` suppresses the check -- both were run against
# #4014's base commit in a container built from this very base image and both
# still errored. The only thing that works is removing the field for the
# duration of the install and restoring the manifest immediately afterwards.
# Only the manifest is touched, never a source file, and the
# `git checkout -- package.json` below plus check_git_changes.sh at the end
# re-prove the tree is pristine.
node -e '
  const fs = require("fs"), p = "{repo_dir}/package.json";
  const j = JSON.parse(fs.readFileSync(p, "utf8"));
  if (j.engines && j.engines.pnpm) {{
    delete j.engines.pnpm;
    fs.writeFileSync(p, JSON.stringify(j, null, 2));
    console.log("prepare: engines.pnpm temporarily removed for the install");
  }}
'

# `|| true` is required by the rubric on the install, and on its own it would
# ALSO swallow a total install failure and ship an image with no node_modules --
# all three stages would then die identically and parse to (0,0,0) with nothing
# in the log naming the cause. The retry loop plus the hard `test -x` gates below
# turn that into a loud BUILD failure instead.
for attempt in 1 2 3; do
    pnpm install --frozen-lockfile --reporter=append-only && break
    echo "prepare: pnpm install attempt $attempt failed, retrying"
    sleep 15
done || true

# Restore every TRACKED file the install touched, before anything else looks at
# the tree. Two distinct things need this and both are install side effects, not
# source changes:
#
#   1. the engines.pnpm edit above;
#   2. packages/beta's own preinstall/prepare lifecycle scripts (`node setup.js`
#      / `node prepare.js`) REWRITE the tracked file packages/beta/pnpm. At
#      #3553 and #3598 that file is committed EMPTY (git blob e69de29), so the
#      write dirties it and the final check_git_changes.sh rejects the image; by
#      #3795 the committed content already equals what the script writes, which
#      is why those PRs built clean. Observed, not theorised -- both failures
#      reported exactly `M packages/beta/pnpm`.
#
# Build output is untouched by this: lib, dist, tsconfig.tsbuildinfo and
# **/node_modules/** are all in the repo's .gitignore, and `git checkout -- .`
# only restores tracked paths. The final check_git_changes.sh is still the proof
# -- if a later step dirties the tree, the build should and does fail loudly.
git checkout -- .

test -x {repo_dir}/node_modules/.bin/jest
test -x {repo_dir}/node_modules/.bin/tsc
test -x {repo_dir}/node_modules/.bin/registry-mock

# Warm the whole tsc reference closure of everything this PR touches, at the BASE
# commit. The graded stages then only pay for the projects a patch actually
# changed. Failures are tolerated here for the same reason as in the stages, but
# the `test -d .../lib` gates below turn a closure that produced nothing into a
# build failure.
set +e
./node_modules/.bin/tsc --build {build_dirs}
echo "prepare: tsc --build exit $?"
set -e
{lib_gates}
# PRE-SEED THE DEPENDENCIES THE FIX PATCH INTRODUCES.
#
# A fix patch may add a WORKSPACE dependency that does not exist at the base
# commit. #3795 does exactly that:
#     packages/link-bins/package.json  + "@pnpm/manifest-utils": "workspace:2.0.4"
#     packages/link-bins/tsconfig.json + {{ "path": "../manifest-utils" }}
# `pnpm install` ran at the base commit, so packages/link-bins/node_modules/
# @pnpm/manifest-utils does not exist. At the fix stage TypeScript then cannot
# resolve the import:
#     packages/link-bins/src/index.ts(5,48): error TS2307:
#       Cannot find module '@pnpm/manifest-utils'
# tsc exits non-zero, lib/ keeps the PRE-FIX code, and the test and fix stages
# come out byte-identical -- the instance is rejected with nothing in the log
# naming the cause. Observed on #3795 and #3598 before this block existed.
#
# So: apply ONLY the fix patch's manifests, install so the new workspace links
# are created on disk, then restore the tree. Only dependency links leak, never
# source -- and pnpm leaves the symlinks in place after the manifest is
# reverted, which is the whole point. check_git_changes.sh at the end re-proves
# the tree is pristine.
if git apply --check --whitespace=nowarn \
        --include='*package.json' --include='pnpm-lock.yaml' \
        /home/fix.patch 2>/dev/null; then
    echo "prepare: pre-seeding workspace deps from fix.patch manifests"
    git apply --whitespace=nowarn \
        --include='*package.json' --include='pnpm-lock.yaml' \
        /home/fix.patch
    pnpm install --frozen-lockfile --reporter=append-only \
        || pnpm install --reporter=append-only \
        || true
    git checkout -- .
    # `git checkout -- .` restores TRACKED files but never removes untracked
    # ones, and the include filter above also matches manifests the fix patch
    # CREATES. #4256's fix adds a whole fixture workspace
    # (fixtures/workspace-with-private-pkgs/{{package.json,pnpm-lock.yaml,...}}),
    # which then survives as `?? fixtures/workspace-with-private-pkgs/` and fails
    # the final check_git_changes.sh -- observed as a hard build failure.
    # Delete exactly the files the filtered patch added, identified as the ones
    # git does not track. Deliberately NOT `git clean -fd`: node_modules is
    # matched by .gitignore as `**/node_modules/**`, so the DIRECTORY itself may
    # not read as ignored and a clean could wipe the install we just did.
    git apply --numstat --whitespace=nowarn \
        --include='*package.json' --include='pnpm-lock.yaml' \
        /home/fix.patch 2>/dev/null \
      | awk '{{print $3}}' \
      | while read -r f; do
            [ -n "$f" ] || continue
            git ls-files --error-unmatch "$f" > /dev/null 2>&1 || rm -f "$f"
        done
    echo "prepare: dependency pre-seed done, manifests reverted"
else
    echo "prepare: fix.patch manifests do not apply in isolation; no pre-seed"
fi

{extra_prepare}
# Seed the mock registry's storage once at build time, so the graded stages do
# not each pay for it and so a broken mock is a build failure rather than a
# mystery three stages later.
{registry_prepare}

node --version
./node_modules/.bin/jest --version

bash /home/check_git_changes.sh
"""

CHECK_GIT_SH = """#!/bin/bash
# Assert the working tree is pristine. `git reset --hard` restores tracked files
# but does not remove stray untracked ones, and the Dockerfile's HEAD/refs asserts
# only prove WHICH commit is checked out -- a dirty tree satisfies all of them.
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


class PnpmImageBase(Image):
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
        # ONE base for every era. node:16.14.0 is bullseye and ships npm 8. It was
        # exercised in a throwaway container against BOTH ends of the dataset --
        # era A's jest 26 / ts-jest 26 / TypeScript 4.1.5 tree at #3217 and era
        # C's jest 27.4 tree at #4316. The repo's ci.yml never tests node 16 at
        # #3217 (that matrix stops at 15) and never tests node 14 at #4317 (that
        # matrix starts at 16); 16 is the only version inside every era's window
        # that is also an LTS.
        return "node:16.14.0"

    def image_tag(self) -> str:
        # Deliberately identical in all three era files so the eras dedupe onto
        # ONE built base image. Safe only because this whole class -- and hence
        # the rendered base Dockerfile -- is byte-identical across those files;
        # the image carries no commit and no dependencies, only the clone, so
        # there is nothing for the dedupe race to get wrong. Same pattern, and
        # same reasoning, as repos/python/pwndbg.
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # HAND-WRITTEN IN FULL, on purpose.
        #
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this string VERBATIM (image.py). That is the whole point: it
        # stops _inject_final_sanitize from appending its
        # `git checkout --detach "${BASE_COMMIT}"` + gc-prune block, which would
        # pin this SHARED base to whichever PR the builder happened to dedupe to
        # and break the other nine with `fatal: reference is not a tree`.
        # Precedent: repos/python/pwndbg, repos/typescript/tldraw,
        # repos/typescript/remix_run.
        #
        # Because the enhancer is skipped, everything it would normally supply is
        # written out below by hand, in the reference Dockerfile's order: syntax
        # directive, pinned FROM, TARGETARCH/REPO_URL, the six proxy ARGs,
        # CA_CERT_PATH, one ENV block, OCI labels, then the CA symlink farm
        # BEFORE the first network call.
        #
        # Nothing follows the clone except CMD -- no WORKDIR into the repo, no
        # checkout, no history scrub. Those belong to the PR layer (house
        # convention, 2026-09-02), and live in _harden() below.
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
    CI=true \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
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
# and already ships git, curl, patch, tar, ca-certificates and coreutils -- D10
# explicitly permits an absent apt block for the official node images, and
# Image._get_apt_update_command's archive rewrite only matches bases literally
# named debian:*/gcc:* anyway. Assert the tools instead, so a missing one is a
# build failure on both architectures rather than a mystery mid-run.
RUN set -eux; \\
    command -v git; \\
    command -v curl; \\
    command -v patch; \\
    command -v tar; \\
    node --version; \\
    npm --version; \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class PnpmImageDefault(Image):
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
        return PnpmImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # ---- scope helpers ----------------------------------------------------

    def _scope(self) -> dict:
        return SCOPE[self.pr.number]

    def _build_dirs(self) -> str:
        return " ".join(self._scope()["build_dirs"])

    @staticmethod
    def _slug(pkg_dir: str) -> str:
        return pkg_dir.replace("/", "__")

    def _run_calls(self) -> str:
        scope = self._scope()
        lines = []
        for pkg_dir, spec_files in scope["test_files"].items():
            port = scope["registry"].get(pkg_dir) or ""
            args = " ".join(f'"{f}"' for f in spec_files)
            lines.append(f'run_pkg "{pkg_dir}" "{port}" "{self._slug(pkg_dir)}" {args}')
        return "\n".join(lines)

    def _extra_build(self) -> str:
        scope = self._scope()
        out = ""
        if "packages/pnpm" in scope["build_dirs"]:
            out += BUNDLE_BLOCK
        if scope["fixtures"]:
            out += FIXTURES_BLOCK
        return out

    def _stage(self, apply_lines: str) -> str:
        return STAGE_TEMPLATE.format(
            repo_dir=REPO_DIR,
            report_dir=REPORT_DIR,
            apply=apply_lines,
            build_dirs=self._build_dirs(),
            extra_build=self._extra_build(),
            run_calls=self._run_calls(),
            n_reports=len(self._scope()["test_files"]),
        )

    def files(self) -> list[File]:
        # EXACTLY the files the agreed image-directory layout allows:
        #   build_image.log  Dockerfile  check_git_changes.sh  fix-run.sh
        #   fix.patch  prepare.sh  run.sh  test-run.sh  test.patch
        # The jest JSON translator is written at build time by prepare.sh.
        scope = self._scope()

        lib_gates = "\n".join(
            f'test -d "{d}/lib" || {{ echo "prepare: FATAL {d}/lib was never emitted" >&2; exit 1; }}'
            for d in scope["test_files"]
        )
        ports = sorted({p for p in scope["registry"].values() if p})
        if ports:
            registry_prepare = "\n".join(
                f"PNPM_REGISTRY_MOCK_PORT={p} ./node_modules/.bin/registry-mock prepare"
                for p in ports
            )
        else:
            registry_prepare = (
                "# This PR's packages run bare `jest`; no registry mock is involved."
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_SH),
            File(
                ".",
                "prepare.sh",
                PREPARE_TEMPLATE.format(
                    repo_dir=REPO_DIR,
                    sha=self.pr.base.sha,
                    jest_report_js=JEST_REPORT_JS,
                    pnpm_version=PNPM_VERSION,
                    build_dirs=self._build_dirs(),
                    lib_gates=lib_gates,
                    extra_prepare=self._extra_build(),
                    registry_prepare=registry_prepare,
                ),
            ),
            File(".", "run.sh", self._stage("# Baseline: no patches applied.")),
            File(
                ".",
                "test-run.sh",
                self._stage(
                    "# Verified against the real base trees before this config was written:\n"
                    "# test.patch applies clean at all ten base commits, in both orders, and no\n"
                    "# patch in this dataset carries a binary hunk -- checked for `GIT binary\n"
                    "# patch` payloads AND for the payload-less `Binary files ... differ` form,\n"
                    "# which is the nastier one because `git apply` is ATOMIC and one such hunk\n"
                    "# silently drops every text hunk with it.\n"
                    "git apply --whitespace=nowarn /home/test.patch"
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                self._stage(
                    "# test.patch FIRST, then fix.patch -- the order Report.check() assumes.\n"
                    "# Two invocations rather than one so a failure names which patch failed.\n"
                    "git apply --whitespace=nowarn /home/test.patch\n"
                    "git apply --whitespace=nowarn /home/fix.patch"
                ),
            ),
        ]

    def _harden(self) -> str:
        """Git-history stripping for the PR image, applied AFTER prepare.sh has
        checked out THIS PR's base commit -- so the commit to KEEP is the current
        HEAD. Mirrors the harness Image._HARDENING_BLOCK, anchored on HEAD rather
        than ${BASE_COMMIT}.

        This is also the leak control. The base image cloned pnpm/pnpm at its
        default branch, so before this runs the tree contains every commit in the
        repository -- including this PR's own merged fix. Deleting every ref and
        expiring the reflog makes all of that unreachable from HEAD; prune and
        repack then destroy it.
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

        # LAYER ORDER IS DELIBERATE. Everything prepare.sh itself reads -- the
        # two patches and check_git_changes.sh -- is COPY'd before it; the three
        # graded stage scripts are COPY'd AFTER it. They are the files most
        # likely to need a fix, and `RUN bash /home/prepare.sh` is by far the
        # most expensive layer (pnpm install + the whole tsc reference closure,
        # ~10 min). With the stage scripts ahead of it, editing one line of
        # run.sh invalidates the install and recompiles everything; behind it,
        # the install stays a cache hit and only the cheap COPY plus the
        # hardening layer re-run. All seven files are still COPY'd, and
        # prepare.sh is still RUN exactly once.
        pre_prepare = {"fix.patch", "test.patch", "check_git_changes.sh", "prepare.sh"}
        copy_before = ""
        copy_after = ""
        for file in self.files():
            line = f"COPY {file.name} /home/\n"
            if file.name in pre_prepare:
                copy_before += line
            else:
                copy_after += line

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_before}
RUN bash /home/prepare.sh

{copy_after}

{self._harden()}

{self.clear_env}
"""


@Instance.register("pnpm", "pnpm_3217_to_3217")
class PNPM_3217_TO_3217(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PnpmImageDefault(self.pr, self._config)

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
        # ANSI first. jest still colours its console output under --ci, and pnpm's
        # own reporters emit escape sequences throughout the captured stage log.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Only the canonical markers jest-report.js emits are matched -- never
        # jest's or pnpm's own console output -- so no summary line can be counted
        # as a test. The name group is greedy to end-of-line so a title containing
        # "|" survives intact, and it carries no timing or count metadata, which
        # is what keeps names identical across the three stages (Config-QC 4B).
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

        # A name may live in exactly one bucket, and failure wins. The later eras'
        # jest.setup.js sets `jest.retryTimes(1)`, so a flaky case really can be
        # reported twice with different statuses; this ordering makes such a
        # collapse understate credit, never manufacture a false pass.
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
