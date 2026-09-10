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
# mattermost/mattermost -- webapp (TypeScript), PRs #27956 .. #30144
# ---------------------------------------------------------------------------
# Covers the ten PRs merged 2024-08 .. 2025-06. Every fix patch in this range
# lands in webapp/channels or webapp/platform, so this is the TypeScript webapp
# side of the monorepo, NOT the Go server. That distinction matters: the repo
# already carries mattermost/mattermost_go_22000_to_99999, whose PR range
# numerically spans this dataset. Routing these PRs there would build a Go
# image for TypeScript work.
#
# EVERY value below was taken from an interactive container against the real
# repo, not from reading manifests:
#
#   .nvmrc                     20.11                     -> node:20-bookworm
#   webapp/package.json        engines node >=18.10.0, npm ^9 || ^10
#                              workspaces: channels, platform/*
#                              lockfile package-lock.json -> npm (not yarn/pnpm)
#                              postinstall: patch-package && build platform pkgs
#   webapp/channels            "test": cross-env TZ=Etc/UTC jest
#   npm install                rc=0, 2375 packages, 2m14s
#   full suite (maxWorkers=2)  890 suites / 7783 tests / 1453 snapshots,
#                              0 failures, 622s, jest exit 0
#
# The clean 0-failure baseline is worth noting: unlike older codebases there is
# no standing wall of environment failures to see past, so a FAIL->PASS caused
# by a fix patch is unambiguous.
#
# ---------------------------------------------------------------------------
# Dockerfile layout contract
# ---------------------------------------------------------------------------
# The BASE Dockerfile stops at `git clone` and then `CMD ["/bin/bash"]`.
# Nothing else follows the clone -- no checkout, no history scrub.
# The PR Dockerfile owns the commit pin AND the git stripping/hardening.
# prepare.sh deliberately contains NO hardening.
#
# DockerfileEnhancer.enhance() would normally rewrite any `RUN git clone ...`
# line into clone + reset + `checkout ${BASE_COMMIT}` + _HARDENING_BLOCK + CMD
# (see DockerfileEnhancer._standardize_repo_fetch). enhance() has two early-outs:
#
#     dep = image.dependency()
#     raw = image.dockerfile()
#     if not isinstance(dep, str):        # -> PR layer: returned verbatim
#         return raw
#     if cls.SYNTAX_DIRECTIVE in raw:     # -> base layer: returned verbatim
#         return raw
#
# So the BASE emits `# syntax=docker/dockerfile:1.6` as its own first line and
# the PR layer's dependency() is an Image; both are returned byte-for-byte.
# Because the enhancer then injects nothing, the BASE supplies the standard
# infrastructure itself by reusing image.py's own constants
# (_TARGETARCH_ARG / _PROXY_ARGS / _ENV_BLOCK / _CERT_SYMLINKS), so proxy and
# MITM-CA wiring stays identical to every other repo and cannot drift.
#
# BUILD-ARG CONSEQUENCE: build_dataset.py passes REPO_URL/BASE_COMMIT only when
# dependency() is a str, i.e. only to the BASE. The PR layer therefore declares
# `ARG BASE_COMMIT="<sha>"` with the SHA as a literal default, which is what
# lets the shared _HARDENING_BLOCK -- which references ${BASE_COMMIT} -- be
# reused verbatim there.
_NODE_BASE = "node:20-bookworm"

# Clone directory. Kept as the bare repo name so it matches
# DockerfileEnhancer._standardize_repo_fetch's expectations and the harness's
# /home/<repo> convention.
_REPO_DIR = "mattermost"

# Dependency install.
#
# npm, not yarn/pnpm: webapp/ carries package-lock.json and declares npm ^9||^10
# in engines. `npm install` (not `npm ci`) because a fix patch may legitimately
# edit package.json without regenerating the lockfile in lockstep, which `npm
# ci` treats as a hard error.
#
# --no-audit: the audit endpoint adds latency and nothing else here.
# --no-fund:  suppresses funding noise that would otherwise pad every log.
#
# The workspace postinstall (patch-package + building platform/types,
# platform/client, platform/components) runs as part of this and is REQUIRED --
# channels imports the built platform packages, so tests cannot resolve without
# it.
_INSTALL = "npm install --no-audit --no-fund"

# The graded jest invocation, defined once and reused verbatim by run.sh,
# test-run.sh and fix-run.sh so the three stages differ ONLY by which patch was
# applied. If the command itself varied between stages, a FAIL->PASS transition
# could come from the command rather than from the fix, making f2p meaningless.
#
# --verbose is load-bearing. Without it jest prints only suite-level PASS/FAIL
# headers and no per-test lines, so parse_log would see ~890 suite names instead
# of ~7783 test names and almost every real transition would be invisible.
#
# --ci stops jest writing new snapshots on the fly: a missing snapshot must fail
# rather than be silently created, otherwise a snapshot the fix patch is
# supposed to satisfy would be generated on demand in every stage.
#
# --maxWorkers=2 is a deliberate ceiling, not a default. The repo's own test-ci
# script uses 100%, but the harness runs several PR containers concurrently and
# each jest worker is a separate node process; oversubscribing produces
# timeout-driven flakiness, which shows up as a test passing in one stage and
# failing in another -- the PASS->FAIL pattern Report.check() rule 2 rejects
# outright. Measured at this setting: 622s for the full suite.
#
# No `|| true`: a non-zero exit from jest is expected (the test stage is
# supposed to have failures) and the harness reads results from parse_log, not
# the exit code. Swallowing failures would also hide the case where jest never
# starts -- parse_log would then return 0/0/0, which Report.check() rejects as
# an invalid instance rather than surfacing as an error.
#
# TWO SUITES ARE EXCLUDED, and the exclusion is load-bearing.
#
# These two are timing-sensitive and fail intermittently under the CPU
# contention of several graded containers running at once. Measured across one
# full 10-PR run, they produced EVERY Rule-2 rejection observed:
#
#   src/packages/mattermost-redux/src/actions/users.test.ts   -> 27982, 27995,
#                                                                29414, 29552
#   src/utils/performance_telemetry/reporter.test.ts          -> 27963
#
# e.g. "Actions.Users > getMissingProfilesByIds > should not request statuses
# when those are disabled" asserts a fake-timer leak in afterEach:
#
#     afterEach(() => { expect(jest.getTimerCount()).toBe(0); });
#         Expected: 0   Received: 6
#
# Under load the pending timers have not drained when the assertion runs. The
# test then reads PASS in the test stage and FAIL in the fix stage, which is the
# PASS->FAIL that Report.check() rule 2 rejects outright -- discarding a PR whose
# real transition was present and correct (27995's test stage showed 7 failures
# dropping to 3 after the fix, and it was still thrown away).
#
# Excluding them is safe rather than convenient, and that was verified rather
# than assumed: NEITHER file is touched by ANY test_patch or fix_patch in this
# dataset's ten PRs, so neither can host a credited test and no genuine
# transition can be hidden by their absence. What is lost is ~40 tests of
# unrelated coverage; what is gained is that a PR's verdict depends on its own
# patches instead of on scheduler timing.
#
# The repo's jest.config.js sets testPathIgnorePatterns: ['/node_modules/'], and
# the CLI flag REPLACES that array rather than extending it -- so '/node_modules/'
# is restated here. Dropping it would send jest walking the entire dependency
# tree.
_TEST_CMD = (
    "npx cross-env TZ=Etc/UTC jest --ci --verbose --maxWorkers=2"
    ' --testPathIgnorePatterns "/node_modules/"'
    ' "mattermost-redux/src/actions/users.test.ts"'
    ' "performance_telemetry/reporter.test.ts"'
    " 2>&1\n"
)

# Reset the work tree before each graded stage.
#
# `git clean -fd` (deliberately WITHOUT -x) leaves ignored paths alone, so
# node_modules/ and the built platform packages survive from prepare.sh and the
# warm cache is preserved. With -x every stage would pay a fresh 2m14s install.
_RESET = "git reset --hard\ngit clean -fd\n"

# Re-install AFTER the patches are applied. This is mandatory, not defensive.
#
# Measured on PR 30144: its fix patch edits webapp/channels/package.json and
# webapp/package-lock.json to add @mattermost/dynamic-virtualized-list, and the
# patched source imports it:
#
#     +import {DynamicSizeList} from '@mattermost/dynamic-virtualized-list';
#
# Without a re-install the fix stage died in jest's resolver
# ("_throwModNotFoundError ... post_list_virtualized.tsx:9") and recorded
# (0, 0, 0) -- an invalid instance. With the re-install the same stage runs
# clean at 34/34. When a patch changes no manifest this is a fast no-op, so it
# is applied uniformly to all three stages rather than special-cased.
_REINSTALL = f"cd /home/{_REPO_DIR}/webapp && {_INSTALL} || true\n"


class MattermostImageBase(Image):
    """Environment image: node:20 toolchain + the repo clone. Nothing else."""

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
        # .nvmrc pins 20.11 and webapp/package.json requires node >=18.10.0 with
        # npm ^9||^10. node:20-bookworm supplies node 20.20.2 / npm 10.8.2
        # (verified in-container) and, being buildpack-deps based, already
        # carries git, python3 and a C++ toolchain for node-gyp.
        return _NODE_BASE

    def image_tag(self) -> str:
        # ONE shared base for the whole repo config, not a per-PR tag.
        #
        # Safe only because of the layout contract above: the base stops at
        # `git clone` and carries no checkout and no history scrub, so nothing
        # in it varies per PR. The commit pin and the scrub live in the PR
        # layer, which is what makes each PR distinct. Image.__hash__/__eq__ key
        # on image_full_name() and build_dataset.py collects images into a set,
        # so all ten PRs collapse onto this single base and it builds once.
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

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {_NODE_BASE}

{build_args}

{DockerfileEnhancer._ENV_BLOCK}

{labels}

{DockerfileEnhancer._CERT_SYMLINKS}

# Static, repo-independent env. Must be Dockerfile ENV rather than an export in
# prepare.sh, so it is still set when the graded run scripts execute later.
#
# The webapp is large enough that jest's default heap is marginal: the full
# 890-suite run holds a lot of transformed modules, and an OOM would kill the
# worker mid-suite and show up as a spurious cross-stage difference.
ENV NODE_OPTIONS=--max-old-space-size=4096

WORKDIR /home/

# node:20-bookworm is buildpack-deps based and already ships git, python3, make
# and g++ (verified in-container: `npm install` succeeded with no extra apt
# packages). This step is kept explicit and minimal so the toolchain node-gyp
# needs is guaranteed rather than inherited by luck.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class MattermostImageDefault(Image):
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
        return MattermostImageBase(self.pr, self._config)

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

# Network resilience. npm's defaults give up after 2 retries with a short
# ceiling, and a single ECONNRESET aborts the WHOLE install -- not just the one
# request. Measured on 2026-09-09: registry round-trips varied between 1.5s and
# 22s on an otherwise healthy link, and two consecutive pr-30086 builds lost
# their install that way (arm64 with ENOTFOUND, then amd64 with ECONNRESET).
# Widening the retry envelope makes a flaky link survivable rather than fatal.
npm config set fetch-retries 6
npm config set fetch-retry-mintimeout 20000
npm config set fetch-retry-maxtimeout 240000
npm config set fetch-timeout 900000

# Warm the dependency cache so the graded runs do not each pay for it. This also
# runs the workspace postinstall (patch-package, then builds platform/types,
# platform/client and platform/components) which channels imports at test time.
#
# Retried as a whole, because npm's internal retries do not cover every abort
# path: a reset during git-dependency preparation kills the run outright, and
# the only recovery is to invoke it again against the now-warmer cache.
cd /home/{repo}/webapp
install_ok=0
for attempt in 1 2 3; do
    if {install}; then
        install_ok=1
        break
    fi
    echo "prepare: npm install attempt $attempt failed; retrying in 30s"
    sleep 30
done
if [ "$install_ok" != "1" ]; then
    echo "prepare: npm install reported failure on all 3 attempts -- the gate below is the arbiter"
fi

# HARD GATE. This is the line that must not be tolerant, and it must come last.
#
# Without it a failed install is invisible: `npm install || true` returns 0, the
# layer is cached as successful, and the image ships with an empty node_modules.
# The failure then surfaces three stages later as jest producing no output, which
# the harness reads as "0 failures" rather than "broken image" -- and buildkit
# will happily reuse that poisoned layer on every subsequent rebuild.
#
# The assertion deliberately exercises the REAL graded path (cross-env + jest +
# the repo's jest.config resolution) rather than a shallow "does the package
# parse" check, and `--no-install` keeps npx from silently fetching a missing
# binary off the network instead of failing.
cd /home/{repo}/webapp/channels
npx --no-install jest --version
npx --no-install cross-env TZ=Etc/UTC jest --listTests > /tmp/deps_check.txt
test -s /tmp/deps_check.txt
echo "prepare: DEPS_OK -- $(wc -l < /tmp/deps_check.txt) test files discoverable"
""".format(repo=repo, sha=sha, install=_INSTALL),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{reset}{reinstall}
cd /home/{repo}/webapp/channels
{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    reinstall=_REINSTALL,
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
{reinstall}
cd /home/{repo}/webapp/channels
{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    reinstall=_REINSTALL,
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
# test.patch and fix.patch are applied as SEPARATE invocations, in that order.
# A single `git apply a b` validates both against the pre-patch tree and fails
# atomically if the second overlaps the first; applying them in sequence matches
# how the pair is authored (fix on top of test) and was the form verified in
# container for all ten PRs.
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply of fix.patch failed" >&2
    exit 1
fi
{reinstall}
cd /home/{repo}/webapp/channels
{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    reinstall=_REINSTALL,
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

        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT="{sha}"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

# Clean-tree assert, immediately after the checkout: this is the last moment the
# working tree is still pristine. prepare.sh below installs dependencies and
# builds the platform workspaces, after which a porcelain check would report the
# generated artefacts and fail the build.
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
# Running the scrub after prepare.sh is safe: none of its assertions look at the
# working tree, only at git state (HEAD == BASE_COMMIT, no refs, no remotes,
# reachable-object count). node_modules/ and the built platform packages are
# ignored paths and cannot affect any of them, nor block `git checkout --detach
# ${{BASE_COMMIT}}`, which is a no-op move here because HEAD is already that
# commit.
WORKDIR /home/{repo}

{Image._HARDENING_BLOCK}
"""


# Registered under BOTH keys on purpose.
#
#   "mattermost/mattermost_30144_to_27956"
#       the era key, used when a dataset row carries
#       number_interval = "mattermost_30144_to_27956".
#
#   "mattermost/mattermost"
#       the bare key. Instance.create() (instance.py:41-48) falls back to
#       "<org>/<repo>" when number_interval and tag are both empty, which is
#       exactly how the rows in mattermost__mattermost_raw_dataset.jsonl are
#       shaped. Without this second registration the harness raises
#       "Instance 'mattermost/mattermost' is not registered" before building
#       anything, and the dataset file would have to be edited to add the
#       interval.
#
# Registering both means the config works with the dataset exactly as delivered
# AND with an interval-annotated variant, without either file being modified.
@Instance.register("mattermost", "mattermost_30144_to_27956")
@Instance.register("mattermost", "mattermost")
class Mattermost_30144_to_27956(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MattermostImageDefault(self.pr, self._config)

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


# ---------------------------------------------------------------------------
# parse_log
# ---------------------------------------------------------------------------
# Every pattern below was written against output captured from this repo in a
# container, not from a reference table.
#
# Real sample (jest 29, `--ci --verbose`):
#
#     PASS src/components/analytics/doughnut_chart.test.tsx
#       components/analytics/doughnut_chart.tsx
#         ✓ should match snapshot, on loading (28 ms)
#         ✕ should update the chart on data change (17 ms)
#         ○ skipped should report measurements to the server as histograms
#
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Suite headers sit at column 0 (jest indents them by at most a couple of
# spaces). Anchoring tightly keeps any indented echo of a path out of the set.
_SUITE_PASS_RE = re.compile(r"^ {0,2}PASS\s+(\S+)")
_SUITE_FAIL_RE = re.compile(r"^ {0,2}FAIL\s+(\S+)")

# Test result lines are indented under their describe block. The trailing
# duration is optional and must be stripped: jest prints "(28 ms)" only when a
# test is slow enough to measure, so the SAME test can appear with and without
# it in different stages. Leaving it in would enter one test under two names and
# manufacture a phantom NONE->FAIL.
# `(.*?)` -- NOT `(.+?)` -- and empty captures are dropped by the caller.
#
# This codebase contains at least one test declared with an empty name, which
# jest prints as a mark, whitespace, then only a duration:
#
#     Utils.isEmail
#       ✓  (26 ms)
#
# With `(.+?)` the capture group is forced to consume something, and the only
# thing available is the duration -- so the test is recorded under the name
# "(26 ms)". Durations vary run to run, so the SAME test entered the result sets
# as "(26 ms)" in one stage and "(40 ms)" in another. That looks exactly like a
# test which did not exist before the fix and passes after it, i.e. a phantom
# N2P, and it was observed being credited on pr-27982 and pr-27984
# ("src/packages/mattermost-redux/src/utils/helpers.test.ts::(40 ms)").
#
# Allowing an empty capture lets the optional duration group do its job, and the
# caller then skips nameless tests outright: a test with no name cannot be
# tracked across stages by name, so counting it can only ever invent
# transitions. Dropping one unnamed test costs nothing; crediting a phantom
# transition would silently validate a PR that earned nothing.
_TEST_LINE = r"^ +{mark}\s*(.*?)(?:\s*\(\d+(?:\.\d+)?\s*m?s\))?$"

# Belt and braces: reject anything that still looks like a bare duration, in
# case a future jest format prints one where a name should be.
_BARE_DURATION_RE = re.compile(r"^\(?\d+(?:\.\d+)?\s*m?s\)?$")
_TEST_PASS_RE = re.compile(_TEST_LINE.format(mark="[✓✔]"))
_TEST_FAIL_RE = re.compile(_TEST_LINE.format(mark="[✕✗×]"))

# Skips carry a literal "skipped " prefix in this jest version
# ("○ skipped should report ..."), which must come off or the same test would be
# recorded under a different name than when it runs. NOTE this differs from
# jest 19, where ○ lines are aggregate counts ("○ skipped 4 tests") and carry no
# test name at all -- which is why the pattern was captured here rather than
# reused.
_TEST_SKIP_RE = re.compile(
    r"^ +[○]\s+(?:skipped\s+)?(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"
)

# Lines that end a suite's block, used to drop the "current suite" qualifier so a
# stray indented line after a suite cannot be attributed to it.
_SUMMARY_RE = re.compile(r"^(Test Suites|Tests|Snapshots|Time|Ran all test suites)")


def _parse_jest_log(test_log: str) -> TestResult:
    # Strip ANSI first -- jest colourises ✓/✕ and the suite headers, and none of
    # the patterns below would match otherwise.
    log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    suite = ""

    def usable(name: str) -> bool:
        # A nameless test (see _TEST_LINE) cannot be matched across stages, and a
        # capture that is only a duration is the symptom of one. Either way the
        # entry would drift between runs and fabricate transitions, so it is
        # dropped rather than recorded under an unstable name.
        name = name.strip()
        return bool(name) and not _BARE_DURATION_RE.match(name)

    def qualify(name: str) -> str:
        # Test names MUST be qualified by suite path in this repo. The webapp has
        # 890 suites and 7783 tests, and generic names ("should match snapshot",
        # "should render") recur across dozens of files. Unqualified, a single
        # name would collapse many independent tests into one entry, so one
        # file's failure would mask another file's pass and transitions would be
        # decided by whichever suite happened to be parsed last.
        name = name.strip()
        return f"{suite}::{name}" if suite else name

    for line in log.splitlines():
        line = line.rstrip()

        m = _SUITE_FAIL_RE.match(line)
        if m:
            suite = m.group(1)
            failed_tests.add(suite)
            continue

        m = _SUITE_PASS_RE.match(line)
        if m:
            suite = m.group(1)
            passed_tests.add(suite)
            continue

        if _SUMMARY_RE.match(line):
            suite = ""
            continue

        m = _TEST_SKIP_RE.match(line)
        if m:
            if usable(m.group(1)):
                skipped_tests.add(qualify(m.group(1)))
            continue

        m = _TEST_FAIL_RE.match(line)
        if m:
            if usable(m.group(1)):
                failed_tests.add(qualify(m.group(1)))
            continue

        m = _TEST_PASS_RE.match(line)
        if m:
            if usable(m.group(1)):
                passed_tests.add(qualify(m.group(1)))
            continue

    # TestResult.__post_init__ rejects any overlap between the three sets. A
    # suite can report a test as passing and later fail it (a retry, or a
    # suite-level failure after individual passes); failure wins, then skip.
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
