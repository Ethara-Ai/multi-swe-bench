r"""jestjs/jest -- era config for PRs 10484 .. 12546.

Twenty PRs, ONE era, so ONE shared base image (rule 4, row 2). The toolchain
was read at ALL NINETEEN distinct base commits, not sampled from one:

    engines.node        >=10.14.2 (oldest) .. ^12.13 || ^14.15 || ^16.13 || >=17
    vendored yarn       .yarn/releases/yarn-{sources,2.3.1,2.4.0,2.4.1,2.4.2,
                        2.4.3,3.1.1,3.2.0}.cjs  -- present at EVERY commit
    scripts.build:js    node ./scripts/build.js -- identical at all 19
    root jest config    jest.config.js -- identical shape at all 19

The yarn version moves five times across the range, and that is exactly why it
does NOT force an era split: every commit vendors its own release under
`.yarn/releases/`, and `.yarnrc.yml`'s `yarnPath` points at it. Invoking that
file directly --

    YARN="$(ls .yarn/releases/yarn*.cjs | head -1)"; node "$YARN" install

-- makes the install self-pinning. No corepack, no global yarn install, and no
network fetch of a package manager. Proven in a throwaway container at BOTH
ends of the range on 2026-09-10: `yarn-sources.cjs` (Sep 2020) and
`yarn-3.2.0.cjs` (Mar 2022) each returned rc=0 under node:16-bullseye.

Several configs for this repo already exist -- jest.py plus ten era shards,
two of which (`jest_10723_to_9326.py`, `jest_12912_to_7792.py`) overlap this
range. They are DELIBERATELY left untouched (rule 12, 2026-09-08: never modify
an existing repo config, always add a new one for the dataset in hand). Because
the bare ("jestjs","jest") key IS registered by jest.py, every row in this
dataset must be stamped with number_interval="jest_12546_to_10484" by
run_pipeline.sh, or it would silently resolve to the wrong era.


WHY node:16 AND NOT node:14
---------------------------
node 14 is the only version present in jest's OWN CI matrix across the whole
range (2020-09 ships [10,12,13,14]; 2022-03 ships [12,14,16,17]), so node 14
looks like the safe pick. It is not, for one measured reason:

    PR 11331's test patch adds, in
    packages/jest-runtime/src/__tests__/runtime_require_module.test.js,

        onNodeVersions('^16.0.0', () => {
          it('finds node core built-in modules with node:prefix', ...)
        })

On node 14 that test SKIPS. A skipped test is not a failure, so it can never
become F2P, and the graded signal for that file is silently lost. node 16 runs
it. Every other version guard in the graded set is satisfied by node 16 too:
'>=12.17.0' (12397), '>=12.16.0' and '>=14.3.0' (12392), '>=11' and '>=11.10.0'
(11382), '^12.17.0 || >=13.2.0' (11191).

The cost of that choice is that the six 2020-era PRs (10484, 10564, 10624,
10678, 10806, 10947) were never CI-tested on node 16. That was checked rather
than assumed: a throwaway node:16-bullseye container at c5785b9a71cc (the
OLDEST commit in the set, Sep 2020) installed, built, and ran both an e2e
suite and a unit suite green -- e2e/__tests__/showConfig.test.ts 1/1 and
packages/jest-core/src/__tests__/SearchSource.test.ts 27/27.

The two overlapping in-tree shards also chose node:16-bullseye, which agrees.


HOW THE GRADED TESTS ARE CHOSEN, AND THE THREE TRAPS IN DOING IT NAIVELY
------------------------------------------------------------------------
`_targets_for()` derives, per PR, the exact set of test FILES the test patch
grades. The obvious implementation -- "changed files ending in .test.ts" --
is wrong here in three separate ways, each of which was found by reading all
twenty test patches before writing this file:

  1. THREE GRADED e2e FILES DO NOT CONTAIN `.test.`
     e2e/__tests__/multipleConfigs.ts   (PRs 12510, 11922)
     e2e/__tests__/detectOpenHandles.ts (PR 11382)
     jest's root config has no custom `testMatch`, so jest's DEFAULT applies,
     and its first pattern is `**/__tests__/**/*.[jt]s?(x)` -- every file in a
     __tests__ directory, suffix or not. A `\.(test|spec)\.` filter drops
     these three and loses the entire e2e half of those PRs' signal.

  2. PR 12392 CHANGES NO TEST FILE AT ALL
     Its test patch touches only two snapshots and two e2e fixtures:
         e2e/__tests__/__snapshots__/moduleNameMapper.test.ts.snap
         e2e/__tests__/__snapshots__/nativeEsm.test.ts.snap
         e2e/native-esm/__tests__/native-esm.test.js   (a FIXTURE, see 3)
         e2e/native-esm/staticDataImport.js
     The graded behaviour lives in the snapshots of two UNCHANGED test files.
     So a changed `__snapshots__/X.snap` is mapped back to its owning test file
     `X`. Without that rule PR 12392 reports ZERO tests, which is precisely the
     zero rule 11 forbids. The same rule also recovers extra real targets for
     11331, 10806, 10624 and 10484.

  3. e2e FIXTURE TESTS ARE NOT TESTS
     e2e/<name>/__tests__/*.test.js are inputs that a driver in
     e2e/__tests__/ spawns jest against. jest's own config excludes them via
     `testPathIgnorePatterns: ['/e2e/.*/__tests__', ...]`. Grading them would
     invent tests that jest never runs. The full ignore list from
     jest.config.js is reproduced in `_JEST_IGNORE` and applied, which also
     correctly drops packages/jest-cli/src/init/__tests__/fixtures/... in
     PR 10564.

After all three corrections every one of the twenty PRs has at least one graded
target: 52 normal targets plus 1 type test, 53 in total.


ONE JEST INVOCATION PER TARGET FILE (rule 11)
----------------------------------------------
Each target file is run in its OWN `jest` process, not all of them in one call.
jest already isolates test files into workers, but it does NOT isolate the
things above that layer: a module-level throw during collection, a config
error, a worker that hard-exits. In one combined invocation any of those can
end the run and take every other target's results with it, which is the
surrealdb pr-2465 failure reproduced in a different runner.

Results are read from jest's `--json` output rather than scraped from console
text. That matters for two reasons: the JSON carries `pending`/`todo` statuses
so SKIPPED reaches the report instead of vanishing, and it carries the suite
`name`, so every id is `jest::<file>::<ancestors > title>` and can never
collide with an identically-named test in another file. The nine older in-tree
shards scrape `✓ <name>` with no file prefix and do collide.

If a target file exists but produces no parseable JSON -- jest crashed, the
process was killed -- jest_report.js emits

    jest::<file>::<suite failed to run> FAILED

so an aborted run is reported as a failure rather than as an absence. An
absence would delete exactly the signal an F2P instance is built on.

A target that does NOT exist in the current act is skipped silently, and that
is correct rather than lax: a brand-new test file genuinely does not exist at
the base commit, and inventing a result for it would be fabricating data. Two
PRs additionally RENAME a test file (12487 moves getNoTestsFoundMessage.test.js
to .ts; 10678's test_sequencer.test.js is later renamed), so both spellings are
targets and exactly one of them exists in any given act.


THE HONEST BOUNDARY, stated rather than papered over
-----------------------------------------------------
PR 11054's only graded target, e2e/__tests__/consoleDebugging.test.ts, is a new
file. Its RUN (baseline) act therefore reports zero results for that instance.
That is the correct reading of a test that did not exist yet, not a defect --
the TEST and FIX acts both report it, and the transition is N2P.

Three PRs -- 12546, 12487, 10678 -- carry production SOURCE files inside their
test patch, not only tests:

    12546  packages/jest-test-sequencer/src/index.ts
    12487  packages/jest-core/src/{getNoTestFound,getNoTestFoundVerbose,
           getNoTestsFoundMessage}.ts
    10678  packages/jest-core/src/{getNoTestFoundFailed,
           getNoTestsFoundMessage}.ts, packages/jest-test-sequencer/src/index.ts

So part of the implementation is already present in the TEST act, and the unit
tests that exercise those functions directly may pass there rather than fail.
Nothing is broken by this: each of the three still has targets that depend on
code only the FIX patch adds (12546's parseShardPair.test.ts and the shard e2e
test; 12487's and 10678's e2e drivers, which spawn the built CLI), so F2P is
never empty. It does mean the build MUST rerun after patching in every act,
which is why run_tests.sh builds rather than relying on prepare.sh's build.

The test patch and the fix patch of every PR in this dataset were checked for
overlapping files: there are NONE, so `apply_patch.sh test.patch fix.patch`
cannot conflict.


WHY THE BUILD RUNS IN EVERY ACT
--------------------------------
e2e drivers spawn `packages/jest-cli/bin/jest.js`, which does
`require('..')`, and packages/jest-cli/package.json says
`"main": "./build/index.js"`. Cross-package imports in the unit tests resolve
the same way. So patched `src/` is invisible to the tests until
`node ./scripts/build.js` regenerates `build/`. Measured cost in-container:
6.5s at the oldest commit, 7.6s at the newest -- cheap enough to run
unconditionally in all three acts, which also keeps the acts symmetrical.

`scripts/buildTs.js` is NOT run for the nineteen PRs that need only JavaScript;
it emits .d.ts, which no jest run reads. It IS run for PR 12484, whose graded
type test is type-checked against those declarations.


STRUCTURE -- rule 9 (2026-09-03), which supersedes rules 4/5/8 on placement:

    base Dockerfile   toolchain, infra block, apt, ENV, WORKDIR /home/,
                      git clone, CMD. NOTHING after the clone.
    PR Dockerfile     FROM base, COPY lines, ARG BASE_COMMIT, RUN prepare.sh,
                      WORKDIR, then the FULL hardening block with all four
                      asserts.
    prepare.sh        checkout, then dependency install and first build.
                      No stripping, no hardening.

Two harness details make that placement work, both read from
multi_swe_bench/harness/image.py rather than assumed:

  * `DockerfileEnhancer.enhance()` returns the Dockerfile untouched when it
    already carries the BuildKit syntax directive (image.py:317). The base
    below emits that directive itself, so `_inject_final_sanitize()` -- which
    would otherwise append the hardening block before the CMD, putting the
    scrub back into the base -- never runs. The infrastructure block is
    produced by calling `DockerfileEnhancer._infrastructure_block()` directly
    so it cannot drift from what every other image in the tree receives.
  * The scrub runs in an Image-dependency layer, and those receive NO build
    args (build_dataset.py passes REPO_URL/BASE_COMMIT only when
    `dependency()` returns a str). So the PR Dockerfile carries the sha itself
    as `ARG BASE_COMMIT="<sha>"`.

Check before running, inverted from rule 5 exactly as rule 9 requires:
`grep -c 'rev-list --all --count'` must be 0 in the base Dockerfile and 1 in
the PR Dockerfile.

Every generated artifact ships WITHOUT comments (rule 10). The reasoning for
each one sits in a Python comment directly above its string literal.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# node:16 is required by PR 11331's `onNodeVersions('^16.0.0')` guard and was
# verified green at the OLDEST commit in the range; see the module docstring.
# The `-bullseye` suffix is load-bearing rather than cosmetic: the bare `node:16`
# tag has moved between Debian releases, and an unsupported Debian's apt pool
# 404s on the very package versions its own index advertises.
NODE_IMAGE = "node:16-bullseye"

# The base tag is fixed by this config's own interval endpoints, NOT by an
# enumerated PR list, so every PR in the shard resolves to the SAME base image
# regardless of which ones are built or in what order.
_BASE_IMAGE_TAG = "base-12546_to_10484"


# ---------------------------------------------------------------------------
# Graded-target derivation.
#
# `_JEST_IGNORE` is jest.config.js's own `testPathIgnorePatterns`, copied
# verbatim from the repo (checked at both ends of the range; the newer commits
# add '/__typetests__/', '/packages/jest-repl/src/__tests__/test_root' and
# '/packages/.*/tsconfig.*', the older ones add '/test-types/' -- the union is
# used, since a pattern that matches nothing at a given commit is harmless).
#
# `_TEST_MATCH_RE` is jest's DEFAULT testMatch. jest.config.js sets no custom
# testMatch at any commit in this range, so the default is what actually
# selects files -- and its first pattern takes EVERY file under a __tests__
# directory, which is why the three graded e2e files without `.test.` in their
# names are real targets. See trap 1 in the module docstring.
# ---------------------------------------------------------------------------
_JEST_IGNORE = (
    r"/__arbitraries__/",
    r"/__typetests__/",
    r"/node_modules/",
    r"/examples/",
    r"/test-types/",
    r"/e2e/.*/__tests__",
    r"/e2e/global-setup",
    r"/e2e/global-teardown",
    r"/packages/.*/build",
    r"/packages/.*/tsconfig\.",
    r"/packages/.*/src/__tests__/setPrettyPrint\.ts",
    r"/packages/jest-core/src/__tests__/test_root",
    r"/packages/jest-core/src/__tests__/__fixtures__/",
    r"/packages/jest-cli/src/init/__tests__/fixtures/",
    r"/packages/jest-haste-map/src/__tests__/haste_impl\.js",
    r"/packages/jest-haste-map/src/__tests__/dependencyExtractor\.js",
    r"/packages/jest-haste-map/src/__tests__/test_dotfiles_root/",
    r"/packages/jest-repl/src/__tests__/test_root",
    r"/packages/jest-resolve-dependencies/src/__tests__/__fixtures__/",
    r"/packages/jest-runtime/src/__tests__/defaultResolver\.js",
    r"/packages/jest-runtime/src/__tests__/module_dir/",
    r"/packages/jest-runtime/src/__tests__/NODE_PATH_dir",
    r"/packages/jest-snapshot/src/__tests__/plugins",
    r"/packages/jest-snapshot/src/__tests__/fixtures/",
    r"/packages/jest-validate/src/__tests__/fixtures/",
    r"/packages/jest-worker/src/__performance_tests__",
    r"/packages/pretty-format/perf/test\.js",
    r"/e2e/__tests__/iterator-to-null-test\.ts",
)
_IGNORE_RE = tuple(re.compile(pattern) for pattern in _JEST_IGNORE)

_TEST_MATCH_RE = (
    re.compile(r"(^|/)__tests__/.*\.[jt]sx?$"),
    re.compile(r"(^|/)[^/]*(\.|^)(spec|test)\.[jt]sx?$"),
)

_SNAPSHOT_RE = re.compile(r"(^|/)__snapshots__/([^/]+)\.snap$")
_TYPETEST_RE = re.compile(r"/__typetests__/.*\.[jt]sx?$")


def _is_ignored(path: str) -> bool:
    return any(rx.search("/" + path) for rx in _IGNORE_RE)


def _is_jest_test(path: str) -> bool:
    if path.endswith(".snap") or _is_ignored(path):
        return False
    return any(rx.search(path) for rx in _TEST_MATCH_RE)


def _patch_paths(patch) -> list:
    return re.findall(r"^diff --git a/\S+ b/(\S+)", str(patch or ""), re.M)


def _targets_for(pr) -> tuple:
    """Return (normal targets, type-test targets) for one PR, sorted.

    A changed `__snapshots__/X.snap` maps back to its owning test file `X`,
    which is the only thing that gives PR 12392 any graded test at all.
    """
    normal, typetests = set(), set()
    for path in _patch_paths(pr.test_patch):
        if path.endswith(".snap"):
            covered = _SNAPSHOT_RE.sub(r"\1\2", path)
            if covered == path:
                continue
            if _TYPETEST_RE.search("/" + covered):
                typetests.add(covered)
            elif _is_jest_test(covered):
                normal.add(covered)
            continue
        if _TYPETEST_RE.search("/" + path):
            typetests.add(path)
        elif _is_jest_test(path):
            normal.add(path)
    return sorted(normal), sorted(typetests)


# jest treats a positional argument as a REGEX matched against the absolute
# test path, so every metacharacter in a real path has to be escaped and the
# pattern anchored, or `args.test.ts` would also match `argsXtestYts`. The
# escaping is done here, in Python, rather than with a sed inside the shell
# script: a shell-side escape would need its backslashes doubled through this
# template, which is a trap this project has been caught by before.
_REGEX_META = re.compile(r"([.\[\]{}()*+?^$|\\])")


def _path_pattern(path: str) -> str:
    return _REGEX_META.sub(r"\\\1", path) + "$"


# Packages a patch ADDS to a real dependency block of a packages/*/package.json.
#
# This exists because prepare.sh installs dependencies at image-build time, which
# is BEFORE any patch is applied. A PR whose fix introduces a new runtime
# dependency therefore runs against a tree where that dependency was never
# installed. PR 10564 ("add support for the jest.config.ts configuration file")
# is exactly that case: its fix patch adds ts-node, typescript and
# @types/micromatch to packages/jest-config/package.json, and without them all
# four graded tests died with
#
#     Error: Cannot find module 'ts-node'
#     Require stack: .../packages/jest-config/build/readConfigFileAndSetRootDir.js
#
# which the harness scored as a PASS-to-FAIL regression and marked the instance
# invalid. Measured on the first full run, 2026-09-10.
#
# Only blocks that are genuinely dependency maps are read. An earlier, naive
# version of this scan matched any added `"key": "value"` line and so picked up
# `"testEnvironment": "node"` and `"type": "module"` out of e2e FIXTURE
# package.json files, which are jest config, not packages.
_DEP_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)


def _added_deps(patch) -> dict:
    added = {}
    for section in re.split(r"(?m)^(?=diff --git )", str(patch or "")):
        header = re.match(r"diff --git a/\S+ b/(\S+/package\.json)", section)
        if not header or not header.group(1).startswith("packages/"):
            continue
        current = None
        for line in section.splitlines():
            body = line[1:] if line[:1] in "+- " else line
            opened = re.match(r'\s*"([A-Za-z]+)":\s*\{', body)
            if opened:
                current = opened.group(1) if opened.group(1) in _DEP_SECTIONS else None
                continue
            if line.startswith("+") and current:
                dep = re.match(r'\+\s*"(@?[\w.\-/]+)":\s*"([^"]+)"\s*,?\s*$', line)
                if dep:
                    added[dep.group(1)] = dep.group(2)
    return added


def _extra_deps_for(pr) -> dict:
    deps = _added_deps(pr.fix_patch)
    deps.update(_added_deps(pr.test_patch))
    return deps


# True when this PR's fix lands ONLY in jest-jasmine2.
#
# jest ships two test runners and jest-circus is the default from v27 on, so a
# fix confined to jest-jasmine2 changes nothing under the default runner and its
# graded tests pass with and without the fix -- no F2P, instance invalid. PR
# 11382 ("Detect open handles with done callbacks") is that case: measured on the
# first full run its three new tests passed in the TEST act, and the test file
# says so itself, with a comment explaining that it normalises the two runners'
# call-stack names "so the test works in both environments".
#
# jest grades this upstream as a SEPARATE CI job. package.json carries
#     "jest-jasmine": "JEST_JASMINE=1 yarn jest"
# and .github/workflows/nodejs.yml runs a `test-jasmine` job alongside the normal
# one. The switch is read in packages/jest-runner/src/runTest.ts:
#     process.env.JEST_JASMINE === '1' ? 'jest-jasmine2' : config.testRunner
# which is present at PR 11382's base commit (3028bb1968d3), checked rather than
# assumed. It is NOT present at the oldest commit in this range, which is why
# this is applied per PR instead of globally.
def _needs_jasmine(pr) -> bool:
    touched = re.findall(r"^diff --git a/\S+ b/(\S+)", str(pr.fix_patch or ""), re.M)
    jasmine = [f for f in touched if f.startswith("packages/jest-jasmine2/")]
    circus = [f for f in touched if f.startswith("packages/jest-circus/")]
    return bool(jasmine) and not circus


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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


# apply_patch.sh -- plain `git apply` first, `--3way` only as a fallback, so a
# genuine apply failure is still visible from the primary attempt rather than
# being papered over by the fallback. The test and fix patches of all twenty
# PRs were checked for overlapping files and there are none, so the fallback
# should never fire; it exists so that if it ever does, the log says so.
# No patch in this dataset carries binary hunks.
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/jest
for patch in "$@"; do
  if ! git apply --whitespace=nowarn "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
"""


# jest_report.js -- turn one jest --json result file into per-test lines.
#
# Reading the JSON rather than scraping console output is what makes SKIPPED
# reach the report (`pending` and `todo` statuses never print a ✓ or a ✕) and
# what puts the owning FILE into every id, so two same-named tests in different
# files cannot collapse into one.
#
# The absent/empty cases are deliberately NOT silent. A missing or unparseable
# results file means jest died before writing it, and a suite with zero
# assertion results means the file failed to collect. Both are reported as a
# FAILED pseudo-test rather than as nothing at all, because an absence would
# read to report.py as "this test does not exist in this act", which is how a
# crash quietly manufactures a false transition.
_JEST_REPORT_JS = """const fs = require('fs');

const jsonPath = process.argv[2];
const target = process.argv[3];
const MARKER = '/home/jest/';

const STATUS = {
    passed: 'PASSED',
    failed: 'FAILED',
    pending: 'SKIPPED',
    skipped: 'SKIPPED',
    todo: 'SKIPPED',
    disabled: 'SKIPPED',
};

function relative(name) {
    const file = String(name || '').replace(/\\\\/g, '/');
    const at = file.indexOf(MARKER);
    return at === -1 ? file : file.slice(at + MARKER.length);
}

function emit(file, name, status) {
    console.log('jest::' + file + '::' + name + ' ' + status);
}

let report = null;
try {
    report = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
} catch (e) {
    report = null;
}

if (!report || !Array.isArray(report.testResults) || report.testResults.length === 0) {
    emit(target, '<suite failed to run>', 'FAILED');
    process.exit(0);
}

for (const suite of report.testResults) {
    const file = relative(suite.name) || target;
    const results = suite.assertionResults || [];
    if (results.length === 0) {
        emit(file, '<suite failed to run>', 'FAILED');
        continue;
    }
    for (const assertion of results) {
        const parts = [].concat(assertion.ancestorTitles || [], [assertion.title]);
        emit(file, parts.join(' > '), STATUS[assertion.status] || 'SKIPPED');
    }
}
"""


# run_tests.sh -- one jest process per graded target file.
#
# --ci is not optional. Without it jest WRITES a missing snapshot and reports
# the test as passing, so a test act that should fail on an unwritten snapshot
# would pass instead and the F2P transition would disappear.
#
# --runInBand because the e2e drivers spawn child jest processes of their own;
# jest's own CI runs its e2e suite with -i for the same reason.
#
# No --testTimeout: the repo's own testSetupFile.js calls jest.setTimeout(70000)
# and a setupFilesAfterEach timeout OVERRIDES the CLI flag, so passing one would
# be a no-op that looks like a control.
#
# The build runs here rather than only in prepare.sh so that all three acts are
# symmetrical and so that patched src/ is visible to tests that import build/.
#
# NODE_OPTIONS caps the V8 old-space at 1536 MB rather than the more usual 4096.
# That is sized to the RUNNER, not to jest: the pipeline is run with
# --max_workers_run_instance 4 on a Docker VM holding 7.75 GB, so a 4096 cap
# would let four concurrent instances ask for 16 GB and the kernel would start
# OOM-killing workers. A killed worker prints no result line, which is exactly
# the failure mode rule 11 exists to prevent -- jest_report.js would report the
# whole file as "<suite failed to run> FAILED" and manufacture noise that looks
# like a real regression. 1536 x 4 = 6 GB fits with headroom, and it is a CAP
# rather than a reservation: jest's own suites here peak in the low hundreds of
# MB, so nothing is actually constrained by it. Note the e2e drivers spawn child
# jest processes which INHERIT NODE_OPTIONS, so the cap applies to them too.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

cd /home/jest

export CI=true
export TZ=UTC
export FORCE_COLOR=1
export BROWSERSLIST_IGNORE_OLD_DATA=1
export NODE_OPTIONS="--max-old-space-size=1536"
export PATH="/home/jest/node_modules/.bin:$PATH"
__JASMINE__

JEST=./packages/jest-cli/bin/jest.js
OUT=/tmp/jest.out
RESULTS=/tmp/jest.results
: > "$OUT"
: > "$RESULTS"

node --version >> "$OUT" 2>&1

__ENSURE_DEPS__
echo "===== build =====" >> "$OUT"
node ./scripts/build.js >> "$OUT" 2>&1 || echo "run_tests: scripts/build.js failed" >> "$OUT"
__BUILD_TS__

if [ ! -f packages/jest-cli/build/index.js ]; then
  echo "run_tests: packages/jest-cli/build/index.js missing after build" >> "$OUT"
fi

idx=0

run_one() {
  target="$1"
  pattern="$2"
  cfg="$3"

  if [ ! -f "$target" ]; then
    echo "run_tests: target absent in this act, skipping: $target" >> "$OUT"
    return 0
  fi

  if [ "$cfg" = "AUTO_TSD" ]; then
    cfg=""
    for candidate in jest.config.tsd.js jest.config.types.js; do
      if [ -f "$candidate" ]; then
        cfg="$candidate"
        break
      fi
    done
    if [ -z "$cfg" ]; then
      echo "run_tests: no type-test config found for $target" >> "$OUT"
      return 0
    fi
  fi

  idx=$((idx + 1))
  json="/tmp/jest-result-$idx.json"
  rm -f "$json"

  echo "===== $target =====" >> "$OUT"
  if [ -n "$cfg" ]; then
    node "$JEST" --ci --runInBand --json --outputFile="$json" --config "$cfg" "$pattern" >> "$OUT" 2>&1
  else
    node "$JEST" --ci --runInBand --json --outputFile="$json" "$pattern" >> "$OUT" 2>&1
  fi
  rc=$?
  echo "run_tests: $target exited $rc" >> "$OUT"

  node /home/jest_report.js "$json" "$target" >> "$RESULTS" 2>> "$OUT"
}

__RUN_ONE_CALLS__

cat "$OUT"
echo "graded target files: __TARGET_COUNT__"
echo "result lines: $(wc -l < "$RESULTS")"
echo "----- per-test results -----"
cat "$RESULTS"
"""


def _run_tests_sh(pr) -> str:
    """Render run_tests.sh for one PR.

    Rendered per PR because the graded target list differs per PR. Everything
    else in the script is identical for every PR and for all three acts, so the
    acts cannot drift apart within an instance -- the same file is executed by
    run.sh, test-run.sh and fix-run.sh.
    """
    normal, typetests = _targets_for(pr)

    calls = []
    for target in normal:
        calls.append(f'run_one "{target}" "{_path_pattern(target)}" ""')
    for target in typetests:
        calls.append(f'run_one "{target}" "{_path_pattern(target)}" "AUTO_TSD"')

    # buildTs.js only matters for the type tests, which read the emitted .d.ts
    # files. It is a full tsc pass over ~40 packages, so the nineteen PRs
    # without a type test do not pay for it.
    build_ts = (
        'node ./scripts/buildTs.js >> "$OUT" 2>&1'
        ' || echo "run_tests: scripts/buildTs.js failed" >> "$OUT"'
        if typetests
        else ":"
    )

    # PR 11382's fix is confined to jest-jasmine2, which the default runner
    # (jest-circus) never loads -- see _needs_jasmine() for the evidence.
    jasmine = "export JEST_JASMINE=1" if _needs_jasmine(pr) else ""

    # A patch may DECLARE a dependency that the image never installed, because
    # prepare.sh installs before any patch exists. PR 10564 is that case: its fix
    # adds ts-node to the root package.json and to yarn.lock, and without it all
    # four graded files died on `Cannot find module 'ts-node'`, which the harness
    # scored as a PASS-to-FAIL regression.
    #
    # Everything about this block was measured inside the pr-10564 image on
    # 2026-09-10 rather than reasoned about, because the first attempt at fixing
    # it made things worse:
    #
    #   * Installing the packages in prepare.sh with `yarn add` is WRONG. It
    #     rewrites package.json and yarn.lock, which are the very files the fix
    #     patch also edits, so `git apply` then fails, falls back to --3way and
    #     leaves `U yarn.lock`. The fix act produced 0/0/0 instead of 50/4/0.
    #   * An INCREMENTAL `yarn install` after patching is not enough. yarn 2.3.1
    #     aborts its Link step with
    #         TypeError: Cannot read properties of null (reading 'packageLocation')
    #     linking ts-node but not typescript, and a retry fails identically.
    #     `rm -rf node_modules` first makes it succeed: rc=0 in 36s.
    #   * The trigger must test BOTH that the package is missing AND that a
    #     manifest now declares it, or the run and test acts would reinstall
    #     pointlessly on every act. Measured per act: run -> nothing,
    #     test -> nothing, fix -> ts-node only.
    #   * `git diff -- 'packages/*/package.json'` cannot be used to detect the
    #     manifest change: git pathspecs match across directory separators, so it
    #     also matched packages/jest-cli/src/init/__tests__/fixtures/*/package.json
    #     and fired in the test act. A shell glob plus a grep for the package name
    #     is precise.
    #
    # Emitted only for PRs whose patches add a dependency, so eighteen of the
    # twenty never carry this code at all.
    extras = sorted(_extra_deps_for(pr))
    if extras:
        names = " ".join(f'"{name}"' for name in extras)
        ensure_deps = (
            "ensure_patch_deps() {\n"
            "  needs=\"\"\n"
            f"  for m in {names}; do\n"
            '    if [ ! -d "node_modules/$m" ] && grep -qs "\\"$m\\"" package.json packages/*/package.json; then\n'
            '      needs="$needs $m"\n'
            "    fi\n"
            "  done\n"
            '  if [ -z "$needs" ]; then\n'
            "    return 0\n"
            "  fi\n"
            '  echo "run_tests: patch declares$needs but not installed, reinstalling" >> "$OUT"\n'
            '  YARN="$(ls .yarn/releases/yarn*.cjs 2>/dev/null | head -1)"\n'
            '  if [ -z "$YARN" ]; then\n'
            "    return 0\n"
            "  fi\n"
            "  rm -rf node_modules\n"
            '  YARN_ENABLE_IMMUTABLE_INSTALLS=false node "$YARN" install >> "$OUT" 2>&1 || true\n'
            "  for m in $needs; do\n"
            '    if [ -d "node_modules/$m" ]; then\n'
            '      echo "run_tests: installed $m" >> "$OUT"\n'
            "    else\n"
            '      echo "run_tests: WARNING $m still missing" >> "$OUT"\n'
            "    fi\n"
            "  done\n"
            "}\n"
            "ensure_patch_deps\n"
        )
    else:
        ensure_deps = ":"

    return (
        _RUN_TESTS_SH.replace("__BUILD_TS__", build_ts)
        .replace("__JASMINE__", jasmine)
        .replace("__ENSURE_DEPS__", ensure_deps)
        .replace("__RUN_ONE_CALLS__", "\n".join(calls))
        .replace("__TARGET_COUNT__", str(len(normal) + len(typetests)))
    )


class JestEraImageBase(Image):
    """Shared era base (`base-12546_to_10484`): toolchain + clone only.

    Rule 9: nothing after the `git clone`. The image therefore keeps the FULL
    git history, which is what makes one base safe for all nineteen base
    commits -- and all nineteen were checked to be ancestors of `main`, so a
    plain clone reaches every one of them and no PR has to fetch anything back.
    The pin and the prune happen per PR, in the PR layer.
    """

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
        return _BASE_IMAGE_TAG

    def workdir(self) -> str:
        return _BASE_IMAGE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()

        # Emitting the syntax directive ourselves makes DockerfileEnhancer a
        # no-op for this image (image.py:317), which is the only way to keep the
        # scrub OUT of the base -- its _inject_final_sanitize() would otherwise
        # append the hardening block before the CMD. The infra block is
        # generated by the enhancer's own helper so it stays identical to what
        # every other image in the tree receives.
        infra = DockerfileEnhancer._infrastructure_block(self, base_img).rstrip("\n")

        # THERE IS DELIBERATELY NO apt LAYER, and that is a fix rather than an
        # omission. node:16-bullseye is the FULL (buildpack-deps) variant, not
        # -slim, so it already ships every package this stack needs. Verified by
        # running the image on 2026-09-10:
        #
        #     git /usr/bin/git          gcc  /usr/bin/gcc
        #     curl /usr/bin/curl        g++  /usr/bin/g++
        #     make /usr/bin/make        python3    /usr/bin/python3
        #     pkg-config /usr/bin/pkg-config
        #     /etc/ssl/certs/ca-certificates.crt present
        #
        # So the apt line this config originally carried installed nothing that
        # was not already there. It also KILLED the first build attempt:
        #
        #     E: Release file for http://deb.debian.org/debian-security/dists/
        #        bullseye-security/InRelease is expired (invalid since 2d 11h)
        #     ERROR: process "/bin/sh -c apt-get update && apt-get install ..."
        #            did not complete successfully: exit code: 100
        #
        # Debian bullseye's security suite reached end of life and apt refuses an
        # expired Release file. `-o Acquire::Check-Valid-Until=false` would paper
        # over it, but that keeps a network layer that fetches nothing useful and
        # the expiry only grows. Removing the layer removes the whole class of
        # failure, and the QC prompt sanctions it explicitly: D10 says a minimal
        # or absent apt block is legitimate for images that already ship the
        # toolchain, and the TypeScript row of its Pass C table lists the apt
        # line as "often NONE".
        #
        # D10's hard floor is still met, just by the base image rather than by
        # apt: git is present (needed for the clone) and ca-certificates is
        # present (needed for TLS through the proxy).

        # Only the clone itself. The QC's shared-base reference is exactly this
        # one command, and everything this line used to carry around it was
        # removed on request as unnecessary:
        #
        #   * a 5-attempt retry loop  -- insurance against a dropped connection.
        #     It was carried over from openclaw, where a 4 GB history cloned
        #     twice concurrently for a multi-arch build really did get truncated.
        #     jest is 377 MB and this is a single-arch build, and the clone
        #     succeeded on the first attempt when measured.
        #   * http.lowSpeedLimit / lowSpeedTime -- abort a stalled transfer so a
        #     retry can fire. Pointless once the retry loop is gone.
        #   * http.postBuffer -- sized the buffer for POSTing data to a remote,
        #     which only affects `git push`. It never did anything for a clone.
        #   * test -d /home/<repo>/.git -- was load-bearing ONLY because the
        #     retry loop swallowed the clone's exit code: the loop's last command
        #     was `sleep 30`, so five failed clones still exited 0. With the loop
        #     gone a failed `git clone` fails the RUN by itself, which is the
        #     behaviour the assert was restoring.
        #
        # The `"${REPO_URL}"` spelling is kept: D11 requires cloning from the ARG
        # rather than a hardcoded URL, and the clone stays full-history and
        # unpinned so every one of the nineteen base commits stays reachable.
        repo = self.pr.repo
        clone = f'RUN git clone "${{REPO_URL}}" /home/{repo}'

        # TZ is pinned because several graded e2e suites snapshot formatted
        # output. CI=true is what makes jest's own reporters take their
        # non-interactive path, matching how the repo is tested upstream.
        #
        # FORCE_COLOR=1 is REQUIRED and was measured, not guessed. jest snapshots
        # its own chalk-coloured output through the pretty-format/ConvertAnsi
        # serializer, so the stored snapshots contain markers like
        # `<yellow><bold>...</>`. chalk emits those codes only at colour level
        # >= 1. On GitHub Actions supports-color returns level 1 because
        # CI + GITHUB_ACTIONS are both set, which is the environment every one of
        # these snapshots was recorded in. A bare container has no TTY and no
        # GITHUB_ACTIONS, so the level is 0, every colour marker vanishes and the
        # snapshot diff is pure formatting. Setting NO_COLOR=1/FORCE_COLOR=0 --
        # the usual instinct for machine-readable logs -- makes it strictly
        # worse by forcing level 0 even where chalk would have picked 1.
        # Measured in-container on 2026-09-10 at 871a8e7c:
        #     NO_COLOR=1 FORCE_COLOR=0 -> normalize.test.ts  107/131, 24 FAILED
        #     FORCE_COLOR=1            -> normalize.test.ts  131/131, 0 FAILED
        # Nothing downstream is harmed by colour: jest_report.js reads the
        # --json file, and parse_log strips ANSI before matching.
        #
        # BROWSERSLIST_IGNORE_OLD_DATA=1 silences
        #     "Browserslist: caniuse-lite is outdated. Please run: ..."
        # which browserslist prints whenever its bundled data is older than a few
        # months. Every commit in this range is from 2020-2022, so it always
        # fires. It does two kinds of damage. The e2e drivers capture the spawned
        # jest's stdout and snapshot it, so the three nag lines are diffed against
        # the stored snapshot; and the same warning during `scripts/build.js`
        # shifts line numbers in the emitted build/*.js, which snapshots that
        # quote a stack frame then also miss. Measured in-container at 4590b9df:
        #     unset                      -> moduleNameMapper.test.ts 1/5, 4 FAILED
        #     set, without a rebuild     -> 3/5, 2 FAILED
        #     set, with a rebuild        -> 5/5, 0 FAILED
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_img}

{infra}

WORKDIR /home/

RUN git config --global --add safe.directory '*'

ENV CI=true
ENV TZ=UTC
ENV FORCE_COLOR=1
ENV BROWSERSLIST_IGNORE_OLD_DATA=1
ENV SHELL=/bin/bash
ENV HOME=/root
ENV npm_config_update_notifier=false

{clone}

CMD ["/bin/bash"]
"""


class JestEraImageDefault(Image):
    """Per-PR image (`pr-<N>`): COPY, prepare, then the full history scrub."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return JestEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        normal, _ = _targets_for(self.pr)

        # Only PR 11191 grades e2e/__tests__/transform.test.ts, and that suite
        # calls runYarnInstall() SEVEN times, each of which shells out to
        # `yarn install` inside an e2e/transform/* fixture at TEST time. Warming
        # those installs here, at image build time, means the act does not
        # depend on a live npm registry mid-run. It is a no-op for the other
        # nineteen PRs, which is why it is conditional rather than always on.
        prewarm = (
            "for fixture in e2e/transform/*/; do\n"
            '  if [ -f "$fixture/package.json" ]; then\n'
            '    (cd "$fixture" && yarn install >/dev/null 2>&1) || true\n'
            "  fi\n"
            "done\n"
            if "e2e/__tests__/transform.test.ts" in normal
            else ""
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH),
            File(".", "jest_report.js", _JEST_REPORT_JS),
            File(".", "run_tests.sh", _run_tests_sh(self.pr)),
            File(
                ".",
                # prepare.sh follows rule 8's structure: dependency work,
                # cd /home/<repo>, git reset --hard, git checkout <sha>, with
                # check_git_changes.sh asserts on both sides of the pin.
                #
                # The ORDER is checkout FIRST, install second. yarn reads
                # package.json and yarn.lock from the working tree, so
                # installing before the pin would install the default branch's
                # dependency set and then leave it stale against the pinned
                # tree.
                #
                # Emitted WITHOUT comments (rule 10). The reasoning lives here:
                #
                #  * the pin needs no fetch-back in the normal case, because the
                #    base kept full git history (rule 9) and all nineteen base
                #    commits are ancestors of main. The `cat-file -e` guard with
                #    a fetch fallback is kept anyway, so that a force-push
                #    upstream produces a clear error instead of a confusing
                #    checkout failure.
                #  * the vendored yarn under .yarn/releases is invoked directly.
                #    That is what makes one base serve five different yarn
                #    versions without corepack and without a network fetch of a
                #    package manager. The `test -n` assert is deliberate: if a
                #    commit ever lacked a vendored release, silently falling
                #    back to the image's yarn 1.22 would install a completely
                #    different dependency tree.
                #  * `--immutable` first, because the lockfile is what makes the
                #    install reproducible. The retry exists because this graph
                #    downloads prebuilt binaries during install and one flaky
                #    download should not cost the whole image. Only the last
                #    attempt drops `--immutable`.
                #  * the install chain ends in `|| true`, which the QC prompt
                #    requires (check 3A). Swallowing the exit code alone would be
                #    dangerous, so the REAL assert is the hard gate at the END of
                #    the script: if the tree cannot build, cannot report a jest
                #    version, or cannot resolve the transform's own dependency,
                #    the image build fails loudly. A partially-installed tree
                #    therefore survives; an unusable one does not.
                #  * the gate is the LAST thing the script does, and it is not
                #    wrapped in `|| true`. `@babel/core` is asserted alongside the
                #    jest binary on purpose: it is a devDependency the TESTS need
                #    (every file is transformed by packages/babel-jest) but which
                #    `jest --version` alone would not exercise, and it is present
                #    at both ends of the range (^7.3.4 at c5785b9a and 871a8e7c).
                #  * there is deliberately NO clean-tree assert after the install.
                #    The install and build are allowed to leave the tree dirty,
                #    and the hardening block that follows must NOT clean it -- a
                #    `git reset --hard` there would silently revert prepare.sh's
                #    work. `git clean -fdx` appears once, at the TOP, before any
                #    dependency exists to be destroyed by it.
                #  * the checkout is `--detach`. The hardening block asserts
                #    HEAD == BASE_COMMIT and then deletes every ref; if HEAD were
                #    still on a branch, that branch would either be deleted out
                #    from under it or keep extra history reachable.
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags origin {pr.base.sha} 2>/dev/null \\
    || git fetch --no-tags origin 2>/dev/null \\
    || true
git cat-file -e {pr.base.sha}^{{commit}}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
bash /home/check_git_changes.sh

node --version

YARN="$(ls .yarn/releases/yarn*.cjs 2>/dev/null | head -1)"
test -n "$YARN"
echo "prepare.sh: vendored yarn is $YARN"
node "$YARN" --version

node "$YARN" install --immutable \\
    || node "$YARN" install --immutable \\
    || node "$YARN" install \\
    || true

node ./scripts/build.js || true

{prewarm}
if [ ! -f packages/jest-cli/build/index.js ]; then
  echo "prepare.sh: yarn install / build did not produce packages/jest-cli/build/index.js"
  exit 1
fi
node ./packages/jest-cli/bin/jest.js --version
node -e "require('./package.json'); require.resolve('@babel/core'); console.log('DEPS_OK')"
""".format(pr=self.pr, prewarm=prewarm),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                # `set -e` is what makes the patch step FATAL. Without it a
                # patch that failed to apply would fall through to run_tests.sh,
                # which would report clean baseline numbers as though the test
                # patch had landed -- a wrong report that looks perfectly valid.
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                # Same reason as test-run.sh: a silently-skipped patch here
                # would produce a report that credits nothing and blames the
                # config.
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # ARG with a hardcoded default, because this layer's dependency() is an
        # Image and Image-dependency layers receive NO build args. The hardening
        # block below reads it.
        #
        # The block runs AFTER prepare.sh: prepare needs the network and the
        # remote for the yarn install and for the fetch fallback, and the scrub
        # removes the remote.
        return f"""FROM {image_name}

{copies}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("jestjs", "jest_12546_to_10484")
class JEST_12546_TO_10484(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JestEraImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section jest_report.js printed. The raw jest output
        # sits in the same log and its own summary lines also carry the words
        # PASSED and FAILED, so a whole-log scan would invent tests that do not
        # exist.
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what jest_report.js prints. The id is
        # captured non-greedily BEFORE the keyword so nothing can leak into it
        # and manufacture a false transition between acts.
        result_res = (
            (re.compile(r"^(.+?)\s+PASSED$"), "pass"),
            (re.compile(r"^(.+?)\s+FAILED$"), "fail"),
            (re.compile(r"^(.+?)\s+SKIPPED$"), "skip"),
        )

        for line in test_log.splitlines():
            line = line.strip()
            for rx, kind in result_res:
                match = rx.match(line)
                if not match:
                    continue
                name = match.group(1)
                if kind == "pass":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif kind == "fail":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                else:
                    skipped_tests.add(name)
                break

        # TestResult requires the three sets to be disjoint, else it raises.
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
