"""pnpm/pnpm -- PRs 6098 .. 7086 (Feb 2023 .. Sep 2023).

A NEW config file for a NEW dataset. `typescript/pnpm/pnpm.py` in this same
folder is a DIFFERENT dataset (PRs 2965..3206, Nov 2020 .. Feb 2021) and is not
touched: that era still has the flat `packages/<name>` layout, runs half its
suites through tape, and needs node 14 + pnpm 6. Nothing about it transfers.

Shape (rule 4, confirmed with the user): ten PRs, ONE era, ONE shared base.

What makes them one era, checked commit by commit rather than assumed:

  * identical monorepo layout -- `cli/*`, `exec/*`, `fs/*`, `patching/*`,
    `pkg-manager/*`, `pnpm`, all listed in pnpm-workspace.yaml
  * identical runner -- jest 29 + ts-jest 29, one root `jest.config.js` and a
    `jest-with-registry.config.js` whose globalSetup starts @pnpm/registry-mock
  * `lockfileVersion: '6.x'` at every one of the ten base commits
  * node 18 sits inside the CI matrix at BOTH ends of the range (14.6/16/18/19
    at 6098, 16.14/18/20 at 7086)

The one commit that looks like an outlier is 6611, whose base sha
f46cab39fc7c33cc2ec2aa084f54d61576f3b5d8 is NOT on `main` -- it is on the
v7.33.x maintenance line. It needs nothing special here: the sha is reachable
from tags v7.33.0..v7.33.7, so the base image's plain `git clone` already has
it, and rule 9 keeps that clone unpinned with its full history.

Rule 9 layout:

  base Dockerfile   toolchain, proxy/CA, ENV, LABELs, WORKDIR /home/, the
                    clone, CMD. Nothing after the clone.
  PR Dockerfile     FROM base, the COPY lines, RUN bash /home/prepare.sh, then
                    the full strip/hardening with all four asserts.
  prepare.sh        the pin (checkout), the dependency install, the compile.
                    No stripping, no hardening.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


ALIAS_KEY = "pnpm_7086_to_6098"

# Distinct from pnpm.py's bare "base" tag. Both configs build image NAME
# mswebench/pnpm_m_pnpm (image_name() is derived from pr.org and pr.repo), so
# the tag is the only thing separating this era's base from that one's.
BASE_TAG = "base-node18-pnpm8"

# node:18-bullseye.
#
# 18 is the only major inside the CI matrix at BOTH ends of the range, and it
# clears pnpm/bin/pnpm.cjs's own floor, which hard-exits below v16.14:
#     ERROR: This version of pnpm requires at least Node.js v16.14
# 20 would break the 6098 end (its matrix stops at 19) and 16 would be below
# what the 7086 end develops against.
#
# bullseye rather than alpine: several dependencies build native addons, and
# the repo's test suites shell out to a real `git`.
_NODE_IMAGE = "node:18-bullseye"

# Bootstrap pnpm only. prepare.sh replaces it with the exact version the
# commit's own `packageManager` field pins -- see the note there.
_BOOTSTRAP_PNPM = "8.6.10"

_JSON_BEGIN = "===== MSB-JEST-BEGIN"
_JSON_END = "===== MSB-JEST-END"

_REPO_ROOT = "/home/pnpm"

# Per-PR setup that runs AFTER the install and BEFORE the compile, in every
# stage. Empty for nine of the ten PRs.
#
# 6847 is the exception, and it is a dataset property rather than a config
# problem. Its FIX patch bundles a test-fixture upgrade in with the code fix:
#     - "@pnpm/registry-mock": "3.10.2",
#     + "@pnpm/registry-mock": "3.11.0",
# and `@pnpm.e2e/has-failing-postinstall-dep`, which both of its gold tests
# install, ships only in 3.11.0. Verified in the container: 3.10.2's bundled
# storage has `@pnpm.e2e/failing-postinstall` but not that one, and the package
# is 404 on the real npm registry too, so no uplink can supply it.
#
# The consequence is silent. The missing package is requested as an OPTIONAL
# dependency, so pnpm skips it, the install succeeds in 228ms without ever
# running the failing postinstall, and the gold test passes VACUOUSLY:
#     run=NONE  test=PASS  fix=PASS   -> nothing transitions, instance invalid
#
# Supplying the fixture registry -- and nothing else; no product code, no test
# patch, no fix patch is touched -- makes the test discriminate again. Proven
# both ways in the container: with 3.11.0 present the PRE-fix code FAILS the
# test, and the post-fix code passes it. So the transition is caused by the
# missing fix, which is exactly what f2p is supposed to measure.
_EXTRA_SETUP = {
    6847: "pnpm add -w -D @pnpm/registry-mock@3.11.0 > /tmp/msb_extra.log 2>&1 || tail -n 5 /tmp/msb_extra.log",
    # 6207 is the same shape as 6847: the gold test cannot LOAD until something the
    # FIX patch declares is resolvable, so without help the whole suite dies at import
    # and no individual test is ever seen failing.
    #
    #     Test suite failed to run
    #     Cannot find module '@pnpm/graceful-fs' from 'test/createImportPackage.test.ts'
    #
    # The crucial difference from a missing package: `fs/graceful-fs` ALREADY EXISTS as
    # a workspace package at this base commit -- the fix only modifies its src and adds
    # it to fs/indexed-pkg-importer's package.json. So nothing is being introduced here;
    # a package already in the tree is merely made resolvable from the package whose
    # tests need it. No product code, no test patch and no fix patch is touched.
    #
    # Why it matters: with the suite unable to load, all 9 tests came back as
    # test=NONE / run=PASS, which report.py correctly reclassifies to p2p ("Classic
    # CBC"), leaving f2p=0 and n2p=0 -- and the delivered validate_dataset.py rejects
    # that on rule 4. Proven in the container with the link in place: the test stage
    # reports "Tests: 9 failed, 9 total" as INDIVIDUAL failures on
    # `expect(gfs.copyFile).toBeCalledWith(...)`, because the pre-fix implementation
    # still uses plain `fs`. That is exactly the bug the PR fixes, so the transition is
    # caused by the missing fix and nothing else. Fix stage: 9 passed. f2p = 9.
    #
    # -sfn, so the fix stage -- where pnpm install creates the same link from the
    # patched manifest -- is a harmless no-op rather than a conflict.
    6207: (
        'mkdir -p "$ROOT/fs/indexed-pkg-importer/node_modules/@pnpm" && '
        'ln -sfn "$ROOT/fs/graceful-fs" '
        '"$ROOT/fs/indexed-pkg-importer/node_modules/@pnpm/graceful-fs"'
    ),
}

_TARGET_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)

# `+++ b/` lines, not the `diff --git` header: the header still names a file
# the patch deletes, and a deleted path is not a test target.
_TEST_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _patch_paths(patch: Optional[str]) -> list[str]:
    out = []
    for raw in _TARGET_RE.findall(patch or ""):
        path = raw.strip()
        if not path or path == "/dev/null":
            continue
        out.append(path)
    return out


def _test_files(pr: PullRequest) -> list[str]:
    """The test files this PR's test patch touches -- the whole graded scope.

    Deliberately narrower than "every test in the package". `pkg-manager/core`
    alone carries 77 end-to-end suites, the root jest config pins
    `maxWorkers: 1` with a four-minute per-test timeout, and five of these ten
    PRs grade that package. Running it whole would be hours per stage, three
    stages per PR, for p2p coverage that adds nothing to the f2p signal.

    Three filters, each for a concrete reason:
      * only files under a `test/` directory -- that is what the root config's
        testMatch looks at
      * `fixtures/` and `__fixtures__/` are dropped -- PR 6138's test patch
        edits `pkg-manager/core/test/fixtures/.../.babelrc`, which is fixture
        DATA, and the root config's testPathIgnorePatterns excludes it anyway
      * `test/utils/` is dropped for the same reason: the root config ignores
        `<rootDir>/test/utils/.+`, so handing those paths to jest would produce
        a run with no tests rather than an error
    """
    out = []
    for path in _patch_paths(pr.test_patch):
        probe = "/" + path
        if "/test/" not in probe:
            continue
        if "/test/utils/" in probe:
            continue
        if not path.endswith(_TEST_EXT) or path.endswith(".d.ts"):
            continue
        parts = path.split("/")
        if "fixtures" in parts or "__fixtures__" in parts:
            continue
        out.append(path)
    return sorted(set(out))


def _touched_files(pr: PullRequest) -> list[str]:
    """Every path either patch writes to, for the rebuild step.

    Both patches, not just the fix one. The owning package of a graded test
    file has to be rebuilt too, because a test imports its own package by NAME
    (`import { ... } from '@pnpm/core'`) through the self-referential
    `"@pnpm/core": "workspace:*"` devDependency, and that resolves to
    `lib/index.js` -- compiled output, never src.
    """
    return sorted(set(_patch_paths(pr.fix_patch)) | set(_patch_paths(pr.test_patch)))


def _sh_list(names: list[str]) -> str:
    return " ".join(f'"{n}"' for n in names)


# ---------------------------------------------------------------------------
# Scripts copied into the PR image
# ---------------------------------------------------------------------------

# Placeholders are [[NAME]] rather than {} on purpose: every one of these files
# is shell or JSON and is full of braces and backslashes, and str.format would
# have to have each of them doubled by hand. See the backslash trap note in the
# project rulebook.

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -euo pipefail

cd [[ROOT]]

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not inside a git repository"
    exit 1
fi

# Force a real content comparison. A stat-cache hit can report a clean tree
# that is not actually clean, so a build-time check can pass while the shipped
# image carries a dirty worktree.
git update-index -q --really-refresh || true

# Untracked files are excluded: node_modules/, lib/, dist/ and
# tsconfig.tsbuildinfo are all gitignored build output that this image is
# SUPPOSED to carry. A dirty TRACKED file is the real failure -- it means a
# stage mutated the tree and the next stage would not start from the base
# commit.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""


_COMPILE_SH = r"""#!/bin/bash
# Rebuild the workspace packages the patches touch.
#
# This is not optional and it is not an optimisation. Every package here
# declares "main": "lib/index.js", and both cross-package imports and a test
# file's import of its OWN package resolve through the workspace symlink to
# that compiled output. A fix patch edits src/. Without this step the fix is
# simply invisible to jest, and the instance reads as unresolved with nothing
# in the log to say why.
#
# `pnpm exec tsc --build`, never `pnpm run compile`. The repo's own compile
# script is `tsc --build && pnpm run lint --fix`, and eslint --fix both mutates
# tracked sources and exits non-zero on anything it cannot fix -- which a test
# patch's brand-new file can easily trip. That would abort the rebuild and turn
# a real result into an empty one.
#
# tsc --build follows project references, so naming one package also rebuilds
# every workspace package it depends on.
set +e

ROOT=[[ROOT]]
cd "$ROOT" || exit 0

# `pnpm` is always first and always present, even when neither patch touches
# it. Two things downstream need its esbuild bundle:
#   * __fixtures__/package.yaml's prepareFixtures runs `node ../pnpm/dist/pnpm.cjs`
#   * pnpm/bin/pnpm.cjs, which the CLI-spawning suites execute, does
#     `require('../dist/pnpm.cjs')` -- output tsc never produces
# and because esbuild inlines every workspace lib into that bundle, a fix in
# any package has to be re-bundled before a spawned CLI can show it.
DIRS=("pnpm")

TOUCHED=( [[TOUCHED]] )

for p in "${TOUCHED[@]}"; do
    d=$(dirname "$p")
    found=""
    while [ "$d" != "." ] && [ "$d" != "/" ]; do
        if [ -f "$ROOT/$d/package.json" ]; then
            found="$d"
            break
        fi
        d=$(dirname "$d")
    done
    # No match means the path belongs to the repo root (pnpm-lock.yaml, the
    # root package.json, a .changeset entry). The root is not a compilable
    # project, so it is skipped rather than handed to tsc.
    [ -n "$found" ] || continue
    case " ${DIRS[*]} " in
        *" $found "*) continue ;;
    esac
    DIRS+=("$found")
done

for d in "${DIRS[@]}"; do
    [ -f "$ROOT/$d/tsconfig.json" ] || continue
    (
        cd "$ROOT/$d" || exit 0
        echo "compile: $d"
        pnpm exec tsc --build 2>&1 | tail -n 40

        if node -e "process.exit(((require('./package.json').scripts)||{}).bundle?0:1)" 2>/dev/null; then
            rm -rf dist
            pnpm run bundle 2>&1 | tail -n 20
            # The four copies the repo's own compile script makes after
            # bundling. Guarded because their sources move between commits and
            # a missing one must not take the whole rebuild down.
            pnpm exec shx cp -r node-gyp-bin dist/node-gyp-bin > /dev/null 2>&1
            pnpm exec shx cp -r node_modules/@pnpm/tabtab/lib/scripts dist/scripts > /dev/null 2>&1
            pnpm exec shx cp -r node_modules/ps-list/vendor dist/vendor > /dev/null 2>&1
            pnpm exec shx cp pnpmrc dist/pnpmrc > /dev/null 2>&1
        fi
    )
done

exit 0
"""


_RUN_TESTS_SH = r"""#!/bin/bash
# Rebuild, then run jest over exactly this PR's test files.
set +e

ROOT=[[ROOT]]
cd "$ROOT" || exit 0

export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export npm_config_color=false
export npm_config_progress=false
export NODE_OPTIONS=--dns-result-order=ipv4first

# Relink node_modules to whatever manifests are now on disk -- OFFLINE.
#
# A patch can change what the tree depends on, and the image's node_modules
# only knows the base commit: 6207 adds @pnpm/graceful-fs, 6496 adds
# @pnpm/which, 6687 moves to @pnpm/patch-package. Without this the fix stage
# cannot LOAD the suite at all, which reads downstream as "these tests do not
# exist" rather than as a broken image.
#
# --offline is what keeps this honest as a GRADED step. prepare.sh has already
# pulled both dependency sets -- base and fix -- into the content-addressed
# pnpm store at build time, so this is a pure local relink: it cannot reach the
# network, and it cannot resolve a different version than the build did. All
# three stages therefore share one dependency resolution, which is the property
# the comparison between them depends on.
#
# The two fallbacks exist only for a lockfile that genuinely drifted from its
# manifests; on this shard neither has been observed to fire.
pnpm install --frozen-lockfile --offline > /tmp/msb_install.log 2>&1 \
    || pnpm install --frozen-lockfile > /tmp/msb_install.log 2>&1 \
    || pnpm install --no-frozen-lockfile > /tmp/msb_install.log 2>&1
tail -n 5 /tmp/msb_install.log

# Per-PR fixture setup; see _EXTRA_SETUP. A no-op for nine of the ten PRs.
[[EXTRA]]

bash /home/compile.sh

TEST_PATHS=( [[TEST_PATHS]] )

# Which package owns a test file cannot be decided from the path alone: the
# packages sit at two different depths (`pkg-manager/core/test/...` but also
# `pnpm/test/...`). So walk up to the nearest directory carrying a
# package.json, stopping short of the repo root.
#
# package.json ALONE, not package.json plus jest.config.js. PR 6687's FIX patch
# is what creates `patching/apply-patch/jest.config.js`; at its base commit the
# package has no jest config at all. Requiring one here would leave the test
# stage with no owner for that PR's only test file, report zero tests, and turn
# a failed-to-passed transition into a none-to-passed one. The missing config is
# handled below instead.
declare -A MSB_GROUPS
ORDER=()

for p in "${TEST_PATHS[@]}"; do
    # A path that does not exist yet is dropped rather than passed to jest. In
    # the baseline stage a brand-new test file is absent, and --runTestsByPath
    # treats a missing argument as a hard error that produces no results at all
    # -- which would read downstream as "this package has no tests".
    [ -f "$ROOT/$p" ] || continue

    d=$(dirname "$p")
    pkg=""
    while [ "$d" != "." ] && [ "$d" != "/" ]; do
        if [ -f "$ROOT/$d/package.json" ]; then
            pkg="$d"
            break
        fi
        d=$(dirname "$d")
    done
    if [ -z "$pkg" ]; then
        echo "run_tests: no workspace package owns $p" >&2
        continue
    fi

    if [ -z "${MSB_GROUPS[$pkg]+set}" ]; then
        ORDER+=("$pkg")
        MSB_GROUPS[$pkg]=""
    fi
    MSB_GROUPS[$pkg]="${MSB_GROUPS[$pkg]} ${p#$pkg/}"
done

if [ ${#ORDER[@]} -eq 0 ]; then
    echo "run_tests: none of this PR's test files exist at this commit" >&2
    exit 0
fi

for pkg in "${ORDER[@]}"; do
    cd "$ROOT/$pkg" || continue

    # PNPM_SCRIPT_SRC_DIR is set by `pnpm run`, never by `pnpm exec`, and the
    # root jest.config.js at every commit below 7086 dereferences it without a
    # guard:
    #     const pathAsArr = process.env.PNPM_SCRIPT_SRC_DIR.split(path.sep)
    # Without it jest dies while LOADING its own config --
    #     TypeError: Cannot read properties of undefined (reading 'split')
    # -- which produces an empty results file and reads downstream as "this
    # package has no tests". The value is only used to name a jest cache
    # directory, so the package's own directory is exactly what `pnpm run
    # _test` would have put here.
    export PNPM_SCRIPT_SRC_DIR="$ROOT/$pkg"

    # A package with no jest.config.js of its own borrows the root one, with
    # rootDir forced back to the package. Without --rootDir jest would root
    # itself where the config file lives (the repo root) and collect the whole
    # monorepo; without --config it would find no config at all, fall back to
    # its defaults, lose the ts-jest preset and report
    #     Jest encountered an unexpected token
    # for every TypeScript file. This is the PR 6687 case described above.
    CFG_ARGS=()
    if [ ! -f jest.config.js ]; then
        CFG_ARGS=(--config "$ROOT/jest.config.js" --rootDir "$ROOT/$pkg")
    fi

    # The port a package's suites expect lives in its own _test script:
    #     cross-env PNPM_REGISTRY_MOCK_PORT=7771 ...
    # Reading it back beats calling _test directly, because behind cross-env
    # and run-p there is no way to hand jest --json. A package whose _test
    # names no port UNSETS the variable, so a port from the previous iteration
    # cannot leak into a package that does not want one.
    port=$(node -e "var s=((require('./package.json').scripts)||{})._test||'';var m=s.match(/PNPM_REGISTRY_MOCK_PORT=([0-9]+)/);process.stdout.write(m?m[1]:'')" 2>/dev/null)
    if [ -n "$port" ]; then
        export PNPM_REGISTRY_MOCK_PORT="$port"
    else
        unset PNPM_REGISTRY_MOCK_PORT
    fi

    # WHO starts the registry mock changes inside this range, so it is decided
    # from the checked-out tree rather than from a table.
    #
    #   6847, 7086      root jest-with-registry.config.js exists and the
    #                   package's jest.config.js re-exports it. Its globalSetup
    #                   calls registry-mock's prepare() and start(), so
    #                   starting one here as well would collide on the port.
    #   6098 .. 6687    no such file. The mock is started OUTSIDE jest, by
    #                   `_test -> test:e2e -> registry-mock prepare && run-p -r
    #                   registry-mock test:jest`. Driving jest directly skips
    #                   that, so it has to be started here or every e2e test
    #                   fails against a dead registry.
    needs_mock=0
    if [ -n "$port" ]; then
        needs_mock=1
        if [ -f jest.config.js ] && grep -q "jest-with-registry" jest.config.js; then
            needs_mock=0
        fi
    fi

    slug=$(printf '%s' "$pkg" | tr '/' '_')
    out="/tmp/msb_jest_${slug}.json"
    log="/tmp/msb_jest_${slug}.log"
    rm -f "$out" "$log"

    mock_pid=""
    if [ "$needs_mock" = "1" ]; then
        pnpm exec registry-mock prepare > /dev/null 2>&1
        pnpm exec registry-mock > "/tmp/msb_mock_${slug}.log" 2>&1 &
        mock_pid=$!
        # Polled, not slept. Measured startup is a couple of seconds, but a
        # sleep short enough to be cheap is also short enough to silently
        # truncate a slow start, and the failures that produces look exactly
        # like real test failures.
        for i in $(seq 1 60); do
            if node -e "require('net').connect($port,'127.0.0.1').on('connect',function(){process.exit(0)}).on('error',function(){process.exit(1)})" > /dev/null 2>&1; then
                echo "run_tests: registry mock up on $port for $pkg"
                break
            fi
            sleep 1
        done
    fi

    # ts-jest diagnostics OFF, and this is the difference between measuring
    # individual tests and measuring nothing. ts-jest type-checks before it
    # runs, and a test patch necessarily calls API the fix patch has not added
    # yet, so the whole FILE dies at compile:
    #     error TS2339: Property 'x' does not exist on type 'Y'
    # One unresolved symbol takes down every test in that file, the stage
    # reports a single suite-load failure instead of N failing tests, and the
    # transitions come out as none-to-passed instead of failed-to-passed.
    # Transpiling without the type check lets those tests run and fail on their
    # own assertions, which is what a failed-to-passed transition should look
    # like. Genuinely broken suites still fail either way: an unresolvable
    # import is a runtime error, not a type error.
    #
    # --coverage=false: the root config turns coverage on, and it is pure cost
    # here. --runInBand matches the config's own maxWorkers: 1, which exists
    # because these suites change dist-tags in a shared mock registry and
    # break each other if run concurrently.
    pnpm exec jest --ci --runInBand --coverage=false \
        "${CFG_ARGS[@]}" \
        --globals '{"ts-jest":{"diagnostics":false}}' \
        --json --outputFile="$out" \
        --runTestsByPath ${MSB_GROUPS[$pkg]} > "$log" 2>&1

    if [ -n "$mock_pid" ]; then
        kill "$mock_pid" > /dev/null 2>&1
        # registry-mock spawns verdaccio as a child, which outlives the kill
        # above and would hold the port against the next package in this loop.
        pkill -f verdaccio > /dev/null 2>&1
        pkill -f registry-mock > /dev/null 2>&1
    fi

    # Why a test failed still has to reach the log; the JSON below only says
    # which.
    tail -n 200 "$log"

    echo "[[BEGIN]] $pkg ====="
    if [ -s "$out" ]; then
        cat "$out"
    else
        echo '{}'
    fi
    echo ""
    echo "[[END]] $pkg ====="

    cd "$ROOT"
done

exit 0
"""


_PREPARE_SH = r"""#!/bin/bash
set -e

cd [[ROOT]]
git reset --hard
bash /home/check_git_changes.sh

# The base image keeps full history and no pin (rule 9), so the sha is already
# here -- including 6611's, which is on the v7.33.x line rather than main but
# is reachable from tags v7.33.0..v7.33.7 that a plain clone brings down. The
# remote re-attach and fetch are a cheap safety net, nothing more.
git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git rev-parse --verify --quiet "[[SHA]]^{commit}" > /dev/null 2>&1 \
    || git fetch --depth=1 origin [[SHA]] 2>/dev/null \
    || git fetch origin 2>/dev/null || true
git checkout -f [[SHA]]
bash /home/check_git_changes.sh

export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export npm_config_color=false
export npm_config_progress=false
export NODE_OPTIONS=--dns-result-order=ipv4first

# Use the pnpm the commit itself pins, read from its own package.json rather
# than from a table in this file.
#
# The range spans a major: 6098/6138/6207/6611 pin pnpm 7.24.2, 6496/6521 pin
# 8.0.0-beta.0, and the rest pin 8.6.x. `.npmrc` sets engine-strict=true and
# every root manifest carries an engines.pnpm floor, so the version is not
# cosmetic -- and pnpm 8 changed enough defaults that resolving a pnpm-7-era
# lockfile with it is a re-resolution, not a read. All six pinned versions are
# on the registry; verified before choosing this over a single global pnpm.
PM=$(node -e "process.stdout.write((require('./package.json').packageManager)||'')")
PM_VER=${PM#pnpm@}
if [ -n "$PM_VER" ] && [ "$PM_VER" != "$PM" ]; then
    echo "prepare: package.json pins pnpm@$PM_VER"
    npm install -g "pnpm@$PM_VER" --no-audit --no-fund || true
fi
# One fallback, and only if the pinned binary cannot even report its version.
pnpm --version > /dev/null 2>&1 || npm install -g "pnpm@[[BOOTSTRAP]]" --no-audit --no-fund
echo "prepare: pnpm $(pnpm --version), node $(node --version)"

# --frozen-lockfile first: it installs exactly the tree the commit locked,
# which is what CI did (CI=true makes pnpm default to frozen) and what keeps
# engine-strict happy, since the engines checked are the locked packages'.
# The fallback exists for a lockfile that genuinely drifted from its manifests;
# it resolves afresh instead of failing the whole image.
pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile

# The install rewrites pnpm-lock.yaml in the working tree, and the fix patches
# of 6098, 6207, 6496, 6687 and 6847 all carry hunks against that same file.
# Without this restore every fix stage would die before running a single test:
#     error: pnpm-lock.yaml: patch does not apply
# node_modules and lib are untracked, so they survive the restore.
git checkout -- .
bash /home/check_git_changes.sh

# Pre-resolve the FIX patch's dependencies into the pnpm store, at BUILD time.
#
# Why this has to exist at all: a fix patch can change what the tree depends
# on, and the image's node_modules only knows the BASE manifests. Three PRs in
# this shard do exactly that -- 6207 adds @pnpm/graceful-fs, 6496 adds
# @pnpm/which, 6687 moves from patch-package to @pnpm/patch-package -- and
# without those packages the fix stage cannot even LOAD the suite:
#     Test suite failed to run: Cannot find module '@pnpm/which'
#
# Doing it HERE rather than in the graded run scripts is the point. The pnpm
# store is content-addressed and survives everything below, so once these
# packages are in it the fix stage's relink is a pure local operation: no
# network during grading, and no second resolution that could pick different
# versions than the first.
#
# The revert is deliberately NOT just `git checkout -- .`. A fix patch can
# CREATE files -- 6687 creates patching/apply-patch/jest.config.js and a
# fixture -- and `git checkout -- .` does not delete untracked files, so part
# of the fix would be left behind in the baseline image. The created paths are
# read back out of the patch and removed explicitly.
if git apply --check --whitespace=nowarn /home/fix.patch > /dev/null 2>&1; then
    git apply --whitespace=nowarn /home/fix.patch
    pnpm install --frozen-lockfile > /tmp/msb_warm.log 2>&1 \
        || pnpm install --no-frozen-lockfile > /tmp/msb_warm.log 2>&1 \
        || tail -n 5 /tmp/msb_warm.log

    # Restore tracked files, then delete exactly what the patch added.
    git checkout -- .
    # Each path is emitted WITH a trailing newline, and the loop reads a final
    # unterminated line anyway. Both halves matter: `out.join("\n")` leaves the
    # last path without a newline, and a bare `while read` then silently DROPS
    # it. That cost a build -- pr-6496's last created path is
    # exec/plugin-commands-script-runners/src/buildCommandNotFoundHint.ts, it
    # survived into the baseline image, and tsc died on the didyoumean2 import
    # that only the fix patch installs.
    node -e "var fs=require('fs');var out=[];fs.readFileSync('/home/fix.patch','utf8').split('diff --git ').slice(1).forEach(function(b){if(b.indexOf('new file mode')===-1)return;var m=b.match(/^\+\+\+ b\/(.+)$/m);if(m)out.push(m[1].trim())});out.forEach(function(x){process.stdout.write(x+String.fromCharCode(10))})" > /tmp/msb_fix_new.txt
    echo "prepare: fix patch creates $(grep -c . /tmp/msb_fix_new.txt) file(s); removing them"
    while IFS= read -r f || [ -n "$f" ]; do
        if [ -n "$f" ]; then
            rm -f "[[ROOT]]/$f"
        fi
    done < /tmp/msb_fix_new.txt

    # Assert the revert actually happened. A leftover file from the fix patch is
    # exactly the failure above, and it is silent until something downstream
    # cannot compile.
    while IFS= read -r f || [ -n "$f" ]; do
        if [ -n "$f" ] && [ -e "[[ROOT]]/$f" ]; then
            echo "prepare: fix-patch file survived the revert: $f" >&2
            exit 1
        fi
    done < /tmp/msb_fix_new.txt

    # Put node_modules back on the BASE lockfile so the baseline and test
    # stages see exactly the tree they saw before this block ran. --offline
    # because everything needed is already in the store.
    pnpm install --frozen-lockfile --offline > /tmp/msb_rebase.log 2>&1 \
        || pnpm install --frozen-lockfile > /tmp/msb_rebase.log 2>&1
    git checkout -- .
    bash /home/check_git_changes.sh
    echo "prepare: fix-patch dependencies pre-resolved into the store"
else
    echo "prepare: fix patch does not apply at build time; store not pre-warmed" >&2
fi

# Build the CLI bundle and every package the patches touch, once, here. The
# graded stages then rebuild only what their patch changed.
bash /home/compile.sh

# The fixtures are real installed projects, not inert files: several graded
# suites copy them through @pnpm/test-fixtures and expect their node_modules to
# be present. This is the repo's own root `pretest` step, run with the CLI
# bundle compile.sh just produced.
pnpm --dir=__fixtures__ run prepareFixtures

# Refuse to seal an image whose graded stages could not report anything. A
# missing jest, an unresolvable ts-jest transform or a moved package directory
# all produce an empty log, which reads downstream as "these tests do not
# exist" rather than as a broken image -- and the harness scores that as a
# valid resolve.
# The check is on the owning PACKAGE, not on the test FILE, and the walk does
# not require the file to exist. PR 6687's only graded test file is created by
# its test patch, so at the base commit there is nothing to stat -- but
# `patching/apply-patch` itself is there, and that is what has to be sound.
CHECKED=0
for p in [[TEST_PATHS]]; do
    d=$(dirname "$p")
    pkg=""
    while [ "$d" != "." ] && [ "$d" != "/" ]; do
        if [ -f "[[ROOT]]/$d/package.json" ]; then
            pkg="$d"
            break
        fi
        d=$(dirname "$d")
    done
    [ -n "$pkg" ] || continue
    (
        cd "[[ROOT]]/$pkg"
        # Same reason as in run_tests.sh: the root jest.config.js reads
        # PNPM_SCRIPT_SRC_DIR unguarded below commit 7086.
        export PNPM_SCRIPT_SRC_DIR="[[ROOT]]/$pkg"
        pnpm exec jest --version > /dev/null
        # Where a package declares itself as its own dependency, that self-link
        # has to resolve: it is how the test files import the code under test
        # (`import { ... } from '@pnpm/core'`), and a run without it reports
        # zero tests rather than an error. Guarded, because the `pnpm` package
        # itself declares no self-dependency -- its suites reach the CLI by
        # spawning bin/pnpm.cjs instead.
        node -e "var p=require('./package.json');var d=Object.assign({},p.dependencies,p.devDependencies);if(d[p.name])require.resolve(p.name)"
    )
    CHECKED=$((CHECKED + 1))
done
if [ "$CHECKED" -eq 0 ]; then
    echo "prepare: no workspace package owns any graded test path" >&2
    exit 1
fi
echo "prepare: $CHECKED graded package path(s) checked"

cd [[ROOT]]
git checkout -- .
bash /home/check_git_changes.sh
"""


_RUN_SH = """#!/bin/bash
set -e

bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -e

cd [[ROOT]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -e

cd [[ROOT]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh
"""


class Pnpm7086To6098ImageBase(Image):
    """Clone-only base, shared by all ten PRs.

    Rule 9: it stops at the clone plus CMD. No checkout, no pin, no gc, no
    scrub, no asserts. Nothing in it is commit-specific, so its content is
    byte-identical for every PR and one tag serves all ten -- and because the
    history is left whole, 6611's off-main base commit is present without any
    fetch-back workaround.
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

    def dependency(self) -> Union[str, "Image"]:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # The syntax directive is what keeps this file verbatim. It makes
        # DockerfileEnhancer treat the content as already-enhanced and return
        # early, so _standardize_repo_fetch does NOT rewrite the clone into a
        # pinned checkout plus hardening block. That is exactly what rule 9
        # asks for, and it is also why the ARG/ENV/LABEL/CA blocks below are
        # written out by hand instead of being injected.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

# NODE_OPTIONS below carries --dns-result-order=ipv4first, and it is
# load-bearing. Verdaccio -- the mock npm registry every e2e suite here
# installs from -- binds the address `localhost`, and from Node 17 on that
# resolves to ::1 BEFORE 127.0.0.1 because Node stopped reordering DNS results.
# pnpm's own fetch dials 127.0.0.1, so the registry listens where nothing
# connects:
#     GET http://localhost:4873/@pnpm.e2e%2Fpkg-with-optional:
#     connect ECONNREFUSED 127.0.0.1:4873
# Measured on pr-6847 before this existed: 16 of 17 tests failed that way, each
# after ~190s of pnpm fetch-retry backoff, so one test file took 88 minutes.
# Proven in the container: with ipv4first, verdaccio binds 127.0.0.1 and
# `curl http://127.0.0.1:4873/@pnpm.e2e%2Fpkg-with-optional` returns 200.
# It lives on the IMAGE, not in a script, so it also covers the verdaccio that
# jest starts from its own globalSetup, which no script of ours wraps.
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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_OPTIONS=--dns-result-order=ipv4first \\
    CI=true \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    NPM_CONFIG_COLOR=false \\
    NPM_CONFIG_PROGRESS=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

# The bullseye-security pool rotates its package versions out from under apt:
# a plain `apt-get install` can 404 on the very version apt just picked as the
# candidate, and retrying does not help. Dropping the security line keeps the
# main pool, which is stable.
RUN sed -i '/bullseye-security/d; /security.debian.org/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    python3 \\
    make \\
    g++ \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

# A bootstrap pnpm only. prepare.sh replaces it with the exact version each
# commit's `packageManager` field pins.
RUN npm install -g pnpm@{_BOOTSTRAP_PNPM} --no-audit --no-fund

# Several suites shell out to a real `git commit`, which refuses to run without
# an identity.
RUN git config --global user.name "msb" && \\
    git config --global user.email "msb@example.com" && \\
    git config --global init.defaultBranch main

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class Pnpm7086To6098ImageDefault(Image):
    """PR layer: the COPY lines, prepare.sh, then the strip/hardening.

    dependency() returns an Image, so DockerfileEnhancer emits this file
    verbatim and injects no ARG/ENV/WORKDIR/CMD. That is also why the sha is
    written into the hardening block literally rather than read from
    ${BASE_COMMIT}: build_dataset.py passes REPO_URL and BASE_COMMIT only to
    images whose dependency() is a str, so an Image-dependency layer receives
    no build args at all.
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

    def dependency(self) -> Optional[Image]:
        return Pnpm7086To6098ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

        test_paths = _sh_list(_test_files(self.pr))
        touched = _sh_list(_touched_files(self.pr))

        extra = _EXTRA_SETUP.get(
            int(self.pr.number), "true  # no extra fixture setup needed for this PR"
        )

        def _fill(text: str) -> str:
            return (
                text.replace("[[EXTRA]]", extra)
                .replace("[[TEST_PATHS]]", test_paths)
                .replace("[[TOUCHED]]", touched)
                .replace("[[BOOTSTRAP]]", _BOOTSTRAP_PNPM)
                .replace("[[BEGIN]]", _JSON_BEGIN)
                .replace("[[END]]", _JSON_END)
                .replace("[[ROOT]]", _REPO_ROOT)
                .replace("[[ORG]]", org)
                .replace("[[REPO]]", repo)
                .replace("[[SHA]]", sha)
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _fill(_CHECK_GIT_CHANGES_SH)),
            File(".", "compile.sh", _fill(_COMPILE_SH)),
            File(".", "run_tests.sh", _fill(_RUN_TESTS_SH)),
            File(".", "prepare.sh", _fill(_PREPARE_SH)),
            File(".", "run.sh", _fill(_RUN_SH)),
            File(".", "test-run.sh", _fill(_TEST_RUN_SH)),
            File(".", "fix-run.sh", _fill(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Rule 9: the strip lives here, never in the base and never in
        # prepare.sh. It runs AFTER prepare.sh because prepare.sh needs both
        # the network and the git remote -- it pins the commit, installs the
        # whole workspace from the registry, and runs the fixture installs.
        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
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
    fi
"""


@Instance.register("pnpm", ALIAS_KEY)
class Pnpm7086To6098(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Pnpm7086To6098ImageDefault(self.pr, self._config)

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
        """Read jest's own --json, one fenced blob per graded package.

        Ids are `jest::<repo-relative path>::<full test name>`. The tool name
        comes FIRST on purpose: an id that starts with a path the fix patch
        creates makes report.py flag the whole instance as fix-authored.

        Every blob is read, not only the last one -- that is the difference
        between scoring all the packages a PR touches and scoring one of them.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        prefix = _REPO_ROOT + "/"

        blob_re = re.compile(
            re.escape(_JSON_BEGIN)
            + r"\s+(?P<pkg>\S+)\s+=====\s*(?P<body>.*?)"
            + re.escape(_JSON_END),
            re.S,
        )

        for match in blob_re.finditer(text):
            body = match.group("body")
            start = body.find("{")
            if start == -1:
                continue

            try:
                data = json.loads(body[start:])
            except Exception:
                # A truncated or interleaved blob must not be read as "no
                # tests" while it can still be recovered: retry on the
                # outermost braces.
                end = body.rfind("}")
                if end <= start:
                    continue
                try:
                    data = json.loads(body[start : end + 1])
                except Exception:
                    continue

            for suite in data.get("testResults") or []:
                path = suite.get("name") or ""
                if prefix in path:
                    path = path.split(prefix, 1)[1]
                path = path.replace("\\", "/")

                assertions = suite.get("assertionResults") or []

                if not assertions:
                    # A suite that fails to load reports zero assertions and a
                    # message. Recording it keeps a broken import visible
                    # rather than letting the file vanish from the counts.
                    if suite.get("status") == "failed" or suite.get("message"):
                        failed_tests.add(f"jest::{path}::<suite failed to load>")
                    continue

                for a in assertions:
                    name = a.get("fullName") or a.get("title") or ""
                    if not name:
                        continue
                    test_id = f"jest::{path}::{name}" if path else f"jest::{name}"
                    status = (a.get("status") or "").lower()

                    if status == "passed":
                        passed_tests.add(test_id)
                    elif status == "failed":
                        failed_tests.add(test_id)
                    else:
                        # pending / skipped / todo / disabled all mean "did not
                        # run to a pass" without being a failure.
                        skipped_tests.add(test_id)

        # jest.setup.js calls jest.retryTimes(1), so one test can be reported
        # twice. TestResult rejects overlapping sets, and double-counting would
        # invent transitions: a pass beats an earlier failed attempt.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
