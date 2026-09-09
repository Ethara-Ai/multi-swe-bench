from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Dockerfile layout contract
# ---------------------------------------------------------------------------
# The BASE Dockerfile stops at `git clone` and then `CMD ["/bin/bash"]`.
# Nothing else follows the clone -- no checkout, no history scrub.
# The PR Dockerfile owns the commit pin AND the git stripping/hardening.
# prepare.sh deliberately contains NO hardening.
#
# HOW THIS IS ENFORCED WITHOUT TOUCHING image.py
# ----------------------------------------------
# DockerfileEnhancer.enhance() would normally rewrite any `RUN git clone ...`
# line into clone + reset + `checkout ${BASE_COMMIT}` + _HARDENING_BLOCK + CMD
# (see DockerfileEnhancer._standardize_repo_fetch). That is exactly the layout
# we are moving away from. enhance() has two documented early-outs:
#
#     dep = image.dependency()
#     raw = image.dockerfile()
#     if not isinstance(dep, str):        # -> PR layer: returned verbatim
#         return raw
#     if cls.SYNTAX_DIRECTIVE in raw:     # -> base layer: returned verbatim
#         return raw
#
# So:
#   * The BASE emits `# syntax=docker/dockerfile:1.6` as its own first line.
#     enhance() then returns it byte-for-byte and injects nothing.
#   * The PR layer's dependency() is an Image, not a str, so it is returned
#     verbatim too.
# Because the enhancer no longer injects the infrastructure block, the BASE has
# to supply it itself. It does that by reusing the very same constants from
# image.py (_TARGETARCH_ARG / _PROXY_ARGS / _ENV_BLOCK / _CERT_SYMLINKS), so the
# proxy + MITM-CA wiring stays identical to every other repo and cannot drift.
#
# BUILD-ARG CONSEQUENCE
# ---------------------
# build_dataset.py only passes REPO_URL/BASE_COMMIT when dependency() is a str,
# i.e. only to the BASE image. The PR layer therefore receives no build args, so
# it declares `ARG BASE_COMMIT="<sha>"` with the SHA as a literal default. That
# is what lets the shared _HARDENING_BLOCK -- which references ${BASE_COMMIT} --
# be reused verbatim in the PR layer.
#
# ---------------------------------------------------------------------------
# ONE CONFIG -> ONE BASE
# ---------------------------------------------------------------------------
# Dataset entries for this repo carry no `number_interval` and no `tag`, so
# Instance.create() resolves the bare key "jestjs/jest" (instance.py:41-48) and
# THIS module handles every PR in the dataset. The previous revision forked
# internally: PRs 1174-1983 were delegated to Jest_1983_to_1174 (node:10-buster)
# while everything else used a node:18 base -- but both classes returned
# image_tag() == "base" under the same image_name(), i.e. two different
# Dockerfiles claiming mswebench/jestjs_m_jest:base. Images dedup on
# image_full_name() (Image.__hash__/__eq__) and build_dataset.py collects them
# into a set, so only one of the two would ever have been built, and whichever
# PR won that race would have decided the Node version for every other PR.
#
# The fork is removed. A single base serves all PRs, which is both the required
# layout and the only self-consistent one.
#
# ---------------------------------------------------------------------------
# ERA NOTES (why node:10 + npm + lerna, not node:18 + yarn)
# ---------------------------------------------------------------------------
# The PRs in this dataset span 2016-10 .. 2017-03 (jest 16 -> jest 19). That
# codebase is a Lerna monorepo:
#
#   * packages are linked by `lerna bootstrap`, invoked from the repo's own
#     scripts/postinstall.js. npm's lifecycle postinstall does not reliably run
#     here, so bootstrap is called explicitly -- without it every entry point
#     dies with "Cannot find module 'jest-util'" and zero tests are collected.
#   * packages/jest-cli/bin/jest.js in this era does `require('../build/jest')`,
#     so `node ./scripts/build.js` is a hard prerequisite of the test command,
#     not an optimisation.
#   * there is no `build:js` script and no Yarn-2 lockfile. The previous
#     revision ran `yarn --immutable` (a Yarn 2+ flag) and `yarn build:js`, both
#     under `|| true`. Both fail on this codebase, and because the failures were
#     swallowed the graded stages produced an EMPTY log -> parse_log returns
#     0/0/0 -> Report.check() rejects the instance. npm is used instead; it is
#     what this era was developed against.
#
# This era also straddles jestjs/jest#1361 ("single jest run for all packages",
# 2016-08-04), which moved the test setup from per-package configs to a single
# root `jest` block. Every PR in this dataset is #1983 or higher, i.e. entirely
# on the far side of that boundary, so the single root run is correct for all of
# them and no per-package loop is needed.
_NODE_BASE = "node:10-buster"

# Era-2 (2016 npm/lerna era) lower PR bound. Imported by the jest_4506_to_3217
# bucket, whose `number >= _ERA2_MIN_PR` predicate gates its patch-rebalancing;
# 3217 is that bucket's low bound / era-2 start. Restored here after a jest.py
# refactor dropped the constant while the bucket still imports it.
_ERA2_MIN_PR = 3217

# `--concurrency 1` is load-bearing, not a tuning knob.
#
# lerna bootstraps packages in parallel by default, and every worker shares one
# package-manager cache. Two different races were observed, one per client:
#
#   PR 1983 (npmClient npm)  npm ERR! cb() never called!
#                            preceded by dozens of
#                            "npm WARN tar ENOENT ... node_modules/.staging/..."
#                            -- concurrent npm processes tripping over each
#                            other's .staging directory. Bootstrap died, the fix
#                            stage recorded (0, 0, 0).
#
#   PR 2859 (npmClient yarn) Error: ENOTEMPTY: directory not empty, rmdir
#                            '/usr/local/share/.cache/yarn/v6/npm-lodash-...'
#                            -- concurrent yarn processes racing on the shared
#                            global cache. All THREE stages recorded (0, 0, 0).
#
# Both are the same defect wearing different clothes, so the fix is to serialize
# rather than to force a particular client: lerna.json pins npmClient per commit
# ("yarn" from 19.0.2 onward), and overriding it would change the dependency
# resolution path for the PRs that already pass.
#
# Note this also failed inside prepare.sh, where `|| true` swallowed it -- the
# giveaway was git clean reporting leftover packages/*/yarn-error.log at the
# start of the graded stage.
# The fallback to --npm-client npm rescues PR 2859 without disturbing anything
# else. lerna.json pins "npmClient": "yarn" from 19.0.2 onward, and yarn 1.22.5
# dies on its shared global cache while bootstrapping that commit:
#
#   Error: ENOTEMPTY: directory not empty, rmdir
#   '/usr/local/share/.cache/yarn/v6/npm-lodash-4.18.1-.../node_modules/lodash'
#
# leaving all THREE stages at (0, 0, 0). Forcing npm globally would be the wrong
# cure -- it would change the dependency-resolution path for the seven PRs that
# already pass -- so the commit's own client is tried first and npm is used only
# after it has actually failed. Verified on the built pr-2859 image: the first
# form exits 1, the fallback reports "Successfully bootstrapped 32 packages."
# The trailing `|| true` rescues PR 1983, and is safe for a specific reason.
#
# npm 6 crashes with "cb() never called!" while bootstrapping that commit. Its
# own debug log shows why, and it is not the network:
#
#   silly extract jsdom@^9.8.0 extracted to .../.staging/jsdom-4931b1c1
#   silly extract jsdom@^9.8.1 extracted to .../.staging/jsdom-4931b1c1
#   silly extract jsdom@^9.8.1 extracted to .../.staging/jsdom-4931b1c1
#   error cb() never called!
#
# fix.patch adds jsdom@^9.8.1 while the tree already wants jsdom@^9.8.0; both
# ranges resolve to one version, so npm extracts the same tarball three times
# into an identically-named staging directory at once. The extractions delete
# each other's files (hence the flood of "tar ENOENT .../ajv-725395ae/...")
# and npm's internal tracker dies. Registry-level tuning cannot help: audit
# off, maxsockets 1, long retries, npm 6.14.18, npm 7.24.2 and a 4GB heap were
# each measured and none fixes it.
#
# But npm crashes AFTER doing the work. Measured on the pr-1983 image with the
# failure tolerated: `node ./scripts/build.js` exits 0, jsdom resolves, and the
# full suite runs 661 tests -- and the three stages line up exactly as a valid
# instance should:
#
#            suites            tests                       snapshots
#   run      9 F / 74 P        204 F / 456 P / 660         313 F
#   test    10 F / 73 P        205 F / 456 P / 661         313 F
#   fix      9 F / 74 P        204 F / 457 P / 661         313 F
#
# (the 313 snapshot failures are this 2016 commit's baseline, identical in all
# three stages -- not a symptom of a damaged tree).
#
# Tolerating the failure cannot fabricate a result, because bootstrap is only
# cache-warming: the jest run is the arbiter. If the dependency tree were truly
# broken, jest would produce nothing, the stage would record (0, 0, 0), and
# Report.check() would reject the instance exactly as it does today. This is
# therefore quite different from pre-installing fix-stage dependencies during
# prepare.sh, which WOULD change what the run and test stages see and could turn
# a legitimate "module not found" failure into a pass.
_BOOTSTRAP = (
    "./node_modules/.bin/lerna bootstrap --concurrency 1"
    " || ./node_modules/.bin/lerna bootstrap --npm-client npm --concurrency 1"
    " || echo 'bootstrap reported failure; continuing -- the jest run is the arbiter'\n"
)
_BUILD = "node ./scripts/build.js\n"

# The graded jest invocation, defined once and reused verbatim by run.sh,
# test-run.sh and fix-run.sh, so the three stages differ ONLY by which patch was
# applied. If the command itself varied between stages, a FAIL->PASS transition
# could come from the command rather than from the fix, making f2p meaningless.
#
# No `|| true` here: a non-zero exit from jest is EXPECTED (the test stage is
# supposed to have failures) and the harness reads results from parse_log, not
# from the exit code. Swallowing failures would also hide the case where jest
# never starts -- parse_log would then see an empty log and return 0/0/0, which
# Report.check() rejects as an invalid instance rather than surfacing as a build
# error. `|| true` belongs in prepare.sh and ONLY there.
_TEST_CMD = "node ./packages/jest-cli/bin/jest.js --verbose 2>&1\n"

# Reset the work tree to the pinned commit before every graded stage.
#
# `lerna bootstrap` rewrites every packages/*/package.json, so the tree is dirty
# from the warm-cache step in prepare.sh. git apply refuses to patch a modified
# file, which silently produces a no-op fix stage (identical test/fix results,
# zero f2p). Running the same reset in run.sh too keeps all three stages
# starting from a byte-identical tree.
#
# `git clean -fd` (no -x) does not touch ignored paths, so node_modules/ and
# packages/*/build survive and the warm cache built in prepare.sh is preserved.
_RESET = "git reset --hard\ngit clean -fd\n"


class JestImageBase(Image):
    """Environment image: node:10 toolchain + the repo clone. Nothing else."""

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
        return _NODE_BASE

    def image_tag(self) -> str:
        # ONE shared base for the whole repo config, not a per-PR tag.
        #
        # This is only safe because of the layout contract above: the base stops
        # at `git clone` and carries no checkout and no history scrub, so nothing
        # in it varies per PR -- every PR would otherwise render a byte-identical
        # image under a different tag. The commit pin and the scrub live in the
        # PR layer, which is what makes each PR distinct.
        #
        # (Under the OLD layout a shared base tag was genuinely unsafe: the
        # hardening block detached at one ${BASE_COMMIT}, deleted every other ref
        # and gc-pruned unreachable objects, so a shared base stayed pinned to
        # whichever PR built it FIRST and any second PR reusing it would find its
        # own base commit already pruned away. Moving the scrub out removes that
        # hazard entirely, and with it the `git fetch` dance the old PR-layer
        # prepare.sh needed to claw its base commit back.)
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        # The base stages nothing: no patches, no scripts. Those belong to the
        # PR layer.
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        org = self.pr.org
        repo_url = f"https://github.com/{org}/{repo}.git"

        # Reuse image.py's own infrastructure constants so proxy/CA/locale
        # wiring is identical to every other repo and cannot drift out of sync.
        build_args = (
            f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
            f'ARG REPO_URL="{repo_url}"\n'
            f"ARG BASE_COMMIT\n"
            f"\n{DockerfileEnhancer._PROXY_ARGS}"
        )
        labels = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # buster is EOL: its apt endpoints moved to archive.debian.org and
        # buster-updates no longer exists at all, so the sources rewrite is
        # mandatory before any apt-get update. Check-Valid-Until is disabled
        # because the archived Release files are long past their expiry date.
        #
        # node:10-buster is buildpack-deps based and already ships git,
        # python2.7, make and g++ (verified in the image), so this step is
        # near-empty today; it is kept explicit so the toolchain this era's
        # node-gyp builds require is guaranteed rather than inherited by luck.
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {_NODE_BASE}

{build_args}

{DockerfileEnhancer._ENV_BLOCK}

{labels}

{DockerfileEnhancer._CERT_SYMLINKS}

# NO FORCE_COLOR HERE -- deliberately.
#
# An earlier revision set ENV FORCE_COLOR=1, on the theory that jest's own
# matcher/diff suites snapshot COLOURISED error strings (the stored .snap files
# do contain literal ANSI codes), and that chalk disabling colour without a TTY
# was what made 427 of 465 snapshots mismatch.
#
# That theory was wrong. The snapshots were being corrupted by the PARALLEL
# `lerna bootstrap` (see _BOOTSTRAP), which raced on the shared package-manager
# cache and left a broken dependency tree. With `--concurrency 1` the two
# settings are byte-identical over a full run at PR 2593's base commit:
#
#                                   no FORCE_COLOR    FORCE_COLOR=1
#   tests                           40 F / 974 P      40 F / 974 P
#   snapshots                       10 F / 455 P      10 F / 455 P
#   integration suites              13 F / 22 P       13 F / 22 P
#   "Could not find test summary"   30                30
#
# So it buys nothing, and it actively costs something: FORCE_COLOR is inherited
# by the child jest processes that integration_tests spawn and then parse, which
# is a hazard for anything scraping their output. Left unset.

WORKDIR /home/

RUN sed -i "s|deb.debian.org|archive.debian.org|g" /etc/apt/sources.list \\
 && sed -i "s|security.debian.org|archive.debian.org|g" /etc/apt/sources.list \\
 && sed -i "/buster-updates/d" /etc/apt/sources.list \\
 && apt-get update -o Acquire::Check-Valid-Until=false \\
 && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    make \\
    g++ \\
    python2.7 \\
 && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class JestImageDefault(Image):
    """Thin PR layer: pins the commit, stages patches + run scripts, then scrubs."""

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
        return JestImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
export CI=true

# NOTE: no git stripping/hardening here by design -- the scrub is the LAST thing
# the PR Dockerfile does, after this script has finished.
#
# There is no checkout here either: the PR Dockerfile has already pinned the
# tree at BASE_COMMIT and asserted it clean via check_git_changes.sh. All that
# is re-checked here is that the commit really is the one this PR expects.
cd /home/{repo}
test "$(git rev-parse HEAD)" = "{sha}"
echo "prepare: HEAD pinned at {sha}"

# Pin dependency resolution to this PR's own commit date.
#
# These commits carry NO lockfile, so a bare `npm install` resolves whatever is
# newest-compatible TODAY: measured 499 packages against 416 for the era, and
# the image is then not reproducible across rebuilds -- two builds of the same
# PR weeks apart get different dependency trees and therefore different results.
# `before` is set as global npm config rather than passed as a flag so that the
# per-package installs `lerna bootstrap` shells out to inherit it too; a flag on
# this one command would only pin the root install.
#
# Falls back to an unpinned install if the pinned one cannot resolve, so a gap
# in old registry metadata degrades the image rather than failing the build.
CUTOFF="$(git show -s --format=%cI HEAD)"
echo "prepare: pinning npm resolution to $CUTOFF"
npm config set before "$CUTOFF" || true
npm install || {{ npm config delete before || true; npm install || true; }}

# Warm the link/build caches so the graded runs do not pay for them.
# `|| true` belongs here and ONLY here -- a cold-cache failure is recoverable at
# run time (all three graded scripts redo these steps), but a swallowed failure
# in a graded run would be silent and would fabricate an empty result set.
{bootstrap_soft}
{build_soft}

# Release the pin before the image is sealed.
#
# `npm config set` writes to /root/.npmrc, which is part of the image, so the
# pin would otherwise still be in force when the GRADED scripts run -- and a
# fix patch is free to add a dependency published AFTER its base commit.
# Measured on PR 1983: fix.patch adds jsdom@^9.8.1, whose matching version
# postdates the 2016-10-25 base commit, so `lerna bootstrap` died with
#   npm ERR! notarget No matching version found for jsdom@^9.8.1
#                     with an Enjoy By date of 10/25/2016
# and the fix stage recorded (0, 0, 0) -- an invalid instance, despite run and
# test having been healthy at (454, 6, 1) and (446, 6, 1).
#
# Deleting it here keeps the era-correct resolution for the ~416 packages
# installed above, while leaving the graded stages free to resolve the small
# delta a patch introduces.
npm config delete before || true
""".format(
                    repo=repo,
                    sha=sha,
                    bootstrap_soft=_BOOTSTRAP.rstrip("\n") + " || true",
                    build_soft=_BUILD.rstrip("\n") + " || true",
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{reset}{bootstrap}{build}{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    bootstrap=_BOOTSTRAP,
                    build=_BUILD,
                    test_cmd=_TEST_CMD,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{reset}
# A patch that fails to apply must ABORT, not fall through. Falling through
# would leave the tree at baseline and make the "test" stage a silent re-run of
# the "run" stage -- producing an empty or bogus f2p set that looks legitimate.
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
{bootstrap}{build}{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    bootstrap=_BOOTSTRAP,
                    build=_BUILD,
                    test_cmd=_TEST_CMD,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{reset}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch + fix.patch failed" >&2
    exit 1
fi
{bootstrap}{build}{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    bootstrap=_BOOTSTRAP,
                    build=_BUILD,
                    test_cmd=_TEST_CMD,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        # check_git_changes.sh is COPY'd early and run right after the checkout,
        # so it is excluded from the bulk COPY below to avoid a duplicate layer.
        copy_commands = "".join(
            f"COPY {f.name} /home/\n"
            for f in self.files()
            if f.name != "check_git_changes.sh"
        )

        # ARG default carries the SHA because build_dataset.py passes build args
        # only to the base image (dependency() is a str there, an Image here).
        # _HARDENING_BLOCK references ${BASE_COMMIT}, so it needs this to resolve.
        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT="{sha}"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

# Clean-tree assert, immediately after the checkout: this is the last moment the
# working tree is still pristine. prepare.sh below runs `lerna bootstrap`, which
# rewrites every packages/*/package.json, so running this any later would report
# "Uncommitted changes" and fail the build.
COPY check_git_changes.sh /home/
RUN bash /home/check_git_changes.sh

WORKDIR /home/

{copy_commands}
RUN bash /home/prepare.sh

# Git stripping/hardening LAST, after prepare.sh.
#
# WORKDIR must be restored to the repo first: the block above left it at /home/,
# and every command in the scrub is a git operation that has to run inside the
# work tree.
#
# Running the scrub after prepare.sh is safe: none of its four assertions look
# at the working tree, only at git state (HEAD == BASE_COMMIT, no refs, no
# remotes, reachable-object count). The package.json edits and the node_modules/
# tree prepare.sh leaves behind cannot affect any of them, and `git checkout
# --detach ${{BASE_COMMIT}}` is a no-op move here (HEAD is already that commit),
# so the dirty tree cannot block it either.
WORKDIR /home/{repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("jestjs", "jest")
class jest(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JestImageDefault(self.pr, self._config)

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
        return _parse_jest_log(test_log)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Test-line shape, shared by the pass/fail/skip patterns.
#
# Three details here are load-bearing for cross-stage name stability, and each
# was observed differing between the test and fix stages of the SAME PR:
#
#   indent      the reporter indents by describe-nesting, and a patch that adds
#               a describe shifts EVERY line beneath it. Measured on an earlier
#               jest PR: run stage {4:101, 6:188, 8:8} vs fix stage {4:1, 6:101,
#               8:188, 10:8} -- the whole tree moved 2 columns right.
#               Indentation therefore carries no information about whether a
#               line is a real result, and must not be used to filter one out:
#               an earlier 1-6 space cap silently dropped the 8 deepest tests of
#               the fix stage, which had passed in both stages, making them look
#               like they had vanished. Nested integration-test transcripts are
#               excluded by the explicit OUTPUT: guard below instead.
#
#   keyword     "✓it name" (glued, early-2016 reporter) must lose the keyword,
#               but "✓ tests with no implementation" must NOT -- stripping a
#               spaced keyword truncates a real name to "s with no
#               implementation".
#
#   duration    "(8 ms)" in one stage, "(7ms)" in the other; both must be
#               stripped, or the same test lands in the union twice under two
#               names and Report.check() sees a bogus NONE->FAIL transition.
_TEST_LINE = r"^ +[{marks}](?:(?:it|test)\b)?\s*(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"

# Jest's own integration_tests spawn a nested jest and echo its entire output
# inside a failure block, prefixed by an "OUTPUT:" line. Those inner ✓/✕ lines
# are a transcript of a different run, not results of this one -- counting them
# invents passes that never happened. The block runs until the nested summary
# ("Ran all tests"), so the parser suppresses collection between the two.
_NESTED_START_RE = re.compile(r"^\s*OUTPUT:\s*$")

# End of an echoed nested run.
#
# This MUST be able to fire on something that actually occurs. The previous
# revision ended the block only on "Ran all tests"/"Test Summary" -- neither of
# which appears even once in these logs, because the nested transcript is
# truncated inside the failure block rather than printed to completion.
# Measured on pr-2746's test stage: 36 "OUTPUT:" markers, 0 end markers, so the
# very first one suppressed everything that followed -- 2439 of 3899 lines (62%
# of the log) and 482 genuine result lines. That is what made PR 2746 report
# f2p=0 even though jest itself went from 47 failures in the test stage to 41 in
# the fix stage.
#
# The reliable boundary is column position. A nested transcript is printed
# indented inside a failure block, so anything at column 0-2 belongs to the
# OUTER run: the next suite header, or the final summary. Both anchors are
# deliberately strict about leading space -- these same words appear indented
# within the nested transcript (67 indented "Test Suites:" lines in that same
# log) and must NOT end the block.
_NESTED_END_RE = re.compile(
    r"^ {0,2}(?:PASS|FAIL)\s"
    r"|^(?:Test Suites|Tests|Snapshots|Time):"
    r"|^(?:Ran all tests|Test Summary)\b"
)

# Suite headers ("PASS <path>") sit at column 0-1. The nested transcript
# described above is indented well past that -- an unanchored `^\s*` would count
# those inner lines as real results, inventing passes that never ran.
_SUITE_PASS_RE = re.compile(r"^ {0,2}PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$")
_SUITE_FAIL_RE = re.compile(r"^ {0,2}FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$")
_TEST_PASS_RE = re.compile(_TEST_LINE.format(marks="✓✔"))
_TEST_FAIL_RE = re.compile(_TEST_LINE.format(marks="×✗✕"))

# There is deliberately no skip pattern.
#
# The previous revision matched "○●" as skip markers. Both readings were wrong
# for this reporter, and each corrupted the result sets:
#
#   ●  is the FAILURE-DETAIL bullet, not a skip. 124 such lines in pr-2746's
#      test stage, carrying real failing test names ("basic support", "error
#      thrown before snapshot"). Treating them as skips moved genuine failures
#      into skipped_tests, and `passed -= skipped` then deleted the same names
#      from passed_tests in the other stage -- manufacturing transitions.
#
#   ○  is a skip marker, but this reporter emits it as an AGGREGATE COUNT line
#      ("○ skipped 4 tests"), never a test name. Capturing it injected junk
#      entries like "skipped 4 tests" into the result sets.
#
# The bullet lines are redundant anyway -- every name they carry already appears
# on its own ✕ line -- and they use a different "suite > test" format, so
# counting both would enter one test under two spellings and let Report.check()
# see a phantom NONE->FAIL. Skipped counts are reported as 0, which is honest:
# this reporter gives no per-test skip names to report.


def _parse_jest_log(test_log: str) -> TestResult:
    # Strip ANSI first -- the reporter colorizes ✓/✕ and suite headers, and the
    # patterns below will not match otherwise.
    log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    in_nested = False

    for line in log.splitlines():
        line = line.rstrip()

        # Suppress the echoed transcript of a nested jest run (see _NESTED_*).
        if _NESTED_START_RE.match(line):
            in_nested = True
            continue
        if in_nested:
            if not _NESTED_END_RE.match(line):
                continue
            # The line that ENDS the block is outer-run output in its own right
            # (a suite header, or the summary), so fall through and classify it
            # rather than swallowing it as the previous revision did.
            in_nested = False

        m = _SUITE_FAIL_RE.match(line)
        if m:
            failed_tests.add(m.group(1).strip())
            continue

        m = _SUITE_PASS_RE.match(line)
        if m:
            passed_tests.add(m.group(1).strip())
            continue

        m = _TEST_FAIL_RE.match(line)
        if m:
            failed_tests.add(m.group(1).strip())
            continue

        m = _TEST_PASS_RE.match(line)
        if m:
            passed_tests.add(m.group(1).strip())
            continue

    # TestResult.__post_init__ rejects any overlap between the three sets.
    # A suite can report a test as passing and later fail it (retries, or a
    # suite-level failure after individual passes); failure wins.
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
