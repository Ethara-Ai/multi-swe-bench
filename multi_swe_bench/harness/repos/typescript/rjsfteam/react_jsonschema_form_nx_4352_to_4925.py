"""rjsf-team/react-jsonschema-form - nx era (8 PRs, #4352 - #4925).

WHY THIS FILE EXISTS SEPARATELY FROM ``reactjsonschemaform.py``
--------------------------------------------------------------
``reactjsonschemaform.py`` in this directory is registered as the *generic*
key ``rjsf-team/react-jsonschema-form`` and serves an earlier phase's dataset
(PRs #1993 - #3763), whose JSONL rows carry no ``number_interval``.  Editing
or replacing that file would silently re-point already-processed PRs at a
different toolchain, so this era is added as a NEW ``number_interval`` config
and the generic one is left untouched.

ERA BOUNDARY - MEASURED, NOT ASSUMED
------------------------------------
Every ``base.sha`` in the dataset was checked out and inspected.  The 20 rows
split cleanly in two, and the split is NOT contiguous by PR number::

    era     .nvmrc  .node-version  engines.node  lerna.json  root "test" script
    lerna   18      18.16.0        >=14          present     lerna run --concurrency 2 --stream test
    nx      22      22.13.1        >=20          absent      nx run-many --parallel=2 --target=test

    nx era (this file, 8 PRs)
        4352 4599 4757 4815 4818 4860 4920 4925
    lerna era (react_jsonschema_form_lerna_3916_to_4669.py, 12 PRs)
        3916 3969 4002 4034 4085 4123 4326 4356 4398 4417 4570 4669

Two PRs sit outside their numeric neighbourhood and that is real, not a
mistake in the split:

* **#4352** was opened 2024-10-28 but not merged until 2025-05-02, so it was
  rebased and its ``base.sha`` (``2a0329350a``, 2025-05-01) is already on nx -
  which is why this interval starts below the lerna interval's upper bound.
* **#4669** targets the **``v5`` maintenance branch**, which stayed on lerna,
  so it belongs to the other file despite its higher number.

Because era membership follows the base commit's toolchain rather than the PR
number, the two intervals overlap numerically.  The interval names therefore
carry the toolchain token; each PR still belongs to exactly one era, so the
per-PR image tags never collide.

TEST COMMAND - WHY EACH FLAG IS THERE (all measured in-container)
----------------------------------------------------------------
``--verbose``
    Only 7 of the 14 test packages set ``verbose: true`` in their own jest
    config: core, utils, antd, mantine, semantic-ui, playground,
    validator-ajv8.  The other 7 - chakra-ui (133 tests), daisyui (150),
    fluentui-rc (133), mui (133), primereact (133), react-bootstrap (138) and
    shadcn (138) - do not, and jest then prints a file-level ``PASS`` line and
    nothing else.  Without this flag those 958 of 6974 tests are invisible to
    parse_log.  With it, the tick lines reconcile exactly with jest's own
    per-package summaries: 6974/6974, 0 packages missing.

``--skip-nx-cache``
    nx caches task output keyed by input hashes and a replayed task prints
    jest's raw output WITHOUT the ``"<package>: "`` stream prefix that
    parse_log keys on, so a cache hit yields a 0/0/0 TestResult.  This was
    observed for real on the sibling lerna era, where the fix stage replayed
    11 of 11 tasks from cache and parsed zero tests.  The flag is set here and
    ``NX_SKIP_NX_CACHE=true`` is exported as well, so neither lever alone has
    to hold.

``--nx-bail=false``
    Keeps every package running after one fails, so the three stages produce
    the same test-name universe.  Without it a single failing package can stop
    the rest from being scheduled and the missing tests become phantom
    NONE->PASS transitions in the report.

``--output-style=stream``
    Restores the per-line ``"<package>: "`` prefix that parse_log needs.

BUILD PLACEMENT - AND WHY THE TEST STAGE MUST NOT REBUILD
---------------------------------------------------------
The build lives in prepare.sh and is repeated ONLY in fix-run.sh.

In this era ``tsconfig.build.json`` type-checks the test tree, so applying
``test.patch`` alone makes the build fail by design - the new tests reference
API that only ``fix.patch`` introduces.  Measured on PR #4925::

    ../utils/test/schema/omitExtraDataTest.ts(5,3): error TS2305:
        Module '"../../src"' has no exported member 'omitExtraData'.
    Failed tasks: - @rjsf/validator-ajv8:build

and because each package's ``build:ts`` runs ``rimraf ./lib`` *before* ``tsc``,
that failure does not merely stop - it deletes the build tree.  A test stage
that rebuilt would therefore report zero tests instead of the failing ones.
Keeping the pristine prepare.sh build makes the test stage do its job: the
patched suites fail against unfixed code while all 14 packages still run.
The fix stage does rebuild, because ``fix.patch`` edits ``packages/*/src`` and
sibling packages import each other through ``dist``/``lib``.

BASE IMAGE - SHARED WITH THE LERNA ERA
--------------------------------------
This era does NOT define its own base image.  Both eras build on the single
image defined in ``_shared_base.py`` (``node:22-bookworm``), so the pipeline
produces one base for all 20 PRs instead of one per era.

Node 22 is this era's own ``.nvmrc`` pin, so nothing changes here; it is the
lerna era that moved up to meet it, which is the only direction that works -
this era's ``engines.node`` is ``">=20"`` and would reject node:18 outright.

See ``_shared_base.py`` for the full rationale, the measured proof that the
lerna era is unaffected by Node 22, and why the shared tag is not simply
``"base"``.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.rjsfteam._shared_base import (
    RjsfSharedImageBase,
)

_INTERVAL_NAME = "react_jsonschema_form_nx_4352_to_4925"



# No dataset patch in this era contains a "GIT binary patch" hunk (checked
# across all 16 patch bodies), so no image/lockfile binary exclusion is needed.
#
# package-lock.json IS excluded, and one PR in this era actually needs it:
# #4599's fix.patch carries a package-lock.json hunk alongside a new
# "fast-uri": "^3.0.6" dependency in packages/utils/package.json.
# node_modules is installed once in prepare.sh from the BASE lockfile and is
# never reinstalled, so letting the patch rewrite the lockfile would leave the
# manifest describing a tree that is not on disk.  Excluding it keeps the
# lockfile consistent with what is actually installed.  This is safe here
# because the base lockfile already resolves fast-uri at exactly 3.0.6 (hoisted
# to the root node_modules), which satisfies the constraint the fix introduces,
# so the fix stage resolves the module without a network install.
# Verified: all 16 patch bodies apply cleanly at their base.sha with these
# exact flags, both test.patch alone and test.patch + fix.patch together.
_GIT_APPLY_EXCLUDES = "--exclude=package-lock.json"

# --parallel=1 is load-bearing, not tuning: nx defaults to one worker per CPU
# and each worker inherits NODE_OPTIONS=--max-old-space-size=4096.  On a
# 10-CPU / 8 GB Docker host, with two PR images building at once, that
# overcommits and the kernel kills the build ("npm error code 137").  The
# sibling lerna era lost all 11 of its images to exactly this.
_BUILD_COMMAND = (
    "npx nx run-many --target=build --exclude=@rjsf/playground "
    "--skip-nx-cache --parallel=1"
)

# Defined once and interpolated into all three stage scripts so the graded
# invocation is byte-identical by construction.
_TEST_COMMAND = (
    "npx nx run-many --parallel=2 --target=test --skip-nx-cache "
    "--output-style=stream --nx-bail=false -- --verbose"
)


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
test -x node_modules/.bin/nx
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
# nx --output-style=stream prefixes every forwarded line with "<package>: ",
# and jest (with --verbose forced on) prints a two-space-per-level tree
# underneath a "PASS <file>" header:
#
#   @rjsf/utils:  PASS  test/schema.test.ts (12.4 s)
#   @rjsf/utils:    toPathSchema()
#   @rjsf/utils:      ✓ should return a pathSchema (3 ms)
#
# Names are qualified as "<package> > <file> > <describe...> > <test>".
# Both the package prefix and the file are required: --parallel=2 means two
# packages' lines interleave, so all parser state is keyed by package, and leaf
# titles repeat across files within a package.
#
# Trailing "(3 ms)" durations are stripped.  They vary run to run, and a name
# that differs between the run/test/fix stages is scored as two separate tests
# by Report.__post_init__.
#
# Measured on a real run of all 14 packages at base 12882d933a: jest's own
# summaries total 6974 tests, this parser records all 6974 tick lines (0 lost),
# and they collapse to 5342 unique names.  The 1632 collapses are genuine: rjsf's
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
#
# Unprefixed jest output is deliberately IGNORED rather than parsed with a
# fallback name.  The only way to get it is an nx cache replay, and a replayed
# stage would then contribute differently-named copies of tests that other
# stages report under their package prefix - the exact cross-stage mismatch
# that Report.check() rule 4 flags as anomalous.  Dropping it instead makes a
# cache hit fail loudly on rule 1 (all_count == 0) rather than quietly corrupt
# the f2p classification.
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


def rjsf_nx_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # All parser state is per package: --parallel=2 interleaves two packages'
    # lines and a shared stack would mix their describe paths.
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
            # Unprefixed lines are nx banners, never part of a jest tree.
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



class RjsfNxEraImageDefault(Image):
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
class REACT_JSONSCHEMA_FORM_NX_4352_TO_4925(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return RjsfNxEraImageDefault(self.pr, self._config)

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
        return rjsf_nx_parse_log(test_log)
