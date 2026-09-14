from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NUMBER_INTERVAL = "jest_8751_to_7119"

BASE_TAG = "base-8751-to-7119"

JEST_START = "-----MSB_JEST_JSON_START-----"
JEST_END = "-----MSB_JEST_JSON_END-----"

JEST_JSON = "/home/jest-report.json"


# Every base commit in the bundle. The SHARED base fetches exactly these, at
# depth 1 -- see the note in the base Dockerfile for why. Each entry is
# annotated with the PR it belongs to so the list stays auditable by eye.
BUNDLE_BASE_SHAS = [
    "ad5b040fbfbf23bc303610a5254ade9a3da65015",  # pr-7119
    "251c2953d788031e36ad64c088d6947dec19ee84",  # pr-7213
    "319329f7942ed33f0be706a19a2b23d08eac0ea0",  # pr-7251
    "df782554a274db1345d064945fb35368e6f8b811",  # pr-7497
    "e67c8183d220b73ccbb57f5d10ac0da176053526",  # pr-7611
    "9ba62ada66f731880229073eb193e8cb60863ae3",  # pr-7652
    "7d54557ee650035c65edd5f8435460b72e63b5c1",  # pr-7746
    "d0b2c24537b70b5323fc53cb595adbfb36b1a910",  # pr-7752
    "3edccf64a6f46350d0e2688af79ae52a080ad9f4",  # pr-7770
    "d7f3427f5caa366634575be4a7a885e7769d42a7",  # pr-7776
    "a61ac1dfd997218d832ff0301e8c397f0dd7ab46",  # pr-7787
    "e66a0e888021e1528a196b0550f05199dd35c7d1",  # pr-7792
    "392a815acb978820967f6539ded446b7be4b8762",  # pr-8021
    "b0cbcbf3f813db9a66a5bfbe822e15d59d157064",  # pr-8079
    "8054b94924664b3ca0f122283cffe346270c395f",  # pr-8382
    "8294bab1ebbf9bac7d2aa01abd256bbca4952eb7",  # pr-8612
    "2b64bb45fc952d02ecdd09618c817fd0d67f072e",  # pr-8629
    "48329474828efd53d11f4e9de6cd13ce545adee6",  # pr-8686
    "bd76829f66c5c0f3c6907b80010f19893cb0fc8c",  # pr-8687
    "c8418dfab714ddd75dcaaca28f9811c7da3e6123",  # pr-8751
]


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

git update-index -q --really-refresh || true

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""


_APPLY_PATCH_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

for p in "$@"; do
    if git apply --whitespace=nowarn "$p"; then
        echo "apply_patch: applied $p"
    elif git apply --whitespace=nowarn --exclude=CHANGELOG.md "$p"; then
        # EVERY PR in this bundle edits CHANGELOG.md, and several edit the same
        # region, so the fix patch can collide with the test patch there. The
        # changelog is prose: no suite reads it, and excluding it changes no test
        # outcome. --3way cannot rescue it either -- the base is fetched at depth
        # 1, so the blob the patch indexes is not in the object store:
        #     error: repository lacks the necessary blob to fall back on 3-way
        # MEASURED on pr-7787, whose fix act produced nothing for this reason.
        echo "apply_patch: applied $p WITHOUT CHANGELOG.md (conflicting prose)"
    else
        # --3way needs the patch's PRE-IMAGE blob, and the base is fetched at
        # depth 1, so ancestry -- and the blobs in it -- is absent:
        #     error: repository lacks the necessary blob to fall back on 3-way
        # Deepen this commit's history on demand and retry. MEASURED on pr-7251,
        # whose fix patch was generated against an OLDER parent:
        #     blob at base_commit      : 35e4b74e7c23
        #     patch pre-image expects  : 1428f1295291
        #     patch post-image produces: 35e4b74e7c23   <- already at base
        # i.e. part of the fix is already in the base tree, and only a 3-way
        # merge can recognise that. FLOW Issue 3: recoverable, not a skip.
        echo "apply_patch: deepening history so --3way has its blobs"
        git fetch --depth 200 --no-tags -q origin "$(git rev-parse HEAD)" || true

        # A patch can fail because part of it is ALREADY IN THE BASE TREE: the
        # patch was generated against an older parent, so its post-image is what
        # the base already holds. MEASURED on pr-7251, packages/babel-jest/src/
        # index.js:
        #     blob at base_commit      : 35e4b74e7c23
        #     patch pre-image expects  : 1428f1295291
        #     patch post-image produces: 35e4b74e7c23   <- already present
        # --3way is the normal way to recognise that, but it needs the pre-image
        # blob, which a depth-1 base does not carry. So detect the condition
        # directly -- compare each target's CURRENT blob against the patch's
        # post-image -- and exclude only the files that are already at their
        # final state. Nothing else in the patch is skipped.
        SKIP=""
        while read -r f post; do
            [ -n "$f" ] || continue
            cur=$(git rev-parse "HEAD:$f" 2>/dev/null | cut -c1-${{#post}})
            if [ "$cur" = "$post" ]; then
                echo "apply_patch: $f is already at the patch's post-image, skipping"
                SKIP="$SKIP --exclude=$f"
            fi
        done <<EOF
$(awk '/^diff --git a\// {{f=$3; sub(/^a\//,"",f)}} /^index / {{i=index($2,".."); if (f != "" && i > 0) print f, substr($2, i+2); f=""}}' "$p")
EOF

        if [ -n "$SKIP" ] && git apply --whitespace=nowarn $SKIP "$p"; then
            echo "apply_patch: applied $p minus the already-applied files"
        else
            echo "apply_patch: retrying $p with --3way"
            git apply --3way --whitespace=nowarn "$p"
            echo "apply_patch: applied $p with --3way"
        fi
    fi
    git add -A
done

if git grep -qI -e '^<<<<<<< ' -- . 2>/dev/null; then
    echo "apply_patch: conflict markers survived the merge"
    git grep -nI -e '^<<<<<<< ' -- . | head
    exit 1
fi

# A patch may ADD A DEPENDENCY, and node_modules was installed from the
# pre-patch manifests. MEASURED on pr-8382: its fix patch adds
# "@babel/plugin-syntax-bigint" to packages/babel-preset-jest/package.json and
# to yarn.lock; without re-installing, babel cannot load the plugin and EVERY
# targeted suite dies with "Cannot find module" -- the fix act reported 0/0/0
# and the PR looked unresolvable when the environment was simply stale.
#
# Re-install only when a manifest actually moved, so PRs that touch no
# dependency pay nothing.
if git diff --name-only HEAD -- '*package.json' 'yarn.lock' '*/package.json' \
        | grep -q .; then
    echo "apply_patch: dependency manifests changed, re-installing"
    git diff --name-only HEAD -- '*package.json' 'yarn.lock' '*/package.json'
    bash /home/yarn_install.sh
else
    echo "apply_patch: no dependency manifest touched, install left as built"
fi

# REBUILD after patching. prepare.sh compiles packages/*/src -> packages/*/build
# BEFORE any patch exists, and jest's e2e suites spawn the BUILT cli, so without
# this they exercise UNPATCHED code -- while the unit suites, which jest maps to
# src, do see the patch. The result is a fix act that silently fixes nothing.
#
# MEASURED on pr-7770, whose patch adds `maxConcurrency`. Fix act, stored
# snapshot vs received:
#     -     "maxConcurrency": 5,   <- the test patch expects it
#     +     (absent)               <- the built cli predates the fix patch
# The PR read as unresolvable; the build was simply stale. Every e2e-heavy PR in
# this bundle failed this way, which is why the unit-test PRs passed and the
# e2e ones did not.
#
# `yarn build` measures 8 SECONDS here, so it is paid unconditionally rather
# than guessed at from which paths a patch happens to touch.
echo "apply_patch: rebuilding packages so the e2e cli reflects the patch"
if node -e "process.exit(require('./package.json').scripts['build:js'] ? 0 : 1)" \
        2>/dev/null; then
    yarn build:js
else
    yarn build
fi
"""


_YARN_INSTALL_SH = """#!/bin/bash
# One installer, used by BOTH prepare.sh and apply_patch.sh, so the two can
# never drift into installing differently.
#
# This bundle STRADDLES the yarn 1 -> yarn 2 (Berry) migration and the two take
# incompatible flags: Berry rejects `--frozen-lockfile` and `--network-timeout`
# outright ("Unknown Syntax Error"), which is an INSTANT non-zero exit rather
# than a slow failure. MEASURED on pr-7792.
#
# The split does not follow PR number -- several of these PRs stayed open for
# over a year (pr-8751: opened 2019-07, merged 2020-11), so their base commits
# land on very different trunks. Nor can it be read from `.yarnrc` carrying a
# yarn-path: pr-7776 pins .yarn/releases/yarn-1.22.4.js and is still CLASSIC.
# Ask the toolchain that will actually run, in the tree as checked out.
set -eo pipefail
cd /home/{repo}

YARN_MAJOR="$(yarn --version 2>/dev/null | cut -d. -f1)"
echo "yarn_install: yarn $(yarn --version 2>/dev/null)"
if [ "$YARN_MAJOR" = "1" ]; then
    # --ignore-scripts is REQUIRED on the classic path, and it is not a
    # time-saving shortcut -- without it the install cannot complete at all.
    #
    # This era's package.json declares:
    #     root     postinstall: "opencollective postinstall && yarn build"
    #     website  postinstall: "node fetchSupporters.js"
    # Both call third-party services that no longer answer as they did in 2018.
    # MEASURED on pr-7119 with a full install:
    #     SyntaxError: Unexpected token N in JSON at position 0
    #         at Request.request (/home/jest/website/fetchSuporters.js:14:26)
    # i.e. the sponsor API now returns a non-JSON error body, JSON.parse throws,
    # and the whole install exits 1. That is FLOW Issue 5 -- a public host
    # breaking the run -- not a property of the PR.
    #
    # What this costs is bounded and was checked, not assumed: ZERO packages/*
    # declare an install hook, so nothing the suites exercise is skipped. The
    # only useful half of the root hook is `yarn build`, which prepare.sh runs
    # explicitly straight afterwards. If a third-party native addon ever did
    # need building, the hard gates below fail the BUILD rather than letting a
    # half-built environment reach the acts.
    #
    # Berry needs none of this: it does not run the workspace-root postinstall,
    # which is exactly why the Berry PRs installed cleanly while every classic
    # one failed.
    # The registry is a third party and it flakes. MEASURED in one run:
    #     pr-8021: browserslist-4.4.2.tgz -> 502 Bad Gateway
    #     pr-7497: setprototypeof-1.0.3.tgz -> 503 Service Unavailable
    # Both had been RESOLVED in the previous run, so a single transient 5xx was
    # enough to turn real evidence into an empty act. Retry before believing it.
    for i in 1 2 3 4; do
        if yarn install --frozen-lockfile --network-timeout 600000 \\
                --ignore-scripts; then break; fi
        echo "yarn_install: attempt $i failed (registry flake?), retrying"
        sleep $((i * 15))
        [ "$i" = "4" ] && exit 1
    done

    # --ignore-scripts skipped the repo's broken postinstalls, but it also
    # skipped node-gyp builds for NATIVE addons, and jest genuinely needs one:
    # packages/jest-leak-detector requires weak-napi. MEASURED on pr-8686, fix
    # act, all three acts alike:
    #     Test suite failed to run
    #     Could not locate the bindings file. Tried:
    #       -> node_modules/weak-napi/build/Release/weakref.node
    # So compile the addons back explicitly. This is the narrow repair for the
    # one thing --ignore-scripts costs, rather than re-enabling the hooks that
    # call dead 2018 endpoints.
    echo "yarn_install: rebuilding native addons skipped by --ignore-scripts"
    npm rebuild --no-audit --no-fund 2>&1 | tail -5 || true
else
    for i in 1 2 3 4; do
        if yarn install --immutable; then break; fi
        echo "yarn_install: attempt $i failed (registry flake?), retrying"
        sleep $((i * 15))
        [ "$i" = "4" ] && exit 1
    done
fi
"""


_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export YARN_CACHE_FOLDER=/root/.cache/yarn

# Docker build output is not streamed anywhere the harness keeps, so a slow
# image is a black box: 45 minutes in, "install" and "build" and "wedged on a
# network retry" all look identical. Stamp each step so the build log itself
# says where the time went.
_t0=$(date +%s)
step() {{ echo "prepare[+$(( $(date +%s) - _t0 ))s] $*"; }}

cd /home/{repo}

git reset --hard
# node_modules is NOT tracked, and re-installing it on every act would add
# minutes per act for no benefit. Keep it; remove every other untracked file so
# a stale build artefact cannot masquerade as source.
git clean -fdq -e node_modules -e '**/node_modules'

step "checking work tree"
bash /home/check_git_changes.sh

# The image must sit on the dataset's base_commit and nothing else. The PR layer
# checked it out; assert it here so a silent drift fails the BUILD rather than
# surfacing as unexplainable test results three acts later.
test "$(git rev-parse HEAD)" = "{sha}"
echo "prepare: HEAD is {sha}"

# The installer itself lives in yarn_install.sh (shared with apply_patch.sh).
step "yarn install (73 workspaces, full postinstall)"
bash /home/yarn_install.sh

# jest's own suites import from packages/*/build, which only exists after the
# monorepo is compiled. `build` is present in package.json at every commit in
# this bundle (verified across all 20); on later commits it delegates to
# build:js + build:ts.
# Prefer build:js. `yarn build` on the later commits is `build:js && build:ts`,
# and build:ts only emits .d.ts TYPE DEFINITIONS -- nothing any suite loads at
# runtime. It is also the half that breaks on third-party type drift:
# MEASURED on pr-8751,
#     node_modules/@types/jsdom/ts3.3/index.d.ts(4,10): error TS2305:
#       Module '"../../../parse5/lib"' has no exported member ...
#      Unable to build TypeScript definition files
# which failed the whole image for a reason that has nothing to do with the PR.
# Earlier commits have no build:js script, so fall back to build there.
step "yarn build"
if node -e "process.exit(require('./package.json').scripts['build:js'] ? 0 : 1)" \
        2>/dev/null; then
    yarn build:js
else
    yarn build
fi

# Hard gates -- FILESYSTEM CHECKS ONLY. Never execute the jest CLI here.
#
# `node -e "require('.../jest-cli/bin/jest.js')"` does NOT merely load the file:
# requiring a bin script RUNS it, and jest with no arguments starts its own
# ~5000-test self-suite. `node .../bin/jest.js --version` is barely better on a
# loaded machine. MEASURED on pr-7770 from its build_image.log:
#     11:41:31  yarn build done ......................... 8s
#     11:55:01  node -e require(bin/jest.js) -> Killed .. 13.5 min, OOM
#     11:58:41  node --version .......................... 3.7 min (thrashing)
#     12:24:40  bin/jest.js --version ................... 26 min
#     -> install 37s + build 8s, then 43 MINUTES of gates proving nothing.
#
# Worse, the `2>/dev/null || node --version` tail SWALLOWED the OOM kill -- the
# same "gate that hides the failure it exists to catch" pattern this config
# rejects everywhere else.
#
# What the gates must establish is that the install and build produced the tree
# the acts will import. That is a filesystem question. Whether jest RUNS is
# answered by the acts themselves, where a failure is attributable to a PR
# instead of being buried in a build log.
test -d /home/{repo}/node_modules
test -d /home/{repo}/packages/jest-cli/build
test -s /home/{repo}/packages/jest-cli/bin/jest.js
test -d /home/{repo}/packages/expect/build
step "gates passed"
echo "prepare: jest cli is runnable and packages are built"
"""


_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export YARN_CACHE_FOLDER=/root/.cache/yarn
# jest's e2e suites shell out to node and compare stdout, so pin the clock
# rather than letting the host decide.
#
# FORCE_COLOR=1 is REQUIRED, and the intuition to silence colour is backwards.
#
# jest's own suites TEST its colouring: packages/jest-matcher-utils and
# packages/expect store snapshots full of <dim>/<red>/<green> markers, which its
# convert_ansi snapshot serializer produces BY CONVERTING REAL ANSI ESCAPES. The
# harness runs acts through `docker run` WITHOUT a TTY, so chalk auto-disables
# colour, the serializer has no escapes to convert, and every colour-bearing
# snapshot mismatches -- while looking exactly like the PR broke them.
#
# MEASURED on pr-8382, fix act, same image, only this variable changed:
#     FORCE_COLOR=0 / unset -> 440 failed / 204 passed
#     FORCE_COLOR=1         ->   0 failed / 644 passed
export TZ=utc
export FORCE_COLOR=1

cd /home/{repo}

TARGETS=$(cat /home/jest_targets.txt 2>/dev/null || true)

echo "{jest_start}"
rm -f {jest_json}
if [ -n "$TARGETS" ]; then
    EXISTING=""
    for f in $TARGETS; do
        [ -f "$f" ] && EXISTING="$EXISTING $f"
    done
    if [ -n "$EXISTING" ]; then
        echo "jest targets:$EXISTING"
        rc=0
        # -i (runInBand) is load-bearing, not tuning. jest's own e2e suites
        # SPAWN further jest processes; with the default worker-per-CPU pool and
        # several acts running at once the machine is oversubscribed many times
        # over, and these suites assert on timing-sensitive child-process
        # output. --ci additionally refuses to write new snapshots, so a missing
        # snapshot fails instead of being silently created and then "passing".
        # A WALL-CLOCK CEILING, and --forceExit, are both load-bearing.
        #
        # These suites deliberately crash worker processes (jest-worker,
        # fatalWorkerError) and a worker that fails to die leaves jest waiting
        # forever. MEASURED: pr-8079's act hung 18+ minutes with the machine
        # idle at load 3, stalling the WHOLE run behind one instance.
        #
        # The harness gives a docker read timeout of 600s (docker_util.py, and
        # it is core, so not ours to raise). A ceiling BELOW that turns a hung
        # suite into a failed act with a real log, instead of an instance the
        # harness eventually abandons with no evidence at all.
        timeout --signal=KILL 480 \\
        node ./packages/jest-cli/bin/jest.js \\
            --ci -i --forceExit --json --outputFile={jest_json} \\
            $EXISTING > /tmp/jest_stdout 2>&1 || rc=$?
        [ "$rc" = "137" ] && echo "jest: KILLED by the 480s ceiling (hung suite)"
        echo "jest exit: $rc"
    else
        echo "jest: none of the target files exist at this tree state"
    fi
else
    echo "jest: this PR's test patch adds no runnable test file"
fi

# The machine report is the LAST thing between the markers and nothing else is
# printed inside them. jest's human output routinely contains braces (failure
# diffs, printed objects); with that text between the markers a JSON scan can
# start on the wrong brace and recover nothing. Keep the readable log OUTSIDE.
# Do NOT `cat` the raw report. jest writes one enormous single line (the
# failureMessages alone run to hundreds of KB), and docker's stdout is a
# non-blocking pipe with a 64KB buffer: the write fails part-way with
#     cat: write error: Resource temporarily unavailable
# leaving HALF a JSON object in the log. MEASURED on pr-8382: the act truncated
# at exactly 65537 bytes, parse_log could not parse it, and a run that had
# actually scored 892 passed / 0 failed was recorded as 0/0/0.
#
# Emit only the fields parse_log reads, PRETTY-PRINTED so every individual write
# is a short line that fits the buffer. json.loads is whitespace-insensitive, so
# the parser is unchanged.
if [ -f {jest_json} ]; then
    node -e '
        var r = require("{jest_json}");
        var out = {{testResults: (r.testResults || []).map(function (s) {{
            return {{
                name: s.name,
                assertionResults: (s.assertionResults || []).map(function (a) {{
                    return {{
                        ancestorTitles: a.ancestorTitles,
                        title: a.title,
                        status: a.status
                    }};
                }})
            }};
        }})}};
        process.stdout.write(JSON.stringify(out, null, 1));
    ' || echo "FATAL: could not re-emit the jest report"
fi
echo "{jest_end}"

echo "----- jest stdout (human, not parsed) -----"
tail -60 /tmp/jest_stdout 2>/dev/null || true
if [ ! -f {jest_json} ] && [ -n "$TARGETS" ]; then
    echo "FATAL: jest produced no JSON report -- the runner failed to start."
fi
"""


class Jest8751To7119ImageBase(Image):
    """Clone-only base, shared by all twenty PRs in the bundle.

    node:10-buster is chosen from the repo, not from habit. PR #8751 declares
    `engines: ^10.13.0 || ^12.13.0 || ^14.15.0 || >=15.0.0`, while the earliest
    commits here (#7119, Oct 2018) predate that field and test node 6/8/10 in
    CircleCI. Node 10 is the only line valid at BOTH ends of the range, so one
    base can serve the whole bundle. It is also a real multi-arch tag --
    linux/amd64 and linux/arm64 both published -- which the delivery requires.

    The base stops at the clone: no checkout, no pin, no scrub.
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
        return "node:10-buster"

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

        repo = self.pr.repo

        # The `-C <dir>` form is deliberate: core's
        # DockerfileEnhancer._inject_final_sanitize() appends its hardening
        # block to any Dockerfile whose text contains a bare git verb, and under
        # this layout that block belongs in the PR layer.
        #
        # The PR-ref fetch is NOT optional. pr-8686's base_commit
        # (48329474828efd53d11f4e9de6cd13ce545adee6) is not an ancestor of any
        # branch -- a plain clone does not contain it and `git checkout` fails
        # with "reference is not a tree". GitHub still serves it under
        # refs/pull/8686/head, and fetching every PR head makes all twenty base
        # commits resolvable from one shared base. The per-PR scrub prunes the
        # extra refs away again, so nothing leaks into a shipped image.
        return """FROM {image_name}

{global_env}

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /home/

# buster left the main mirrors when it went EOL; its packages now live on
# archive.debian.org. Without this rewrite apt-get update 404s and the image
# cannot install git.
RUN sed -i "s/deb.debian.org/archive.debian.org/g" /etc/apt/sources.list && \\
    sed -i "s/security.debian.org/archive.debian.org/g" /etc/apt/sources.list && \\
    sed -i "/buster-updates/d" /etc/apt/sources.list && \\
    apt-get -o Acquire::Check-Valid-Until=false update && \\
    apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

# A SHALLOW, BUNDLE-SCOPED history -- this is the difference between a build
# that takes minutes and one that takes hours, and it is not a shortcut.
#
# The PR layer must carry the hardening block (your 2026-09-03 rule), and that
# block ends in `git gc --prune=now --aggressive`, twice. Over jest's full
# eight-year history that repack is the ENTIRE per-image cost.
#
# MEASURED, same container, same commits:
#     full-history clone  -> gc ~90 minutes per PR image
#     20 commits @depth 1 -> gc 4 SECONDS, rev-list --all == rev-list HEAD == 1
#
# Fetching each base commit at depth 1 gives twenty parentless roots: every PR
# can still check out its own commit, but there is no ancestry to repack. It is
# also STRICTLY safer on the property the scrub exists to protect -- a tree that
# never contained a later commit cannot leak one, whereas scrub-after-full-clone
# depends on the scrub working (and today it silently did not, when the enhancer
# pinned the shared base to one PR and pruned the other nineteen away).
#
# All 20 commits resolve this way, including pr-8686's
# (48329474828efd53d11f4e9de6cd13ce545adee6), which is an ancestor of no branch:
# GitHub serves a reachable commit by SHA. Verified ok=20 bad=0.
#
# The `-C <dir>` form throughout: the enhancer scans this text for bare git verbs
# and would inject a checkout plus scrub into the SHARED base if it found one.
RUN set -eux; \\
    mkdir -p /home/{repo}; \\
    git -C /home/{repo} init -q; \\
    git -C /home/{repo} remote add origin "${{REPO_URL}}"; \\
    for sha in {bundle_shas}; do \\
        for i in 1 2 3 4 5; do \\
            if git -C /home/{repo} fetch --depth 1 --no-tags -q origin "$sha"; \\
                then break; fi; \\
            echo "fetch $sha attempt $i failed, retrying"; sleep 10; \\
        done; \\
        git -C /home/{repo} cat-file -e "$sha^{{commit}}"; \\
    done; \\
    test -d /home/{repo}/.git

{clear_env}

CMD ["/bin/bash"]
""".format(
            image_name=image_name,
            global_env=self.global_env,
            clear_env=self.clear_env,
            repo=repo,
            bundle_shas=" ".join(sorted(BUNDLE_BASE_SHAS)),
        )


class Jest8751To7119ImageDefault(Image):
    """Per-PR layer: context files, the checkout, the scrub, then prepare.sh."""

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
        return Jest8751To7119ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # ---- which test files this PR needs, from its OWN test patch ----------
    #
    # jest's own suite is enormous and its e2e tests spawn child jest
    # processes; running all of it three times for each of twenty PRs is hours
    # of wall clock that proves nothing extra. The evidence f2p/n2p needs lives
    # in the files the test patch touches, so run exactly those.
    #
    # test.patch is baked into the image and identical across all three acts,
    # so the selection is constant WITHIN an instance (the acts stay
    # comparable) while differing BETWEEN instances.
    def _test_patch_files(self) -> list[str]:
        out = []
        for line in self.pr.test_patch.split("\n"):
            if line.startswith("+++ b/"):
                out.append(line[6:].strip())
        return out

    def jest_targets(self) -> list[str]:
        """Runnable test files, snapshots and fixtures excluded.

        A `.snap` is data the runner reads, not a suite it can execute; passing
        one to jest matches no test and the act reports nothing. Fixture suites
        under `e2e/<case>/__tests__` are run BY the e2e tests as child
        processes and are excluded from the root config's own run, so invoking
        them directly would execute a different thing than CI does.
        """
        out = set()
        for f in self._test_patch_files():
            # A `__snapshots__/X.test.ts.snap` IS evidence: it is the expected
            # output the suite asserts against, and a test patch often changes
            # ONLY the snapshot. Dropping it means the expectation changes while
            # the test that consumes it never runs, so the transition it encodes
            # is invisible. MEASURED on this bundle: 6 of 20 PRs (7119, 7652,
            # 7770, 7792, 8021, 8382) carry a snapshot whose test file appears
            # nowhere else in their patch. Map it back to its suite:
            #     a/b/__snapshots__/X.test.ts.snap -> a/b/X.test.ts
            if f.endswith(".snap"):
                mapped = re.sub(r"__snapshots__/", "", f)[: -len(".snap")]
                if re.search(r"\.(test|spec)\.[jt]sx?$", mapped):
                    if not (mapped.startswith("e2e/")
                            and not mapped.startswith("e2e/__tests__/")):
                        out.add(mapped)
                continue
            if not re.search(r"\.(js|jsx|ts|tsx)$", f):
                continue
            # Everything under e2e/ EXCEPT the top-level e2e/__tests__/ suites
            # is fixture material. The nesting depth varies -- both
            # `e2e/transform/__tests__/x.js` and
            # `e2e/transform/transform-environment/__tests__/add.test.js` occur
            # -- so match on "not the top-level suite dir" rather than counting
            # path segments, which silently let the deeper ones through.
            if f.startswith("e2e/") and not f.startswith("e2e/__tests__/"):
                continue
            # "lives under __tests__" is too loose: `__tests__/fixtures/...`
            # and `__tests__/test_root/...` are DATA the suites load, not
            # suites. Handed to jest they match a file with no test in it and
            # the runner reports "Your test suite must contain at least one
            # test" -- a failing suite in all three acts that belongs to no
            # PR. Accept a file only if it is named like a test, or sits
            # DIRECTLY in a __tests__ directory (e.g.
            # e2e/__tests__/supports-dashed-args.js, a real suite without the
            # .test suffix).
            named_test = re.search(r"\.test\.[jt]sx?$", f) is not None
            directly_in_tests = re.search(r"(^|/)__tests__/[^/]+$", f) is not None
            if named_test or directly_in_tests:
                out.add(f)
        return sorted(out)

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "jest_targets.txt", "\n".join(self.jest_targets()) + "\n"),
            File(".", "check_git_changes.sh",
                 _CHECK_GIT_CHANGES_SH.format(repo=repo)),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH.format(repo=repo)),
            File(".", "yarn_install.sh", _YARN_INSTALL_SH.format(repo=repo)),
            File(".", "prepare.sh", _PREPARE_SH.format(repo=repo, sha=sha)),
            File(".", "run_tests.sh",
                 _RUN_TESTS_SH.format(repo=repo, jest_start=JEST_START,
                                      jest_end=JEST_END, jest_json=JEST_JSON)),
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

        # The sha is written LITERALLY. build_dataset.py supplies the
        # REPO_URL / BASE_COMMIT build args only when dependency() returns a
        # str -- true for the base alone -- so a PR layer writing
        # ${BASE_COMMIT} would expand it to the empty string and the unquoted
        # `git checkout` would silently land on the default branch.
        sha = self.pr.base.sha

        # The hardening block is DERIVED from the harness's own definition,
        # never retyped. `_HARDENING_BLOCK` is READ from harness.image; nothing
        # in core is modified.
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        return f"""FROM {image_name}

{copies}
WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout {sha}

{scrub}

RUN bash /home/prepare.sh
"""


@Instance.register("jestjs", NUMBER_INTERVAL)
class Jest8751To7119(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Jest8751To7119ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def _between(text: str, start: str, end: str) -> str:
        i = text.find(start)
        if i == -1:
            return ""
        i += len(start)
        j = text.find(end, i)
        return text[i:j] if j != -1 else text[i:]

    def parse_log(self, test_log: str) -> TestResult:
        """Read jest's JSON report, never its human output.

        jest prints `PASS <file>` for a SUITE and `✓ <name>` for a test. A
        parser that matches both counts one file plus its tests and reports a
        number that matches nothing -- FLOW Issue 2. The JSON report
        distinguishes them structurally: testResults[] are files,
        assertionResults[] are tests, and only the latter are tests.
        """
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        # Occurrence counter. A suite that builds tests in a loop, or two
        # `describe` blocks sharing a title in one file, gives several tests an
        # identical fully-qualified name; without this they collapse into ONE
        # set entry and the count silently drops.
        seen: dict[str, int] = {}

        def uniq(name: str) -> str:
            n = seen[name] = seen.get(name, 0) + 1
            return name if n == 1 else f"{name} #{n}"

        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        blob = self._between(text, JEST_START, JEST_END)

        # Anchor on the report's own key, not on the first "{" in the section.
        # jest's human output prints braces (failure diffs, printed objects),
        # and a scan that starts on one of those parses nothing and reports a
        # silent 0/0/0 -- indistinguishable from a suite that ran no tests.
        # Try every candidate opening brace, nearest-to-the-report first.
        starts = [m.start() for m in re.finditer(r"\{", blob)]
        anchor = blob.find('"testResults"')
        if anchor != -1:
            starts.sort(key=lambda i: (i > anchor, abs(anchor - i)))
        data = None
        for start in starts:
            for end in range(len(blob), start, -1):
                chunk = blob[start:end]
                if not chunk.rstrip().endswith("}"):
                    continue
                try:
                    parsed = json.loads(chunk)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and "testResults" in parsed:
                    data = parsed
                break
            if data is not None:
                break

        if data is not None:
            for suite in data.get("testResults", []) or []:
                fname = suite.get("name") or ""
                # jest reports an absolute path inside the container; the ids
                # must be stable across acts, so make it repo-relative.
                #
                # Derived from the path itself, NOT from self.pr: parse_log is
                # called on bare instances (`cls.__new__(cls)`) by the audit
                # tooling, where touching self.pr raises AttributeError and the
                # whole parser looks broken when it is not.
                fname = re.sub(r"^.*?/home/[^/]+/", "", fname)
                for t in suite.get("assertionResults", []) or []:
                    full = " > ".join(
                        (t.get("ancestorTitles") or []) + [t.get("title") or ""])
                    # The tool name comes FIRST: report.py's cheating guard
                    # splits a test name on "::" and treats the head as a file
                    # path, so a head of "jest" matches no file and a PR that
                    # CREATES a test file cannot trip
                    # guard_fix_patch_touched_tests.
                    name = uniq(f"jest::{fname}::{full}".strip())
                    st = (t.get("status") or "").lower()
                    if st == "passed":
                        passed.add(name)
                    elif st in ("failed", "error"):
                        failed.add(name)
                    else:
                        skipped.add(name)

        # TestResult.__post_init__ enforces disjoint sets: failure outranks a
        # skip, and a skip outranks a pass.
        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
