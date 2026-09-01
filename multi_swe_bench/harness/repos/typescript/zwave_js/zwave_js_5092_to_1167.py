"""Era config for zwave-js/zwave-js PRs 1167-5092 -- the **jest** era.

Why this era ends at 5092
-------------------------
zwave-js did not switch test runners in one commit. It migrated package by
package over roughly five months::

    #5093  2022-09-20  migrate `shared`        jest -> ava
    #5096  2022-09-21  migrate `core`          jest -> ava
    #5099  2022-09-22  migrate `nvmedit`       jest -> ava
    #5443  2023-02-09  migrate `transformers`  jest -> ava
    #5452  2023-02-10  migrate `config`        jest -> ava
    #5460  2023-02-14  migrate `zwave-js`      jest -> ava, deletes jest.config.js

So 1167-5092 is jest everywhere, 5461+ is ava everywhere, and 5093-5460 is a
genuinely mixed tree where *neither* runner covers the whole monorepo. The
dispatcher refuses that window rather than silently grading half a repo -- see
``zwave_js_dispatcher.py``.

No build step
-------------
Unusual for a TypeScript monorepo, and worth stating because it is the single
biggest cost saving here: ``jest.config.js`` maps every cross-package import
straight at source::

    moduleNameMapper: {
        "^@zwave-js/config(.*)": "<rootDir>/packages/config/src$1",
        "^@zwave-js/core(.*)":   "<rootDir>/packages/core/src$1",
        ...
    }

``ts-jest`` then compiles on the fly, so ``@zwave-js/core`` resolves to
``packages/core/src`` and never to a built ``build/`` directory. Running
``lerna run build`` before the tests would burn many minutes per stage and
change nothing that jest observes.

``testRegex: "(\\.|/)test\\.tsx?$"`` -- note that a file named
``Devices.unit._test.ts`` (as PR 1324 adds) does **not** match, because the
underscore breaks the ``.test.``/``/test.`` boundary. Upstream uses that
underscore convention to park a disabled test. Such a file contributes no
results in any stage, which is correct behaviour, not a parsing bug.

Package manager is per-PR, not per-era
--------------------------------------
The lockfile is not stable across this interval::

    PR 1167  yarn.lock            PR 1324  yarn.lock
    PR 1195  package-lock.json    PR 2045  yarn.lock

so ``prepare.sh`` picks the installer by inspecting the tree rather than
hard-coding one. Getting this wrong is not cosmetic: running ``npm ci`` in a
yarn-lock tree rewrites ``yarn.lock`` (npm >= 7 syncs it), which trips
``check_git_changes.sh`` and fails the build with a confusing "Uncommitted
changes".

The npm path carries one extra wrinkle. ``node:14`` ships **npm 6**, which
predates workspaces support (npm 7), so ``npm ci`` at the root installs only
root dependencies and leaves ``packages/*/node_modules`` empty. ``lerna
bootstrap`` (lerna 3 is already in devDependencies) is what links the workspace
in that case. Under yarn 1 the ``workspaces: ["packages/*"]`` field handles it
natively and no bootstrap is needed.

Test command
------------
Upstream CI runs::

    test:ci = yarn run test:ts -- --runInBand      # test:ts = jest

so all three stages run ``jest --runInBand`` and nothing else -- no reordering,
no extra selection, no ``--testPathPattern`` narrowing. ``--json
--outputFile`` is added on top; it changes *reporting* only, never which tests
are selected.

Test identity
-------------
``--json`` is used in preference to scraping ``--verbose`` output because jest
hands back the file path and the fully-qualified test name as separate fields::

    testResults[].name                  ->  /home/zwave-js/packages/.../X.test.ts
    testResults[].assertionResults[]
        .fullName                       ->  "Suite > nested > it does a thing"
        .status                         ->  passed | failed | pending | todo

which is exactly the ``<source file>::<test name>`` identity this project
requires, with no indentation heuristics and no ANSI stripping. The absolute
path is made repo-relative so ids stay stable regardless of clone location.
"""

import copy
import json
import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)

_JSON_BEGIN = "##### MSWEB-JEST-JSON-BEGIN"
_JSON_END = "##### MSWEB-JEST-JSON-END"


# ---------------------------------------------------------------- shared base
# One base image for the whole era, pinned to the NEWEST base commit among its
# PRs (PR 2045, 2021-03-18) rather than to whichever PR happens to build first.
#
# That distinction is the entire design. The enhancer appends
# `git checkout ${BASE_COMMIT}` and then scrubs history down to it, asserting
#     test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# so the image holds ONLY commits reachable from BASE_COMMIT. A later PR's
# prepare.sh can check out its own commit only if that commit is an ANCESTOR of
# the pinned one.
#
# build_dataset.py:629 takes BASE_COMMIT from `image.pr.base.sha`, so letting the
# shared base inherit the triggering PR would make correctness depend on build
# order: these four PRs form a strict chain 1195 -> 1167 -> 1324 -> 2045, and the
# JSONL lists 1167 first, which would leave 1324 and 2045 absent from the
# scrubbed history. Anchoring removes the ordering entirely -- verified with
# `git merge-base --is-ancestor`: 1167, 1195 and 1324 are all ancestors of 2045.
#
# CONSTRAINT: the anchor must stay the newest commit in the era. A future PR in
# 1167-5092 with a newer base commit will not be present in the scrubbed image.
# That fails loudly -- prepare.sh's `git checkout` errors and the build stops --
# rather than silently grading the wrong tree; the fix is to move the anchor.
_ERA_ANCHOR_SHA = "477d8504f5d2531f2d9ff3f418552aecf75bb14a"
_ERA_ANCHOR_NUMBER = 2045


def _anchor_pr(pr: PullRequest) -> PullRequest:
    """Copy of ``pr`` whose ``base.sha`` is the era anchor.

    Used only to construct the shared ImageBase, so every PR in the era resolves
    to the same ``image_full_name()`` and the same BASE_COMMIT build arg. org and
    repo are untouched, so the clone URL and image name are unchanged.
    """
    anchored = copy.deepcopy(pr)
    anchored.base.sha = _ERA_ANCHOR_SHA
    return anchored


def _gold_test_exclude_flags(test_patch: str) -> str:
    """``git apply --exclude`` flags for every file the gold test patch touches.

    Reward-hacking guard, defence in depth for
    ``test_result.fix_patch_tampers_with_tests``: that pre-run check reads
    ``get_modified_files``, which drops entries whose ``---`` side is
    ``/dev/null`` and is therefore blind to gold tests the test patch
    *creates*. Both halves are collected here.
    """
    text = (test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    paths = {m.group(2) for m in _DIFF_GIT_RE.finditer(text)}
    paths |= set(get_modified_files(test_patch or ""))
    return " ".join(f"--exclude={shlex.quote(p)}" for p in sorted(paths))


# Upstream CI's exact invocation (`test:ci = yarn run test:ts -- --runInBand`,
# where `test:ts = jest`), plus JSON reporting. --json/--outputFile affect how
# results are reported, not which tests run.
#
# The results file is emitted between markers rather than left on disk because
# the harness only ever sees stdout. `|| true` on the cat is deliberate: if
# jest died before writing the file, the stage should still finish and report
# zero tests, so Report.check() sees an empty stage instead of a truncated log.
_RUN_JEST = """jest_status=0
rm -f /tmp/msweb-jest.json
timeout -k 60 3600 npx --no-install jest --runInBand --ci \\
    --json --outputFile=/tmp/msweb-jest.json 2>&1 || jest_status=$?
printf '##### MSWEB-JEST-EXIT: %s\\n' "$jest_status"
echo '##### MSWEB-JEST-JSON-BEGIN'
cat /tmp/msweb-jest.json 2>/dev/null || true
echo
echo '##### MSWEB-JEST-JSON-END'"""


class ImageBase5092To1167(Image):
    """Per-PR base image -- Node 14, matching upstream CI's ``node-version: [14.x]``.

    Tagged ``base-pr-<N>`` rather than an era-shared name. The enhancer appends
    ``git checkout ${BASE_COMMIT}`` and then scrubs history down to it,
    asserting ``test "$(git rev-list --all --count)" = "$(git rev-list HEAD
    --count)"``. That makes the image specific to one commit, so an era-shared
    tag would let a later PR reuse an image pinned to a different base: with
    ``force_build: false`` the tag is found, the build is skipped, and
    ``prepare.sh`` then checks out a commit that may not exist in the scrubbed
    history. Whether it works depends on build order, which is worse than a
    loud failure.

    ``node:14-bullseye`` (not ``-slim``) is chosen for two reasons: it ships
    ``python3``, ``make`` and ``g++``, which ``@serialport``'s native addon
    needs when no prebuild matches the platform; and it publishes both
    ``linux/amd64`` and ``linux/arm64``, so this config can go multi-arch.
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

    def dependency(self) -> "str | Image":
        return "node:14-bullseye"

    def image_tag(self) -> str:
        # Era-shared, range-named: `base-<hi>-to-<lo>` is the established form for
        # a shared base in this tree -- 70 configs use it (base-21304-to-18210,
        # base-1053-to-659, base-0-to-1999, and base-10979-to-10979 for a
        # single-PR era).
        #
        # The range states exactly which PRs this image is valid for, and the HIGH
        # end is deliberately the anchor commit (_ERA_ANCHOR_SHA = PR 2045). That
        # makes the name self-checking: every PR it claims to serve is <= the
        # anchor, and therefore an ancestor whose commit survives the history
        # scrub. Widening the range means moving the anchor too.
        #
        # NOTE: this is 1167-2045, not the era's full 1167-5092. The base is only
        # provably valid up to the anchor, so the tag claims only that.
        return f"base-{_ERA_ANCHOR_NUMBER}-to-1167"

    def workdir(self) -> str:
        return f"base-{_ERA_ANCHOR_NUMBER}-to-1167"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class ImageDefault5092To1167(Image):
    """Per-PR image -- pins BASE_COMMIT and installs the workspace."""

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
        # Anchored: all four PRs share one base pinned to the era's newest commit.
        return ImageBase5092To1167(_anchor_pr(self.pr), self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        # MUST be exactly pr-<number>: gen_report.py:359 does
        # int(instance_dir.name[3:]) to recover the PR number, so any suffix
        # makes that raise and the instance is silently dropped from the
        # dataset -- report.json is written but never collected.
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

# `set -e`, not `set -euxo pipefail`: the install commands below end in
# `|| true`, and pipefail would let an unrelated pipeline failure abort the
# build before the hard assertions can report the real problem.
#
# Native addons (@serialport) may have no prebuild for this arch and fall back
# to node-gyp; node:14-bullseye already carries python3/make/g++ for that.
export CI=true
export npm_config_build_from_source=false

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Installer is chosen from the tree, not hard-coded: this interval is not
# consistent (1167/1324/2045 ship yarn.lock, 1195 ships package-lock.json).
# Using npm in a yarn-lock tree would rewrite yarn.lock and trip
# check_git_changes.sh below with a confusing "Uncommitted changes".
if [ -f yarn.lock ]; then
    # yarn 1 (bundled with node:14) understands `workspaces: ["packages/*"]`
    # natively, so the monorepo links itself and no bootstrap is needed.
    yarn install --frozen-lockfile --network-timeout 600000 || yarn install --frozen-lockfile --network-timeout 600000 || true
else
    # node:14 ships npm 6, which predates workspaces (npm 7). `npm ci` installs
    # root deps only and leaves packages/*/node_modules empty, so lerna -- which
    # is already in devDependencies -- links the workspace.
    # Pin transitive resolution to the base commit's own date. `lerna bootstrap`
    # below runs a fresh `npm install` per package with NO lockfile (npm 6's
    # package-lock covers root deps only), so without this it resolves every
    # transitive dep to its LATEST version. On PR 1195 that dragged in a modern
    # @so-ric/colorspace via winston -> @dabh/diagnostics, whose dist uses the
    # ES2021 logical-assignment operator:
    #     (limiters[m] ||= [])[channel] = modifier;
    #             SyntaxError: Unexpected token '||='
    # `||=` needs Node 15+; this era pins Node 14 to match upstream CI, so 48 of
    # 69 suites -- including both of PR 1195's gold test files -- failed to load.
    # `--before` (npm >= 6.9, and node:14 ships 6.14) makes npm resolve as of a
    # date, reproducing the dependency state that actually existed at this
    # commit. The yarn branch above needs none of this: --frozen-lockfile
    # already pins everything.
    npm config set before "$(git show -s --format=%cI HEAD)"
    npm ci --no-audit --no-fund || npm ci --no-audit --no-fund || true
    # --no-use-workspaces is load-bearing. lerna.json sets "useWorkspaces": true,
    # which makes `lerna bootstrap` print "bootstrap root only" and delegate all
    # per-package linking to the package manager. That is correct under yarn and
    # a no-op under npm 6, which has no workspaces support -- so without this
    # flag packages/*/node_modules stays empty and 53 of 69 suites die with
    # "Cannot find module 'triple-beam'". Forcing a per-package install is the
    # only way to populate them on the npm path.
    # --hoist installs shared dependencies into the ROOT node_modules instead of
    # each package's own, reproducing the layout yarn workspaces would give.
    # Without it lerna leaves every package isolated, and cross-package
    # resolution fails in three distinct ways seen here:
    #   * @xstate/graph lives in the root tree (from `npm ci`) but requires
    #     `xstate/lib/utils`, and xstate is declared by packages/zwave-js -- so
    #     the root copy cannot see it;
    #   * packages/serial's tests reach code needing @sentry/node, which only
    #     packages/zwave-js declares;
    #   * triple-beam (via winston) is not visible from every package that
    #     transitively needs it.
    # After --before alone, 44 of 69 suites still failed on exactly these three
    # modules (38 @sentry/node, 3 xstate/lib/utils, 3 triple-beam), which is one
    # structural cause rather than three bugs.
    npx --no-install lerna bootstrap --no-ci --no-use-workspaces --hoist || true
    npm config delete before

    # Restore the lockfile. `lerna bootstrap --hoist` performs an `npm install`
    # at the root (not `npm ci`), and with `before` set the resolutions differ
    # from what is committed -- so npm rewrites package-lock.json and the final
    # check_git_changes.sh below fails with "Uncommitted changes", after every
    # other assertion has already passed. node_modules is gitignored; the
    # lockfile is not. Restoring it leaves the installed tree intact and the
    # working tree pristine.
    git checkout -- package-lock.json 2>/dev/null || true
    git checkout -- 'packages/*/package-lock.json' 2>/dev/null || true
fi

# Build the workspace. The jest config maps @zwave-js/* at source via
# moduleNameMapper, so the *tests* need no build -- but `globalSetup` runs in a
# separate module registry where moduleNameMapper does NOT apply. At PR 2045
# jest.config.js sets globalSetup: "./test/jest.globalSetup.ts", which imports
# packages/config/src/ConfigManager, which requires @zwave-js/core; that resolves
# through node_modules to packages/core's "main": "build/index.js" and dies with
# "Cannot find module .../build/index.js" before a single test runs. Building
# unconditionally also removes the fragility for any other entry point outside
# the mapper. `**/build` is gitignored, so this cannot dirty the tree.
# --no-bail is required, not cosmetic. lerna runs the 6 package builds
# concurrently and aborts the whole batch on the first non-zero exit. At PR 1195
# `@zwave-js/testing` fails `tsc -b` with ELIFECYCLE/errno 2, which killed
# `@zwave-js/core`'s build one second after it started -- so
# packages/core/build/index.js never appeared and the assertion below rejected
# the image. Only core (and config, for globalSetup) actually needs to build;
# letting an unrelated package fail is correct, and the assertion still catches
# it if the one that matters does not produce output.
# Gated on the repo's own config, not on the PR number. Only a jest.config.js
# that declares `globalSetup` needs build output, because globalSetup runs in a
# module registry where moduleNameMapper does NOT apply; the tests themselves
# always resolve @zwave-js/* at source. Of this era's PRs only 2045 declares it
# (1167/1195/1324 do not), so the other three skip several minutes of tsc.
#
# Gating also avoids an unsatisfiable requirement: `lerna bootstrap` installs
# each package with a fresh `npm install` and no lockfile, so newer @types/*
# drift in and PR 1195's core no longer type-checks
# ("'Uint8Array | undefined' is not assignable to 'string'"). That breaks tsc
# but not the tests, which run through babel-jest and never type-check.
if grep -q 'globalSetup' jest.config.js; then
    npx --no-install lerna run build --stream --no-bail || true
fi

# Hard assertions. These -- not the installer's exit code -- decide whether the
# environment is usable. `|| true` above deliberately tolerates a partial install
# (an arm64 native addon with no prebuild is the common, benign case); these
# lines then fail the build loudly if that tolerance hid something that matters.
# Without them a broken install surfaces as an empty jest log in all three
# stages, which Report.check() only rejects after a full run.
npx --no-install jest --version
# babel-jest, NOT ts-jest: jest.config.js declares
#     transform: {{ "^.+\\.tsx?$": "babel-jest" }}
# at every base commit in this era, and ts-jest appears nowhere in the tree.
# babel-jest ships transitively with jest; @babel/preset-typescript is the
# explicit devDependency that actually makes the .ts files compile, so both are
# asserted.
node -e "require.resolve('babel-jest')"
node -e "require.resolve('@babel/preset-typescript')"
test -f jest.config.js

# Workspace linkage. Only the npm branch above can leave this broken: node:14
# ships npm 6, which has no workspaces, so packages/*/node_modules is populated
# by `lerna bootstrap` -- and that call ends in `|| true`. Without this check a
# failed bootstrap builds a green image whose every test then dies on a missing
# import, in all three stages, yielding f2p=0 and an invalid report with the
# cause buried in a test log. Under yarn the deps are hoisted to the root
# node_modules instead. Resolving *from packages/config* (which is what
# declares the dep) is what makes one assertion cover both layouts: node walks
# up from there, finding either packages/config/node_modules (lerna) or the
# hoisted root node_modules (yarn).
node -e "require.resolve('alcalzone-shared/objects', {{paths: ['/home/{pr.repo}/packages/config']}})"

# Transitive deps of a sub-package, not just its direct ones: 'triple-beam'
# arrives via winston and is what actually broke when lerna bootstrapped
# "root only". alcalzone-shared alone passed that install, so it was too narrow
# an assertion to catch it.
node -e "require.resolve('triple-beam', {{paths: ['/home/{pr.repo}/packages/core']}})"

# Build output must exist, or globalSetup cannot resolve @zwave-js/core.
# Only meaningful when a build was required; see the gate above.
if grep -q 'globalSetup' jest.config.js; then
    test -f packages/core/build/index.js
fi

# node_modules is gitignored, so the tree must still be pristine. Deliberately
# the last command: no `exit 0` follows it, so this script's exit status *is*
# this check's status.
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/{pr.repo}

{run_jest}
""".format(pr=self.pr, run_jest=_RUN_JEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/{pr.repo}
if ! git apply --exclude yarn.lock --exclude package-lock.json --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_jest}
""".format(pr=self.pr, run_jest=_RUN_JEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/{pr.repo}

# Canonical stage order: gold tests first, fix patch on top.
if ! git apply --exclude yarn.lock --exclude package-lock.json --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# At evaluation time this patch is the *agent's*, so every gold test file is
# excluded -- a fix patch that edits the tests grading it cannot take effect.
if ! git apply --exclude yarn.lock --exclude package-lock.json --whitespace=nowarn {gold_excludes} /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_jest}
""".format(pr=self.pr, gold_excludes=_gold_test_exclude_flags(self.pr.test_patch),
           run_jest=_RUN_JEST),
            ),
        ]

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

{self.clear_env}

"""


def _normalise_identity(name: str) -> str:
    """Collapse a test id to printable ASCII so encoding noise cannot fork it.

    These suites generate names containing U+2192 (the state-path arrows in
    `SerialAPICommandMachine reaches state: "x" via CREATE -> SEND_SUCCESS -> ...`).
    Stage logs are captured in chunks, and a multi-byte UTF-8 sequence split across
    a chunk boundary decodes to U+FFFD. The split lands at a *different offset in
    each stage*, so the SAME test acquires a different name per stage and
    Report.__post_init__ unions them as two separate entries -- one showing NONE in
    the stages where its other spelling appeared.

    Measured on PR 1195: 8 of 3229 names drifted this way, producing 18 phantom
    "appeared" and 8 phantom "vanished". After normalising, appeared falls to
    exactly the 10 real gold transitions and vanished to 0.

    Dropping every non-ASCII character (arrow and replacement char alike) and then
    collapsing whitespace makes both spellings converge, because the hole left
    behind is the same width either way. Real tests stay distinct: the arrows carry
    no information the surrounding state names do not already encode.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^ -~]", " ", name)).strip()


def parse_jest_json(test_log: str, repo: str = "zwave-js") -> TestResult:
    """Classify every assertion from jest's ``--json`` report.

    The report is emitted between ``MSWEB-JEST-JSON-BEGIN``/``-END`` markers so
    it can be separated from whatever the tests themselves logged to stdout.
    Only the last such block is read: if a stage somehow emitted two, the later
    one is the authoritative run.

    Test ids are ``<repo-relative file>::<fullName>``. ``fullName`` is jest's
    own concatenation of the enclosing ``describe`` blocks and the ``it``
    title, so nesting needs no reconstruction here.
    """
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    # jest writes the report via --outputFile, so the JSON itself carries no
    # colour codes -- but the surrounding stage log does, and a marker wrapped in
    # an escape sequence would not be found by rfind. Stripping first costs one
    # pass and removes the assumption entirely.
    text = ANSI_ESCAPE.sub("", test_log or "")
    start = text.rfind(_JSON_BEGIN)
    end = text.rfind(_JSON_END)
    if start == -1 or end == -1 or end <= start:
        # No report block: jest never got far enough to write one. An empty
        # TestResult is the honest answer -- Report.check() then rejects the
        # stage rather than a partial parse inventing passes.
        return TestResult(
            passed_count=0, failed_count=0, skipped_count=0,
            passed_tests=set(), failed_tests=set(), skipped_tests=set(),
        )

    blob = text[start + len(_JSON_BEGIN):end].strip()
    try:
        report = json.loads(blob)
    except (ValueError, TypeError):
        return TestResult(
            passed_count=0, failed_count=0, skipped_count=0,
            passed_tests=set(), failed_tests=set(), skipped_tests=set(),
        )

    prefix = f"/home/{repo}/"
    for suite in report.get("testResults") or []:
        path = suite.get("name") or ""
        path = path.replace("\\", "/")
        idx = path.find(prefix)
        rel = path[idx + len(prefix):] if idx != -1 else path.lstrip("/")

        cases = suite.get("assertionResults") or []

        # A suite that fails to *load* -- a missing module, a syntax error, a
        # broken import -- reports status "failed" with an EMPTY assertionResults
        # list. Iterating only assertionResults therefore renders it completely
        # invisible: the stage reports its surviving tests as passing and zero
        # failures, so a catastrophically broken environment looks healthy.
        #
        # This is not hypothetical. PR 1195 lost 53 of 69 suites to
        # "Cannot find module 'triple-beam'" and this function reported
        # 193 passed / 0 failed, while jest's own summary said
        # numFailedTestSuites=54, success=false.
        #
        # Recording one synthetic failure keyed on the file makes the breakage
        # visible to Report.check() and is stable across stages, so a suite that
        # is broken in run/test but loads in fix is still a legitimate f2p.
        if not cases and suite.get("status") == "failed":
            failed.add(_normalise_identity(f"{rel}::<test suite failed to run>"))
            continue

        for case in cases:
            name = case.get("fullName") or case.get("title") or ""
            if not name:
                continue
            ident = _normalise_identity(f"{rel}::{name}")
            status = case.get("status")
            if status == "passed":
                passed.add(ident)
            elif status == "failed":
                failed.add(ident)
            else:
                # pending / todo / disabled / skipped
                skipped.add(ident)

    # A name can never occupy two buckets; failure wins over a retry's pass.
    passed -= failed
    passed -= skipped
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("zwave-js", "zwave_js_5092_to_1167")
class ZWAVE_JS_5092_TO_1167(Instance):
    """Instance handler for the jest era (PRs 1167-5092)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault5092To1167(self.pr, self._config)

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
        return parse_jest_json(test_log, self.pr.repo)
