from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# jestjs/jest -- PRs #8890 .. #10293  (jest 24.9 .. 26.6, 2019-09 .. 2020-11)
# ---------------------------------------------------------------------------
#
# WHY THIS BUNDLE EXISTS
# ----------------------
# repos/typescript/jestjs/jest.py already owned the bare key "jestjs/jest", but
# it is written for the 2016 codebase: node:10-buster, npm, `lerna bootstrap`,
# and `node ./scripts/build.js` against a Lerna monorepo that predates yarn
# workspaces. Every PR in THIS dataset is from 2019-2020, where the repo is a
# yarn-workspaces monorepo whose yarn binary is checked into the tree. Running
# the 2016 recipe here produces `lerna: not found` and an empty log, i.e. a
# 0/0/0 TestResult that Report.check() rejects.
#
# The dataset carries no `number_interval` and no `tag`, so Instance.create()
# (instance.py:41-48) resolves the bare "jestjs/jest" for all 20 rows. jest.py
# is therefore now a thin dispatcher that routes #8890..#10293 to this module
# and leaves every other PR number on the legacy 2016 implementation, so both
# datasets keep working off one registration key.
#
# ---------------------------------------------------------------------------
# TWO SUB-ERAS, AND WHY THEY ARE **NOT** SPLIT INTO TWO CONFIG FILES
# ---------------------------------------------------------------------------
# The 20 rows straddle jest's Yarn-1 -> Yarn-2 ("berry") migration, verified by
# checking out every `base.sha` in the JSONL and reading the tree:
#
#   sub-era A (16 PRs)  .yarnrc  -> yarn-path ".yarn/releases/yarn-1.17.3.js"
#                       package.json has a `postinstall` that runs `yarn build`
#                       CI: node scripts/remove-postinstall
#                           && yarn --frozen-lockfile --ignore-engines
#                           && node scripts/build
#
#   sub-era B (4 PRs)   .yarnrc.yml -> yarnPath .yarn/releases/yarn-sources.cjs
#                       (yarn 2.1.1), no postinstall, `build:js` script
#                       CI: yarn install && yarn build:js
#
# A PR-number range CANNOT separate them. Sorted by number the eras interleave:
#
#      #9291 A   (base.sha b2c8a69e, 2019-12-09)
#      #9326 B   (base.sha 96258265, 2020-08-02)   <-- berry, in the middle
#      #9431 A   (base.sha 82367790, 2020-01-19)
#      ...
#      #9965 A   (base.sha 968a3019, 2020-05-03)
#      #10016 B  (base.sha e66a0e88, 2020-11-14)
#
# `base.sha` is the base-branch tip recorded by GitHub, not the PR's own
# chronology, so #9326's base commit postdates #9965's by three months. Any
# `if pr.number < X` split would put a berry commit in the yarn-1 branch. The
# split is therefore made where the truth actually lives -- the checked-out
# tree -- by testing for `.yarnrc.yml` at run time (see _INSTALL below).
#
# This also removes the Check-2F image-tag hazard entirely: one base image,
# one tag, no chance of two eras colliding on the same image_full_name() and
# silently building only one of them.
#
# ---------------------------------------------------------------------------
# WHY node:12-buster SERVES BOTH SUB-ERAS
# ---------------------------------------------------------------------------
#   engines.node across the 20 base commits:
#       ">= 8"   ">= 8.3"   ">= 10.14.2"
#       "^10.13.0 || ^12.13.0 || ^14.15.0 || >=15.0.0"   (#10016 only)
#   node:12-buster ships 12.22.12, which satisfies every one of them
#   (^12.13.0 included). The repo's own CI matrix at both ends of the range
#   runs node 8/10/12 (sub-era A) and 10/12/14 (sub-era B), so 12 is the one
#   version tested by upstream on BOTH sides of the migration.
#
#   Yarn: the node:12 image ships yarn 1.22.18 globally, and yarn 1.22 honours
#   BOTH `yarn-path` in .yarnrc and `yarnPath` in .yarnrc.yml. Verified in the
#   built image: at #10293's base commit `yarn --version` reports
#   "2.1.1-git.20200713.2613ca00", i.e. the global yarn transparently handed
#   off to the berry release checked into .yarn/releases. Nothing has to be
#   installed for either era, and no `corepack enable` is needed (corepack does
#   not exist on node 12 anyway).
#
#   buster is EOL, so its apt endpoints have moved to archive.debian.org and
#   buster-updates is gone; the sources rewrite below is mandatory before any
#   apt-get update.
_INTERVAL_NAME = "jest_10293_to_8890"

# Inclusive PR bounds this bundle claims. jest.py's dispatcher reads them, so
# the routing decision and the interval name can never drift apart.
PR_LOW = 8890
PR_HIGH = 10293

_NODE_BASE = "node:12-buster"


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


# Re-create the workspace symlinks yarn's linker would have made.
#
# This is NOT a dependency install -- it is offline, touches only
# node_modules/<name> entries that are missing, and never removes or rewrites
# an existing one. It exists because a fix patch is allowed to introduce a
# brand-new workspace package, and the graded stages must not re-run `yarn
# install` (that would mean network access during grading and three different
# dependency resolutions).
#
# Measured on PR #9801: fix.patch creates packages/jest-globals/package.json
# ("@jest/globals"). node_modules/@jest/globals was linked by the install that
# ran at image-build time, i.e. before the patch existed, so without this step
# the new package is unresolvable and BOTH the new e2e test and
# packages/jest-globals/src/__tests__/index.ts die with "Cannot find module
# '@jest/globals'" -- in the fix stage, which is exactly where they must pass.
# In run.sh it is a no-op (every link already exists), so all three stages stay
# byte-identical apart from the patch.
_LINK_WORKSPACES_JS = r"""/**
 * Ensure every packages/<dir> is reachable under node_modules/<pkg name>.
 * Additive only: an existing entry (real dir or symlink) is left untouched.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const root = process.cwd();
const packagesDir = path.join(root, 'packages');

if (!fs.existsSync(packagesDir)) {
  process.exit(0);
}

for (const entry of fs.readdirSync(packagesDir)) {
  const manifest = path.join(packagesDir, entry, 'package.json');
  if (!fs.existsSync(manifest)) {
    continue;
  }

  let name;
  try {
    name = JSON.parse(fs.readFileSync(manifest, 'utf8')).name;
  } catch (error) {
    continue;
  }
  if (!name) {
    continue;
  }

  const link = path.join(root, 'node_modules', name);

  // lstat, not existsSync: existsSync follows symlinks, so a link left dangling
  // by an earlier stage would read as "absent" and symlinkSync would EEXIST.
  let present = true;
  try {
    fs.lstatSync(link);
  } catch (error) {
    present = false;
  }
  if (present) {
    continue;
  }

  fs.mkdirSync(path.dirname(link), {recursive: true});
  fs.symlinkSync(path.join(packagesDir, entry), link);
  console.log('link_workspaces: linked ' + name);
}
"""


# Undo Babel 7.8.0-7.8.3's `"exports": false`.
#
# Those releases shipped a literal `"exports": false` to opt OUT of Node's then
# new conditional-exports resolution. Node <= 12.15 ignored the field entirely,
# so it was harmless when the lockfiles were written. Node 12.16 turned exports
# resolution on for require(), and from then on `false` reads as "this package
# exports nothing":
#
#     Error [ERR_PACKAGE_PATH_NOT_EXPORTED]: No "exports" main defined in
#       /home/jest/node_modules/@babel/helper-compilation-targets/package.json
#         at Object.<anonymous> (node_modules/@babel/preset-env/lib/debug.js:8:33)
#
# raised out of `node ./scripts/build.js`, which fails the image build outright.
# Babel's own fix was to delete the field in 7.8.4; this reproduces that, in
# node_modules only, and never touches a tracked file.
#
# Measured across the dataset: exactly two PRs are affected, #9431 (base.sha
# 82367790, 2020-01-19) and #9465 (ffdaa751, 2020-02-02), whose lockfiles pin
# @babel/helper-compilation-targets@7.8.3. It repairs that one package and
# reports "repaired 0 packages" everywhere else, so it cannot perturb any PR
# that already builds.
#
# The alternative -- pinning the image to node:12.15 to dodge the bug -- was
# rejected: it would freeze the whole bundle on a build of Node chosen for one
# transitive dependency's packaging mistake, and #10016's engines field
# (^10.13.0 || ^12.13.0 || ^14.15.0 || >=15.0.0) leaves no room to move later.
_PATCH_BABEL_EXPORTS_JS = r"""/**
 * Strip `"exports": false` from installed packages (Babel 7.8.0-7.8.3 bug).
 * Operates on node_modules only; a package without that exact value is skipped.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const root = path.join(process.cwd(), 'node_modules');
if (!fs.existsSync(root)) {
  process.exit(0);
}

function manifestsIn(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir)) {
    // Scoped packages nest one level deeper: node_modules/@babel/<pkg>.
    const target = path.join(dir, entry);
    if (entry.startsWith('@')) {
      let scoped = [];
      try {
        scoped = fs.readdirSync(target);
      } catch (error) {
        continue;
      }
      for (const inner of scoped) {
        out.push(path.join(target, inner, 'package.json'));
      }
    } else {
      out.push(path.join(target, 'package.json'));
    }
  }
  return out;
}

let repaired = 0;
for (const manifestPath of manifestsIn(root)) {
  if (!fs.existsSync(manifestPath)) {
    continue;
  }
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  } catch (error) {
    continue;
  }
  if (manifest.exports !== false) {
    continue;
  }
  delete manifest.exports;
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
  console.log(
    'patch_babel_exports: removed "exports": false from ' +
      manifest.name +
      '@' +
      manifest.version
  );
  repaired += 1;
}

console.log('patch_babel_exports: repaired ' + repaired + ' packages');
"""


# Pick the test files this PR is actually about, from the patches themselves.
#
# Run ONCE at image-build time and baked to /home/jest_pattern.txt, so run.sh,
# test-run.sh and fix-run.sh all read the SAME string. If each script recomputed
# it they could in principle diverge, and a f2p transition caused by the command
# rather than by the fix is exactly what Check 3B forbids.
#
# Both sides of every `diff --git a/X b/Y` header are collected because four of
# the PRs rename test files (#9291, #9431, #10016 test patches; #10016 fix
# patch). run.sh executes against the UNPATCHED tree, where only the `a/` name
# exists; the other two stages see the `b/` name. Feeding jest both keeps the
# baseline stage from silently selecting nothing.
#
# The filter mirrors this repo's jest.config.js: a file counts as a test if it
# lives under a `__tests__/` directory or is named *.test.* / *.spec.*, and is
# not a stored snapshot. Fixtures that happen to satisfy that (e.g.
# packages/jest-cli/src/init/__tests__/fixtures/...) are harmless -- jest applies
# its own testPathIgnorePatterns after this pattern, so they are simply never
# collected. Verified across all 20 PRs: every one selects at least one real
# test file.
_SELECT_TESTS = r"""
CHANGED=$(cat /home/test.patch /home/fix.patch 2>/dev/null \
  | sed -n -e 's|^diff --git a/\(.*\) b/\(.*\)$|\1\n\2|p' | sort -u)

SELECTED=$(printf '%s\n' "$CHANGED" \
  | grep -E '\.(js|jsx|ts|tsx|mjs|cjs)$' \
  | grep -v -E '(^|/)__snapshots__/' \
  | grep -E '(^|/)__tests__/|\.(test|spec)\.[a-z]+$' \
  | sort -u || true)

PATTERN=""
for candidate in $SELECTED; do
  escaped=$(printf '%s' "$candidate" | sed 's/[][().*+?^$\\|{}]/\\&/g')
  PATTERN="${PATTERN}${PATTERN:+|}/${escaped}\$"
done

printf '%s' "$PATTERN" > /home/jest_pattern.txt
echo "=== selected test path pattern: $PATTERN ==="

# An empty pattern would make --testPathPattern match EVERYTHING, turning the
# graded run into a multi-hour full-suite run whose result has nothing to do
# with this PR. Fail the build instead of shipping that image.
test -s /home/jest_pattern.txt

# Decide, once, whether this PR also needs a jest-circus pass.
#
# jest ships two test runners in this era: jasmine2 (the default) and
# jest-circus, selected by JEST_CIRCUS=1. Upstream runs BOTH in CI -- see the
# dedicated `test-jest-circus` job in .circleci/config.yml, whose command is
# `JEST_CIRCUS=1 yarn test-ci-partial` -- because a bug can exist in one runner
# and not the other.
#
# #9965 is exactly that case. Its fix.patch touches only
# packages/jest-circus/{eventHandler,run,utils}.ts plus jest-types, so under the
# default runner its new test "interleaved describe and test children order"
# passes even without the fix, and the three stages come out 9/0, 10/0, 10/0 --
# no FAIL->PASS anywhere, which Report.check() rule 3 rejects as an invalid
# instance. Under JEST_CIRCUS=1 that same test fails until the fix is applied.
#
# Scoped to PRs whose patches actually touch packages/jest-circus/ (4 of 20:
# #9326, #9801, #9828, #9965) rather than run unconditionally, because the
# second pass doubles the graded runtime and would re-run jasmine2-specific
# suites -- e2e/__tests__/jasmineAsync.test.ts,
# packages/jest-jasmine2/src/__tests__/concurrent.test.ts -- under a runner they
# were never written for.
#
# Decided HERE, at image-build time, and baked to a file, so all three graded
# stages read the identical value and cannot diverge.
if grep -q '^diff --git a/packages/jest-circus/' /home/test.patch /home/fix.patch; then
  printf '1' > /home/jest_circus.txt
  echo "=== patches touch packages/jest-circus: enabling the jest-circus pass ==="
else
  : > /home/jest_circus.txt
  echo "=== patches do not touch packages/jest-circus: default runner only ==="
fi
"""


# The graded jest invocation. Defined once and pasted verbatim into all three
# run scripts so the stages differ ONLY by which patch was applied.
#
#   --ci          new snapshots FAIL instead of being written. Without it a
#                 fix stage could "pass" by inventing the snapshot it was
#                 supposed to match.
#   --verbose     the only reporter mode that prints one line per test; the
#                 default summary reporter prints counts, from which no test
#                 names can be recovered.
#   --runInBand   upstream's own `test-ci-partial` uses -i. It also keeps each
#                 suite's block contiguous in the log -- with workers, two
#                 suites interleave and the describe-nesting parser below would
#                 attribute tests to the wrong file.
#   --forceExit   jest's e2e suites spawn child jest processes; a leaked handle
#                 would hang the stage until the harness timeout and truncate
#                 the log to an unparseable fragment.
#
# No `|| true`: a non-zero exit is EXPECTED in the test stage and the harness
# grades the log, not the exit code. `|| true` would also hide the case where
# jest never starts, which must surface as an empty log the report rejects
# rather than as a silent 0/0/0.
_CIRCUS_BANNER = "=== jest-circus runner pass ==="

_EXEC_TESTS = (
    r"""
JEST_PATTERN=$(cat /home/jest_pattern.txt)
echo "=== running jest for pattern: $JEST_PATTERN ==="

STATUS=0
node ./packages/jest-cli/bin/jest.js \
  --ci \
  --verbose \
  --runInBand \
  --forceExit \
  --testPathPattern "$JEST_PATTERN" 2>&1 || STATUS=$?

# Second pass under the other runner, when prepare.sh decided this PR needs one.
# The selection, the flags and the banner are identical in all three stages, so
# the only thing that can differ between them is still the patch.
if [ -s /home/jest_circus.txt ]; then
  echo "__CIRCUS_BANNER__"
  JEST_CIRCUS=1 node ./packages/jest-cli/bin/jest.js \
    --ci \
    --verbose \
    --runInBand \
    --forceExit \
    --testPathPattern "$JEST_PATTERN" 2>&1 || STATUS=$?
fi

echo "=== jest exited with status $STATUS ==="
exit $STATUS
""".replace("__CIRCUS_BANNER__", _CIRCUS_BANNER)
)


# Install, era-detected from the checked-out tree rather than from the PR
# number (see the sub-era note at the top of this file).
#
# Sub-era A: `node scripts/remove-postinstall` is what upstream CI does
# (.circleci/config.yml `&install`, .azure-pipelines-steps.yml). The
# postinstall it deletes is `opencollective postinstall && yarn build`, which
# both makes a donation-banner network call and runs the TypeScript build as a
# side effect of installing; removing it keeps install and build separable and
# stops a flaky third-party endpoint from failing the image build.
# `--ignore-engines` is upstream's flag too -- some transitive dependency
# declares a narrower node range than jest itself.
#
# Sub-era B: berry defaults to immutable installs when CI=true and would abort
# with YN0028 if it wanted to touch yarn.lock; YARN_ENABLE_IMMUTABLE_INSTALLS
# =false permits the write. The lockfile is fully populated at these commits,
# so nothing is expected to change -- this is a guard, not a licence to drift.
#
# Retried rather than tolerated: five attempts, then a hard `test`. A swallowed
# install failure would leave the image importable-looking but empty, and the
# graded stages would report 0/0/0 with no indication of why.
_INSTALL = r"""
if [ -f .yarnrc.yml ]; then
  echo "=== yarn era: berry (.yarnrc.yml present) ==="
  export YARN_ENABLE_IMMUTABLE_INSTALLS=false
  INSTALL_CMD="yarn install"
else
  echo "=== yarn era: classic (.yarnrc / yarn-path) ==="
  if [ -f scripts/remove-postinstall.js ]; then
    node ./scripts/remove-postinstall.js
    echo "=== removed package.json postinstall hook (upstream CI does the same) ==="
  fi
  INSTALL_CMD="yarn install --no-progress --frozen-lockfile --ignore-engines"
fi

installed=0
for attempt in 1 2 3 4 5; do
  if $INSTALL_CMD; then
    installed=1
    break
  fi
  echo "=== install attempt ${attempt} failed, retrying in 20s ==="
  sleep 20
done
test "$installed" -eq 1

node /home/patch_babel_exports.js
"""


# `scripts/build.js` transpiles packages/*/src -> packages/*/build. It is a hard
# prerequisite of the test run, not an optimisation, for three separate reasons
# visible in the repo's own config:
#
#   * packages/jest-cli/bin/jest.js does `require('../build/cli')`;
#   * jest.config.js `snapshotSerializers` loads
#     packages/pretty-format/build/plugins/ConvertAnsi.js;
#   * jest.config.js `testEnvironment: './packages/jest-environment-node'`
#     resolves through that package's "main": "build/index.js".
#
# It also has to be redone in EVERY graded stage: a cross-package import inside
# a test (`import ... from 'jest-util'`) resolves via node_modules to the
# workspace's build/ output, so a fix patch that edits another package's src is
# invisible until the build re-runs. Skipping it would make the fix stage
# identical to the test stage and drive f2p to zero.
#
# `build:ts` (tsc -b) is deliberately NOT run: it only emits .d.ts declarations
# for downstream TypeScript consumers. Nothing in the graded run type-checks, and
# it costs minutes per stage.
#
# Output is captured rather than streamed because it is ~200 lines of progress
# dots per stage; it is echoed in full if the build fails, so nothing is hidden.
_BUILD = r"""
echo "=== building packages (scripts/build.js) ==="
if ! node ./scripts/build.js > /home/build.log 2>&1; then
  echo "=== build FAILED; full output follows ==="
  cat /home/build.log
  exit 1
fi
echo "=== build ok ==="
"""


# Reset to the pinned commit before every graded stage.
#
# prepare.sh intentionally leaves the tree dirty (sub-era A rewrites
# package.json via remove-postinstall), and `git apply` refuses to patch a file
# that has uncommitted changes -- which would make the fix stage a silent no-op
# with zero f2p. Running the identical reset in run.sh too keeps all three
# stages starting from a byte-identical tree.
#
# `git clean -fd` WITHOUT -x: node_modules/, packages/*/build/ and e2e/*/
# node_modules/ are all gitignored, so the caches baked at image-build time
# survive while files created by a previous stage's patch are removed.
_RESET = r"""
git reset --hard
git clean -fd
"""


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

# CI=true is what upstream's own pipeline exports (.azure-pipelines.yml
# `variables`), and several suites branch on it.
#
# FORCE_COLOR=1 comes from the same block, where upstream annotates it: "Used by
# chalk. Ensures output from Jest includes ANSI escape characters that are needed
# to match test snapshots." The stored .snap files in this era contain literal
# escape codes, so unsetting it would change which snapshots match. It is set
# identically in all three stages, so it cannot bias the f2p comparison, and
# parse_log strips ANSI before matching anything.
export CI=true
export FORCE_COLOR=1

cd /home/__REPO__

# Apply patches, or ABORT. Falling through would leave the tree at baseline and
# make a graded stage a silent re-run of the previous one -- an empty f2p set
# that looks perfectly legitimate in the report.
#
# No --exclude filters: every test.patch and fix.patch in this dataset was
# scanned for "GIT binary patch" / "Binary files" and none contains one, so
# there is nothing git apply cannot represent as text.
#
# The --3way fallback exists for one measured reason. #10016's fix.patch carries
# a CHANGELOG.md hunk whose context does not match its own base.sha:
#     error: patch failed: CHANGELOG.md:22
#     error: CHANGELOG.md: patch does not apply
# and the whole apply is atomic, so ONE stale documentation hunk discards the
# eight source files the fix actually needs, leaving the fix stage at 0/0/0.
# --3way reconstructs that hunk from the blobs the patch names and reports
# "Applied patch to 'CHANGELOG.md' cleanly", so the fix is applied in full.
#
# It is a FALLBACK, not the default: --3way needs the pre-image blobs to still
# exist, and the PR layer prunes history to HEAD's ancestry. Plain apply is
# tried first so the nine PRs already verified against it keep the exact
# behaviour they were verified with, and --3way only runs where plain apply has
# actually failed. Both are strict -- a failure of both still exits non-zero.
apply_patches() {
    if git apply --whitespace=nowarn "$@"; then
        return 0
    fi
    echo "=== plain git apply failed; retrying with --3way ===" >&2
    git reset --hard
    git clean -fd
    git apply --3way --whitespace=nowarn "$@"
}
"""


_APPLY_TEST_PATCH = r"""
if ! apply_patches /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
"""


_APPLY_BOTH_PATCHES = r"""
if ! apply_patches /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch + fix.patch failed" >&2
    exit 1
fi
"""


_LINK = r"""
node /home/link_workspaces.js
"""


_PREPARE_SH = (
    r"""#!/bin/bash
set -e
export CI=true
export FORCE_COLOR=1

cd /home/__REPO__

# Pin the tree. The base image is a shared, full-history clone pinned to
# nothing, so this is where THIS PR's commit is established; the prune block in
# the PR Dockerfile afterwards only asserts the result.
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
echo "=== HEAD pinned at $(git rev-parse HEAD) ==="
"""
    + _INSTALL
    + _BUILD
    + _LINK
    + r"""
# Warm the per-fixture node_modules used by the e2e suites.
#
# Several e2e tests shell out to `yarn` inside their own fixture directory --
# e2e/__tests__/transform.test.ts does it four times, and
# stackTraceSourceMapsWithCoverage.test.ts once. Those calls happen during the
# GRADED run, so leaving them cold makes every stage depend on live registry
# metadata. Populating the caches here turns them into fast local re-checks.
#
# YARN_IGNORE_PATH=1 bypasses the repo's checked-in yarn (`yarn-path` /
# `yarnPath`) and uses the image's plain yarn 1.22 instead. Without it, sub-era
# B would hand these yarn-1 lockfiles to berry, which does not understand them.
# --frozen-lockfile makes a lockfile rewrite an error rather than a silent
# mutation, and the `git checkout -- e2e` below undoes any that slipped through
# -- #9811's test.patch ADDS e2e/stack-trace-source-maps-with-coverage/yarn.lock,
# and it must apply against the pristine file.
#
# Best-effort by design: a cold cache is recoverable at run time, and the
# DEPS_OK gate below still has to pass.
for lockfile in e2e/*/yarn.lock e2e/*/*/yarn.lock; do
  [ -f "$lockfile" ] || continue
  fixture=$(dirname "$lockfile")
  ( cd "$fixture" \
      && YARN_IGNORE_PATH=1 yarn install --frozen-lockfile --ignore-scripts \
           --non-interactive --no-progress --network-timeout 600000 \
      && echo "=== e2e warm-cache ok: $fixture ===" ) \
    || echo "=== e2e warm-cache miss (non-fatal): $fixture ==="
done
git checkout -- e2e

# Hard gate. No `|| true` anywhere below.
#
# Each of these is a real entry point the graded run depends on, not a token
# import: the snapshot serializer, the test environment, and the CLI the run
# scripts actually execute. A partial install or a half-written build fails
# HERE, at image-build time, instead of three stages later behind an empty log
# that reads as "0 failures".
node -e "require('./packages/pretty-format/build/plugins/ConvertAnsi.js'); require('./packages/jest-environment-node'); console.log('DEPS_OK: runtime deps resolve')"
node ./packages/jest-cli/bin/jest.js --version
echo "DEPS_OK: jest cli runs"
"""
    + _SELECT_TESTS
)


_RUN_SH = _SCRIPT_HEADER + _RESET + _LINK + _BUILD + _EXEC_TESTS
_TEST_RUN_SH = (
    _SCRIPT_HEADER + _RESET + _APPLY_TEST_PATCH + _LINK + _BUILD + _EXEC_TESTS
)
_FIX_RUN_SH = (
    _SCRIPT_HEADER + _RESET + _APPLY_BOTH_PATCHES + _LINK + _BUILD + _EXEC_TESTS
)


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).rstrip("\n") + "\n"


# The BASE Dockerfile opts out of DockerfileEnhancer by emitting the syntax
# directive itself (image.py:317 returns the file verbatim once it sees that
# line). It therefore has to supply the proxy / CA / label infrastructure, which
# it does by reusing image.py's own constants so the wiring cannot drift from
# every other repo in the harness.
#
# ARG BASE_COMMIT is DECLARED and never referenced: build_dataset.py passes it
# to any image whose dependency() is a string, and declaring it silences
# BuildKit's unused-arg warning. CONSUMING it is what would pin a shared base to
# one PR's commit and break every other PR in the shard.
_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

__PROXY_ARGS__

__ENV_BLOCK__

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

__CERT_SYMLINKS__

# buster reached EOL: deb.debian.org no longer serves it and buster-updates does
# not exist at all, so this rewrite has to happen before the first apt-get
# update. Check-Valid-Until is disabled because the archived Release files are
# long past their stated expiry.
#
# node:12-buster is buildpack-deps based and already ships git, python, make and
# g++, so only two things are genuinely added here:
#   ca-certificates  - refreshed so the proxy CA wiring above has a real bundle;
#   mercurial        - packages/jest-changed-files supports hg, and e2e/Utils.ts
#                      `testIfHg` probes for the binary with which.sync('hg').
#                      Without it those cases report as SKIP; with it they
#                      actually exercise the hg code path that #9769's
#                      onlyChanged.test.ts lives next to.
RUN sed -i "s|deb.debian.org|archive.debian.org|g" /etc/apt/sources.list \
 && sed -i "s|security.debian.org|archive.debian.org|g" /etc/apt/sources.list \
 && sed -i "/buster-updates/d" /etc/apt/sources.list \
 && apt-get update -o Acquire::Check-Valid-Until=false \
 && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    mercurial \
 && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

# Full history, pinned to nothing: 20 PR layers share this image and each has to
# be able to reach its own base.sha. No --depth, no --single-branch, no checkout.
RUN git clone "${REPO_URL}" /home/__REPO__ \
 && git -C /home/__REPO__ rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


# The PR layer. Never enhanced (image.py:315-316 returns raw as soon as
# dependency() is an Image), so what is written here is exactly what is built.
#
# Order matters: prepare.sh checks out this PR's commit and provisions the tree,
# and only then does the prune run. The prune deliberately contains no
# `git reset`, no `git clean` and no path-scoped checkout -- prepare.sh leaves
# package.json modified on purpose in sub-era A, and any of those would silently
# revert it. The commit-scoped `git checkout --detach` that opens the shared
# hardening block is a same-tree no-op that preserves those edits.
_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

WORKDIR /home/__REPO__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

__HARDENING__

__CLEAR_ENV__
"""


class JestBundleImageBase(Image):
    """Shared environment image: node:12 toolchain + a full-history clone."""

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
        return _NODE_BASE

    def image_tag(self) -> str:
        # Distinct from jest.py's legacy "base" tag. Images dedup on
        # image_full_name() (Image.__hash__/__eq__) and build_dataset.py
        # collects them into a set, so two eras sharing a tag would mean only
        # one Dockerfile is ever built and whichever PR won that race would
        # decide the toolchain for the other era.
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        # The base stages nothing: no patches, no scripts. Those are per-PR.
        return []

    def dockerfile(self) -> str:
        base_image = self.dependency()
        if isinstance(base_image, Image):
            base_image = base_image.image_full_name()

        return _tidy(
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", base_image)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", DockerfileEnhancer._ENV_BLOCK)
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
        )


class JestBundleImageDefault(Image):
    """Per-PR layer: pins the commit, provisions the tree, prunes the history."""

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
        return JestBundleImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "link_workspaces.js", _LINK_WORKSPACES_JS),
            File(".", "patch_babel_exports.js", _PATCH_BABEL_EXPORTS_JS),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return _tidy(
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace(
                "__HARDENING__",
                # build_dataset.py only passes build args to images whose
                # dependency() is a string, i.e. to the base. The PR layer gets
                # none, so ${BASE_COMMIT} is substituted with the literal SHA.
                Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha),
            )
            .replace("__CLEAR_ENV__", self.clear_env)
        )


# ---------------------------------------------------------------------------
# parse_log
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Suite header, e.g. " PASS  e2e/__tests__/transform.test.ts (13.477s)".
#
# Anchored at column 0-2 on purpose. Jest's own e2e suites spawn a child jest and
# echo its entire transcript inside a failure block, indented well past that; an
# unanchored `^\s*` would count the child's suite headers and result lines as
# results of THIS run, inventing passes that never happened.
_SUITE_RE = re.compile(
    r"^ {0,2}(?:PASS|FAIL)\s+(?:\[[^\]]*\]\s+)?"
    r"(\S+\.[cm]?[jt]sx?)"
    r"(?:\s+\(\d+(?:[.,]\d+)?\s*m?s\))?\s*$"
)

_PASS_MARKS = "\u2713\u2714"  # ✓ ✔
_FAIL_MARKS = "\u2715\u2717\u00d7\u2718"  # ✕ ✗ × ✘

# "\u25cf" (●) is deliberately ABSENT. It is this reporter's FAILURE-DETAIL
# bullet, not a skip marker, and the names it carries are spelled
# "describe > test" rather than indented under the suite. Treating it as a skip
# moves genuine failures into skipped_tests, and the `passed -= skipped`
# reconciliation below then deletes the same name from passed_tests in another
# stage -- manufacturing a transition out of nothing. Probed against the built
# image, the reporter's real non-result markers are exactly these two:
#     "  ○ skipped is skipped"
#     "  ✎ todo is a todo"
_SKIP_MARKS = "\u25cb\u25ef\u270e\u2193"  # ○ ◯ ✎ ↓

_TEST_LINE_RE = re.compile(
    r"^(\s+)([" + _PASS_MARKS + _FAIL_MARKS + _SKIP_MARKS + r"])\s+(\S.*)$"
)

# "(1451ms)" in one stage, "(1.5 s)" in another. Both must go: a duration left in
# the name enters the same test into the union twice under two spellings, and
# Report.__post_init__ then sees a phantom NONE->FAIL that check() rule 4 rejects.
_DURATION_RE = re.compile(r"\s*\(\d+(?:[.,]\d+)?\s*m?s\)\s*$")

# Everything that ends a suite's result block for good. After one of these, and
# before the next suite header, there are no more result lines for this suite:
#
#   "●"    opens the failure-detail section. Its lines carry real test names in
#          a DIFFERENT "describe > test" spelling, so counting them would enter
#          one test under two names and let Report.check() see a phantom
#          NONE->FAIL. Everything downstream of it (diffs, code frames, stack
#          traces) belongs to that section too.
#   rest   the run summary.
#
# "at " is intentionally NOT here: stack-trace lines only ever appear after a
# "●", so the entry would be redundant, and it would misfire on a describe block
# legitimately named "at ...".
_BLOCK_END_PREFIXES = (
    "\u25cf",  # ●
    "Test Suites:",
    "Tests:",
    "Snapshots:",
    "Time:",
    "Ran all test suites",
    "Summary of all failing tests",
)

# Captured console output is a SUSPENSION, not a termination.
#
# jest 24/25's VerboseReporter prints the test tree before the console buffer,
# so terminating here would be harmless -- but that ordering is a reporter
# implementation detail, and if it ever flipped, terminating would silently drop
# every result line of any suite that logged. Suspending instead is correct
# under both orderings.
#
# The block's header sits at column 2 and jest indents every line of the message
# itself to column 4 or deeper, so the first line back at column <= 2 that is not
# another console header is outer-run output again. That boundary also contains
# the real hazard: jest's own e2e suites log the transcript of a child jest run,
# whose ✓/✕ lines are results of a DIFFERENT run and must never be counted.
_CONSOLE_RE = re.compile(r"^\s*console\.[a-zA-Z]+\b")
_CONSOLE_CONTENT_MIN_INDENT = 3

# The reporter emits an AGGREGATE skip line, "○ skipped 4 tests", which carries a
# count rather than a test name. Capturing it injects junk like "skipped 4 tests"
# into skipped_tests, and the count changes between stages so the junk entry
# changes name too.
_SKIP_AGGREGATE_RE = re.compile(r"^(?:skipped|todo)\s+\d+\s+tests?$")
_SKIP_PREFIX_RE = re.compile(r"^(?:skipped|todo)\s+")

# Results printed after this banner came from the jest-circus runner, not
# jasmine2. They MUST be given distinct identities: the same file, describe and
# title exist under both runners, so without a suffix the two results merge and
# `passed -= failed` lets a circus failure erase a jasmine2 pass -- which then
# reads as a PASS->FAIL regression that Report.check() rule 2 rejects.
#
# The marker is a SUFFIX rather than a prefix so that
# report.py's `test_name.startswith(f + " > ")` file-attribution
# (report.py:385-395) keeps matching for circus results too.
_CIRCUS_SUFFIX = " [jest-circus]"


def _clean_title(title: str) -> str:
    return _DURATION_RE.sub("", title.strip()).strip()


def jest_bundle_parse_log(test_log: str) -> TestResult:
    """Parse jest's --verbose reporter output into a TestResult.

    Test identity is "<suite path> > <describe> > ... > <test title>".

    The separator is " > " and not the prettier " \u203a " for a concrete reason:
    report.py's _test_name_matches_files (report.py:385-395) attributes a test to
    a patched file with `test_name.startswith(f + " > ")`. With any other
    separator that matcher never fires, _file_matcher_can_hit returns False, and
    the file-based half of the f2p attribution silently switches off.

    The suite path is mandatory, not decoration: within a single file this
    reporter repeats leaf titles under different describes -- transform.test.ts
    alone has "runs transpiled code" four times -- so a leaf-only name collapses
    distinct tests into one entry and destroys the pass/fail counts.

    Known and accepted: a handful of jest's own suites declare two tests with an
    IDENTICAL file+describe+title, so no name-based identity can separate them.
    Measured against the real logs, path+describe+title reproduces jest's own
    summary exactly for 10 of the 12 captured stages; the two exceptions are
    both PR #9326, where `it.concurrent.each` and duplicated `.test.concurrent`
    blocks emit 365 result lines under 355 distinct names (e.g.
    "concurrent.test.ts > concurrent > should add 1 to number" three times).

    They are merged rather than disambiguated by occurrence index on purpose: an
    index is not stable across stages, because test.patch changes how many
    copies exist, and an unstable name is exactly what makes Report.check()
    see a phantom NONE->FAIL. Merging is also the safe direction -- when copies
    of one name disagree, `passed -= failed` below lets FAIL win, so a duplicate
    can never manufacture a pass.
    """
    # ANSI first: the reporter colourises every ✓/✕ and suite header, and none of
    # the patterns below match while the escape codes are still in the line.
    log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(mark: str, name: str) -> None:
        if not name:
            return
        if mark in _PASS_MARKS:
            passed_tests.add(name)
        elif mark in _FAIL_MARKS:
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    current_file: str | None = None
    describe_stack: list[str] = []
    in_suite = False
    in_console = False
    suffix = ""

    for raw_line in log.splitlines():
        line = raw_line.rstrip()

        if line.strip() == _CIRCUS_BANNER:
            suffix = _CIRCUS_SUFFIX
            in_suite = False
            in_console = False
            continue

        suite = _SUITE_RE.match(line)
        if suite:
            current_file = suite.group(1)
            describe_stack = []
            in_suite = True
            in_console = False
            continue

        if not in_suite:
            continue

        stripped = line.strip()
        if not stripped:
            continue

        indent = len(line) - len(line.lstrip(" "))

        if in_console:
            if indent > _CONSOLE_CONTENT_MIN_INDENT or _CONSOLE_RE.match(line):
                continue
            in_console = False

        if _CONSOLE_RE.match(line):
            in_console = True
            continue

        if stripped.startswith(_BLOCK_END_PREFIXES):
            in_suite = False
            continue

        if indent == 0:
            # A column-0 line inside a suite block is the continuation of a test
            # title that contains a literal newline. Skip it, and change NO
            # state.
            #
            # Both other readings are wrong, and both were observed on
            # packages/expect/src/__tests__/matchers.test.js (#9757), whose
            # `.each` titles embed multi-line strings:
            #
            #   ✓ fails for "with
            #   trailing space" and "without trailing space" (1ms)
            #
            # Ending the suite here loses every result below the first such
            # title -- measured 81 of 605 collected. Treating it as a describe
            # header is worse: it resets describe_stack, so the ~520 tests after
            # it get filed under a fragment of a string literal, giving names
            # that are neither unique nor stable.
            #
            # Dropping the terminator costs nothing, because the real ends of a
            # block are all matched explicitly: the next `PASS|FAIL <path>`
            # header, and the summary keys in _BLOCK_END_PREFIXES.
            continue

        test = _TEST_LINE_RE.match(line)
        if test:
            mark = test.group(2)
            title = _clean_title(test.group(3))
            if mark in _SKIP_MARKS:
                if _SKIP_AGGREGATE_RE.match(title):
                    continue
                title = _SKIP_PREFIX_RE.sub("", title).strip()
            # The reporter indents by describe depth, two columns per level, so a
            # test at column 4 sits under exactly one describe.
            context = describe_stack[: max(indent // 2 - 1, 0)]
            parts = ([current_file] if current_file else []) + context + [title]
            record(mark, " > ".join(part for part in parts if part) + suffix)
            continue

        # An unmarked indented line inside a suite block is a describe header.
        level = max(indent // 2, 1)
        describe_stack = describe_stack[: level - 1]
        describe_stack.append(stripped)

    # TestResult.__post_init__ (test_result.py:56-101) rejects any overlap
    # between the three sets. A suite can report a test as passing and then fail
    # it (retry, or a suite-level failure after individual passes); failure wins.
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


@Instance.register("jestjs", _INTERVAL_NAME)
class JEST_10293_TO_8890(Instance):
    # Exposed on the class so jest.py's dispatcher can read the bounds off the
    # registry entry instead of hard-coding a second copy that could drift.
    PR_LOW = PR_LOW
    PR_HIGH = PR_HIGH

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return JestBundleImageDefault(self.pr, self._config)

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
        return jest_bundle_parse_log(test_log)
