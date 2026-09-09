"""rjsf-team/react-jsonschema-form - lerna era (12 PRs, #3916 - #4669).

WHY THIS FILE EXISTS SEPARATELY FROM ``reactjsonschemaform.py``
--------------------------------------------------------------
``reactjsonschemaform.py`` in this directory is registered as the *generic*
key ``rjsf-team/react-jsonschema-form`` and serves an earlier phase's dataset
(PRs #1993 - #3763).  Its JSONL rows carry no ``number_interval``, so
``Instance.create()`` resolves them through the generic key.  Editing or
replacing that file would silently re-point already-processed PRs at a
different toolchain, so this era is added as a NEW ``number_interval`` config
and the generic one is left untouched.

ERA BOUNDARY - MEASURED, NOT ASSUMED
------------------------------------
Every ``base.sha`` in the dataset was checked out and inspected.  The 20 rows
split cleanly in two, and the split is NOT contiguous by PR number::

    era     .nvmrc  .node-version  engines.node  lerna.json  root "test" script
    lerna   18      18.16.0        >=14          present     lerna run --concurrency 2 --stream test
    nx      22      22.13.1        >=20          absent      nx run-many --parallel=2 --target=test

    lerna era (this file, 12 PRs)
        3916 3969 4002 4034 4085 4123 4326 4356 4398 4417 4570 4669
    nx era (react_jsonschema_form_nx_4352_to_4925.py, 8 PRs)
        4352 4599 4757 4815 4818 4860 4920 4925

Two PRs sit outside their numeric neighbourhood and that is real, not a
mistake in the split:

* **#4352** was opened 2024-10-28 but not merged until 2025-05-02, so it was
  rebased and its ``base.sha`` (``2a0329350a``, 2025-05-01) is already on nx.
* **#4669** targets the **``v5`` maintenance branch** (``base.ref == "v5"``),
  which stayed on lerna long after ``main`` moved to nx.

Because era membership follows the base commit's toolchain rather than the PR
number, the two intervals overlap numerically.  The interval names therefore
carry the toolchain token; each PR still belongs to exactly one era, so the
per-PR image tags never collide.

TEST COMMAND - WHY EACH FLAG IS THERE (all measured in-container)
----------------------------------------------------------------
``--verbose``
    Only 5 of the 11 test packages set ``verbose: true`` in their own jest
    config: core, utils, antd, validator-ajv6, validator-ajv8.  The other 6 -
    bootstrap-4 (78 tests), chakra-ui (73), fluent-ui (73), material-ui (73),
    mui (73) and semantic-ui (80) - do not, and jest then prints a file-level
    ``PASS`` line and nothing else.  Without this flag those 450 of 3967 tests
    are invisible to parse_log.  With it, the tick lines reconcile exactly
    with jest's own per-package summaries: 3967/3967, 0 packages missing.

``--no-bail``
    lerna 6 schedules through nx and stops launching tasks once one fails.
    Measured on PR #3916's test stage: only 2 of 11 packages ran, 972 of 3188
    tests reported, and the other ~2200 became phantom NONE->PASS transitions
    in the report.  With ``--no-bail`` every package runs in every stage, so
    the three stages produce the same test-name universe.

``--concurrency 2``
    Matches the repo's own root script and keeps peak RSS inside the runner.

BUILD PLACEMENT
---------------
The build lives in prepare.sh (baked into the PR image) and is repeated ONLY
in fix-run.sh.  ``fix.patch`` edits ``packages/*/src`` and sibling packages
import each other through ``dist``/``lib`` (``@rjsf/utils`` resolves to
``dist/index.js``), so the fix has to be recompiled to be visible.  The test
stage deliberately keeps the pristine build: its job is to fail against
unfixed code, and each package's ``build:ts`` runs ``rimraf ./lib`` before
``tsc``, so a rebuild that the test patch alone cannot satisfy would delete
the build tree instead of producing failing tests.

BASE IMAGE - SHARED WITH THE NX ERA
-----------------------------------
This era does NOT define its own base image.  Both eras build on the single
image defined in ``_shared_base.py`` (``node:22-bookworm``), so the pipeline
produces one base for all 20 PRs instead of one per era.

Node 22 rather than the ``.nvmrc``-pinned Node 18 is the only runtime that can
serve both eras, because the nx era's ``engines.node`` is ``">=20"``.  This
era declares ``">=14"``, and it was re-run end to end on node:22-bookworm at
its OLDEST base commit (PR #3916, ``fb7adf28ba``) to prove the move is free::

                            node:18   node:22
    packages reporting           11        11
    total tests                3967      3967
    per-test tick lines        3967      3967
    failed tests                  0         0
    per-package differences            NONE

See ``_shared_base.py`` for the full rationale, the per-tool ``engines.node``
audit, and why the shared tag is not simply ``"base"``.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.rjsfteam._shared_base import (
    RjsfSharedImageBase,
)

_INTERVAL_NAME = "react_jsonschema_form_lerna_3916_to_4669"



# No dataset patch in this era contains a "GIT binary patch" hunk (checked
# across all 24 patch bodies), so no image/lockfile binary exclusion is needed.
#
# package-lock.json IS excluded, and one PR in this era actually needs it:
# #4417's fix.patch carries a package-lock.json hunk alongside a new
# "ajv-i18n": "^4.2.0" dependency in packages/validator-ajv8/package.json.
# node_modules is installed once in prepare.sh from the BASE lockfile and is
# never reinstalled, so letting the patch rewrite the lockfile would leave the
# manifest describing a tree that is not on disk.  Excluding it keeps the
# lockfile consistent with what is actually installed.  This is safe here
# because the base lockfile already resolves ajv-i18n at exactly 4.2.0 (hoisted
# to the root node_modules), which satisfies the constraint the fix introduces,
# so the fix stage resolves the module without a network install.
# Verified: all 24 patch bodies apply cleanly at their base.sha with these
# exact flags, both test.patch alone and test.patch + fix.patch together.
_GIT_APPLY_EXCLUDES = "--exclude=package-lock.json"

# --concurrency 1 is load-bearing, not tuning.  lerna dispatches through nx,
# which defaults to one worker per CPU; each worker is a Node process
# inheriting NODE_OPTIONS=--max-old-space-size=4096.  On a 10-CPU / 8 GB
# Docker host, and with the pipeline building two PR images at once, the
# unbounded default overcommits and the kernel kills the build: all 11
# lerna-era images failed with "npm error code 137" (SIGKILL) at this
# exact step.  The repo ships the same idea as its own "build-serial"
# script.  Building serially costs minutes; overcommitting costs the image.
_BUILD_COMMAND = (
    "npx lerna run --concurrency 1 --stream build --ignore @rjsf/playground"
)

# Defined once and interpolated into all three stage scripts so the graded
# invocation is byte-identical by construction.
_TEST_COMMAND = "npx lerna run --concurrency 2 --stream --no-bail test -- --verbose"


_PREPARE_SH = r"""#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
export NX_SKIP_NX_CACHE=true

cd /home/__REPO__

git reset --hard
git checkout __BASE_SHA__

# The pipeline requires installs to be non-fatal, which makes them silent.
# The hard assertions below are what turn a hollow image into a loud build
# failure, so nothing in that block may carry "|| true".
npm ci || npm install || true

# npm rewrites package-lock.json files during install; restore them so the
# tree is pristine and `git apply` of test.patch/fix.patch lands cleanly.
# node_modules is gitignored and is unaffected by this restore.
git checkout -- .

# ---- HARD VERIFICATION (no "|| true" in this block) ----
test -d node_modules
test -x node_modules/.bin/lerna
test -x node_modules/.bin/jest
# --------------------------------------------------------

__BUILD_COMMAND__

# Sibling packages import each other through dist/lib, so a missing build here
# would surface later as thousands of unrelated resolution failures.
test -d packages/utils/lib
test -d packages/utils/dist
test -d packages/core/dist
"""


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
export NX_SKIP_NX_CACHE=true

cd /home/__REPO__
"""

_RESET = r"""
git reset --hard
git clean -fd
"""

_APPLY_TEST = _RESET + r"""
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch
"""

# test.patch BEFORE fix.patch, single invocation.
_APPLY_BOTH = _RESET + r"""
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch
"""

_REBUILD = r"""
echo "===== REBUILD ====="
__BUILD_COMMAND__
"""

_EXEC_TESTS = r"""
echo "===== TESTS ====="
__TEST_COMMAND__
"""

_RUN_SH = _SCRIPT_HEADER + _EXEC_TESTS
_TEST_RUN_SH = _SCRIPT_HEADER + _APPLY_TEST + _EXEC_TESTS
_FIX_RUN_SH = _SCRIPT_HEADER + _APPLY_BOTH + _REBUILD + _EXEC_TESTS




_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

ARG BASE_COMMIT="__BASE_SHA__"

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

__HARDENING__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


# ---------------------------------------------------------------------------
# parse_log
#
# lerna --stream prefixes every forwarded line with "<package>: ", and jest
# (with --verbose forced on) prints a two-space-per-level tree underneath a
# "PASS <file>" header:
#
#   @rjsf/utils:  PASS  test/schema.test.ts (12.4 s)
#   @rjsf/utils:    toPathSchema()
#   @rjsf/utils:      ✓ should return a pathSchema (3 ms)
#
# Names are qualified as "<package> > <file> > <describe...> > <test>".
# Both the package prefix and the file are required: --concurrency 2 means two
# packages' lines interleave, so all parser state is keyed by package, and leaf
# titles repeat across files within a package.
#
# Trailing "(3 ms)" durations are stripped.  They vary run to run, and a name
# that differs between the run/test/fix stages is scored as two separate tests
# by Report.__post_init__.
#
# Measured on a real run of all 11 packages at base fb7adf28ba: jest's own
# summaries total 3967 tests, this parser records all 3967 tick lines (0 lost),
# and they collapse to 3323 unique names.  The 644 collapses are genuine: rjsf's
# shared suites (packages/utils/test/schema/*Test.ts) are invoked several times
# from one .test.ts file without a distinguishing describe(), so jest itself
# prints the identical describe-path + title more than once and the log carries
# no further context to separate them.  Collapsing is the safe reading: a name
# is classified failed if ANY of its occurrences failed, so a test that is
# broken by test.patch and repaired by fix.patch still shows the FAIL -> PASS
# transition that f2p detection needs.  Ordinal suffixes were rejected because
# several test.patch bodies in this dataset edit the shared-suite aggregator
# (packages/utils/test/schema/index.ts), which shifts occurrence counts between
# stages and would rename tests mid-report.
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_PKG_RE = re.compile(r"^(@[A-Za-z0-9._/-]+):\s?(.*)$")

_SUITE_RE = re.compile(r"^\s*(?:PASS|FAIL)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)")

_PASS_SYMBOLS = "\u2713\u2714\u221a"  # ✓ ✔ √
_FAIL_SYMBOLS = "\u2715\u2717\u00d7\u2718"  # ✕ ✗ × ✘
_SKIP_SYMBOLS = "\u25cb\u25ef\u270e\u2193"  # ○ ◯ ✎ ↓

_TEST_RE = re.compile(
    r"^(\s+)([" + _PASS_SYMBOLS + _FAIL_SYMBOLS + _SKIP_SYMBOLS + r"])\s+(\S.*)$"
)

# The minus sign is not defensive padding: jest 29 emitted
# "should merge types (-2 ms)" in a real run on this repo.  Without it the
# duration stays in the name and that test gets a different name in every
# stage, which Report.__post_init__ scores as separate tests.
_DURATION_RE = re.compile(r"\s*\((?:-?\d+(?:[.,]\d+)?\s*(?:ms|s|m)\s*)+\)\s*$")

# Anything that ends the indented jest tree for a package.  Without these, a
# console.warn block or a babel notice would be pushed onto the describe stack
# and corrupt every subsequent test name in that package.
_BLOCK_TERMINATORS = (
    "Test Suites:",
    "Tests:",
    "Snapshots:",
    "Time:",
    "Ran all test suites",
    "console.log",
    "console.error",
    "console.warn",
    "console.info",
    "console.debug",
    "console.trace",
    "at ",
    "[BABEL]",
    "[baseline-browser-mapping]",
    "> ",
    "npm ",
)

_SKIP_PREFIXES = ("skipped ", "todo ")


def _clean_title(title: str) -> str:
    return _DURATION_RE.sub("", title.strip()).strip()


def rjsf_lerna_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # All parser state is per package: --concurrency 2 interleaves two
    # packages' lines and a shared stack would mix their describe paths.
    current_file: dict[str, str] = {}
    describe_stacks: dict[str, dict[int, str]] = {}
    in_block: dict[str, bool] = {}

    def record(symbol: str, name: str) -> None:
        if not name:
            return
        if symbol in _PASS_SYMBOLS:
            passed_tests.add(name)
        elif symbol in _FAIL_SYMBOLS:
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    for raw_line in test_log.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        if not line.strip():
            continue

        pkg_match = _PKG_RE.match(line)
        if not pkg_match:
            # Unprefixed lines are lerna/nx banners, never part of a jest tree.
            continue
        pkg, body = pkg_match.group(1), pkg_match.group(2)
        stripped = body.strip()

        suite_match = _SUITE_RE.match(body)
        if suite_match:
            current_file[pkg] = suite_match.group(1)
            describe_stacks[pkg] = {}
            in_block[pkg] = True
            continue

        # A tick line is unambiguous proof that we are inside the test tree, so
        # it is recorded whatever the noise flag says, and it clears that flag.
        # This is load-bearing: jest prints console blocks INSIDE the tree and
        # they end with an "at Object.<anonymous> (file:line:col)" frame.  When
        # "at " merely suppressed recording, that frame silently killed every
        # remaining test in the file - measured at 135 of 3967 tests lost in one
        # real run, which is exactly the kind of loss that corrupts f2p without
        # failing anything loudly.
        test_match = _TEST_RE.match(body)
        if test_match and pkg in current_file:
            indent = len(body) - len(body.lstrip(" "))
            symbol = test_match.group(2)
            title = _clean_title(test_match.group(3))
            if symbol in _SKIP_SYMBOLS:
                for prefix in _SKIP_PREFIXES:
                    if title.startswith(prefix):
                        title = title[len(prefix) :].strip()
                        break
            stack = describe_stacks.setdefault(pkg, {})
            context = [stack[k] for k in sorted(stack) if k < indent]
            parts = [pkg, current_file[pkg]] + context + [title]
            record(symbol, " > ".join(part for part in parts if part))
            in_block[pkg] = True
            continue

        # Noise only suspends describe-stack updates; it never stops recording.
        if stripped.startswith("\u25cf") or stripped.startswith(_BLOCK_TERMINATORS):
            in_block[pkg] = False
            continue

        indent = len(body) - len(body.lstrip(" "))
        if indent == 0:
            in_block[pkg] = False
            continue

        if not in_block.get(pkg):
            continue

        stack = describe_stacks.setdefault(pkg, {})
        describe_stacks[pkg] = {k: v for k, v in stack.items() if k < indent}
        describe_stacks[pkg][indent] = stripped

    # TestResult.__post_init__ requires the three sets to be pairwise disjoint.
    # A failure outranks everything; a real execution outranks a skip.
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



class RjsfLernaEraImageDefault(Image):
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
        return RjsfSharedImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__EXCLUDES__", _GIT_APPLY_EXCLUDES)
            .replace("__BUILD_COMMAND__", _BUILD_COMMAND)
            .replace("__TEST_COMMAND__", _TEST_COMMAND)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return (
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__HARDENING__", Image._HARDENING_BLOCK)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__CLEAR_ENV__", self.clear_env)
        )


@Instance.register("rjsf-team", _INTERVAL_NAME)
class REACT_JSONSCHEMA_FORM_LERNA_3916_TO_4669(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return RjsfLernaEraImageDefault(self.pr, self._config)

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
        return rjsf_lerna_parse_log(test_log)
