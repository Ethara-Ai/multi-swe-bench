"""Era config for zwave-js/zwave-js PRs 5461+ -- the **ava + turbo** era.

Why this era starts at 5461
---------------------------
PR #5460 (2023-02-14, "migrate ``zwave-js`` tests from Jest to AVA") is the
commit that deletes ``jest.config.js``. Before it, the repo was mid-migration
for five months (see ``zwave_js_5092_to_1167.py``); from 5461 on, every package
runs ava.

What changed besides the runner
-------------------------------
Almost everything about how the repo is built and installed::

                    jest era (<=5092)        ava era (>=5461)
    node            14.x                     18            (upstream CI matrix)
    package mgr     yarn 1 / npm 6           yarn 3.5.0    (vendored)
    linker          node_modules             nodeLinker: pnpm
    orchestration   lerna 3                  turbo
    build before?   no                       YES
    runner          jest 26                  ava 4

Three of those are load-bearing here:

**The yarn binary lives in the repo.** ``.yarnrc.yml`` sets
``yarnPath: .yarn/releases/yarn-3.5.0.cjs``, so whatever ``yarn`` is on PATH
delegates to the vendored 3.5.0. ``corepack enable`` is the supported way to
get a shim that respects that field; node 18 ships corepack, so nothing needs
downloading.

**A build IS required, unlike the jest era.** That era's ``jest.config.js``
mapped ``@zwave-js/*`` at source via ``moduleNameMapper``, so nothing was ever
compiled. Here there is no such mapping -- upstream CI compiles first::

    - name: Compile TypeScript code
      run: yarn build $TURBO_FLAGS

and ``test:ts`` runs ``turbo run test:ts``. Skipping the build leaves tests
importing ``@zwave-js/core`` with no ``build/`` output to resolve to.

**The build must be re-run in every stage.** The fix patch edits ``.ts`` under
``packages/*/src``, and ava executes compiled output, so a fix stage that
reused the base image's build would test *unfixed* code and the fix would
appear to do nothing. Warming the build in ``prepare.sh`` is still worthwhile
-- it turns a broken toolchain into one image-build error instead of three
identical stage failures -- but it is a warm-up, not a substitute.

Test command
------------
Upstream CI's ``test:ci`` is ``yarn test:ts --runInBand --forceExit``, but
those two flags are **jest** flags left behind by the migration; ava 4 rejects
unknown flags. The honest reading of "run what CI runs" here is the underlying
``test:ts`` target, which is what the ava-era CI job actually exercises::

    yarn turbo run test:ts --concurrency=1 -- --tap

``--concurrency=1`` mirrors the ``TURBO_FLAGS`` default in the root script
(``${TURBO_FLAGS:-'--concurrency=1'}``) and keeps package output from
interleaving. ``--tap`` is passed through to ava after ``--``.

Test identity
-------------
ava's TAP output is used rather than its default reporter because TAP is a
stable, line-oriented format that names the file. Two shapes carry the path::

    # packages/config/src/JsonTemplate.test.ts        <- TAP comment
    ok 1 - packages/config/src/X.test.ts > some title <- inline, multi-file runs

turbo additionally prefixes every line with the workspace it came from
(``@zwave-js/config:test:ts: ...``), which the parser strips. Ids are emitted
as ``<source file>::<test title>``; when ava gives a title with no file (a
single-file run), the most recent ``#`` comment supplies the path.
"""

import copy
import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)

# turbo tags each forwarded line with the workspace and task that produced it,
# e.g. `@zwave-js/config:test:ts: ok 1 - ...`. Stripped before TAP parsing.
#
# Both segments are greedy and space-free, and a space after the final colon is
# required. An earlier non-greedy form (`\S+?:[\w:.-]+?:\s?`) was wrong: `:` is
# itself in the second class, so it stopped at `@zwave-js/config:test:` and left
# a stray `ts: ` on the front of every line, which then matched no TAP pattern
# and silently produced zero results. Requiring the trailing space also means a
# bare TAP line (`ok 1 - ...`, no prefix) cannot match and is left untouched.
_TURBO_PREFIX_RE = re.compile(r"^[^\s]*:[^\s]*:\s+")

# Same shape, but capturing the workspace name so it can qualify test ids.
_TURBO_PREFIX_PKG_RE = re.compile(r"^(?P<pkg>[^\s:]+):[^\s]*:\s+")

# TAP: `ok 12 - description` / `not ok 12 - description`, with optional
# trailing ` # SKIP`/` # TODO` directives.
_TAP_LINE_RE = re.compile(
    r"^(?P<ok>not ok|ok)\s+\d+\s*(?:-\s*)?(?P<desc>.*?)\s*$"
)
_TAP_DIRECTIVE_RE = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

# `# packages/config/src/JsonTemplate.test.ts` -- ava emits the file as a TAP
# comment. Anything that does not look like a test file path (`# tests: 12`)
# is ignored.
_TAP_FILE_COMMENT_RE = re.compile(r"^#\s*(?P<path>\S+\.(?:ts|tsx|js|mjs|cjs))\s*$")

# ava joins file and title with U+203A (>) in multi-file runs; the ASCII
# fallback is accepted too since reporters have used both.
_TITLE_SPLIT_RE = re.compile(r"\s*(?:›|>)\s*")


# ---------------------------------------------------------------- shared base
# One base image for this era, pinned to the newest base commit among its PRs
# (PR 6067, currently the only one). Same reasoning as the sibling era file: the
# enhancer scrubs history down to BASE_COMMIT, so a shared base is reusable only
# by PRs whose commits are ANCESTORS of the pinned one, and build_dataset.py:629
# would otherwise take BASE_COMMIT from whichever PR built first.
#
# CONSTRAINT: move this anchor forward when adding a PR with a newer base commit.
# Getting it wrong fails loudly in prepare.sh's `git checkout`, not silently.
_ERA_ANCHOR_SHA = "12d2f2104d9389990df71511a8d824d5dfe1d98b"
_ERA_ANCHOR_NUMBER = 6067


def _anchor_pr(pr: PullRequest) -> PullRequest:
    """Copy of ``pr`` whose ``base.sha`` is the era anchor (shared ImageBase)."""
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


# The build is re-run in every stage on purpose: ava executes compiled output
# and the fix patch edits src/, so reusing a stale build would grade unfixed
# code. `yarn build` is turbo-cached, so an unchanged stage is cheap.
#
# NO --concurrency on `yarn build`: the root script is `yarn turbo run
# build:turbo --`, so anything appended lands PAST the `--` and reaches the
# inner script, which rejects it with
#     Unknown Syntax Error: Invalid option name ("--concurrency=1")
# That made BUILD-EXIT 1 in all three stages and would have masked a genuine
# build failure. The test command below is a direct `yarn turbo` invocation,
# not the wrapper script, so --concurrency is a valid turbo flag there.
#
# --continue is load-bearing. turbo otherwise aborts the remaining packages the
# moment one exits non-zero -- and in the test stage a failing gold test is the
# expected outcome. That bail left 5 of 6 packages unrun (162 of 3359 tests), so
# 3196 tests reported NONE and were reclassified into p2p by inference instead
# of measurement, and a gold test landed in n2p rather than f2p.
_RUN_AVA = """build_status=0
timeout -k 120 3600 yarn build 2>&1 || build_status=$?
printf '##### MSWEB-BUILD-EXIT: %s\\n' "$build_status"

ava_status=0
timeout -k 120 5400 yarn turbo run test:ts --concurrency=1 --continue -- --tap 2>&1 || ava_status=$?
printf '##### MSWEB-AVA-EXIT: %s\\n' "$ava_status\""""


class ImageBase99999To5461(Image):
    """Per-PR base image -- Node 18, matching upstream CI's ``node-version: [18]``.

    Tagged ``base-pr-<N>`` rather than an era-shared name. The enhancer appends
    ``git checkout ${BASE_COMMIT}`` and then scrubs history down to it,
    asserting ``test "$(git rev-list --all --count)" = "$(git rev-list HEAD
    --count)"``. That pins the image to one commit, so an era-shared tag would
    let a later PR reuse an image built for a different base: with
    ``force_build: false`` the tag is found, the build skipped, and
    ``prepare.sh`` then checks out a commit that may not exist in the scrubbed
    history. Correctness would depend on build order.

    ``node:18-bullseye`` (not ``-slim``) ships ``python3``/``make``/``g++`` for
    ``@serialport``'s native addon, and publishes ``linux/amd64`` and
    ``linux/arm64`` so this config can go multi-arch.
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
        return "node:18-bullseye"

    def image_tag(self) -> str:
        # Range-named like the sibling era, matching the `base-<hi>-to-<lo>` form
        # used by 70 configs in this tree. Degenerate here because this era holds
        # exactly one PR -- which has precedent too (base-10979-to-10979).
        return f"base-{_ERA_ANCHOR_NUMBER}-to-{_ERA_ANCHOR_NUMBER}"

    def workdir(self) -> str:
        return f"base-{_ERA_ANCHOR_NUMBER}-to-{_ERA_ANCHOR_NUMBER}"

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


class ImageDefault99999To5461(Image):
    """Per-PR image -- pins BASE_COMMIT, installs via yarn 3, warms the build."""

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
        return ImageBase99999To5461(_anchor_pr(self.pr), self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        # MUST be exactly pr-<number>: gen_report.py:359 does
        # int(instance_dir.name[3:]) to recover the PR number, so any suffix
        # makes that raise and the instance is silently dropped.
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

# `set -e`, not `set -euxo pipefail`: the install below ends in `|| true`, and
# pipefail would let an unrelated pipeline failure abort the build before the
# hard assertions can report the real problem.

export CI=true
# Turbo writes a daemon socket and telemetry under $HOME by default; keeping it
# inside the workspace makes the build reproducible and avoids a writable-$HOME
# assumption at run time.
export TURBO_TELEMETRY_DISABLED=1
export DO_NOT_TRACK=1

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# .yarnrc.yml sets `yarnPath: .yarn/releases/yarn-3.5.0.cjs`, so the repo
# carries its own yarn. corepack (bundled with node 18) provides the shim that
# honours that field -- nothing is downloaded.
corepack enable
yarn --version

# --immutable is yarn 3's --frozen-lockfile: it fails rather than editing
# yarn.lock, which would otherwise trip check_git_changes.sh below.
# nodeLinker is `pnpm`, so this materialises a symlinked store, not a flat
# node_modules tree.
yarn install --immutable || yarn install --immutable || true

# Warm the turbo build once. Every stage rebuilds anyway -- ava runs compiled
# output and the fix patch edits src/ -- but doing it here surfaces a broken
# toolchain as a single image-build failure instead of three identical stage
# failures with no obvious cause.
yarn build || true

# Hard assertions. These -- not the installer's exit code -- decide whether the
# environment is usable. `|| true` above deliberately tolerates a partial install
# (an arm64 native addon with no prebuild is the common, benign case); these
# lines then fail the build loudly if that tolerance hid something that matters.
# Without them a broken install surfaces as an empty ava log in all three
# stages, which Report.check() only rejects after a full run.
# `ava` is NOT a root dependency -- it is declared by 8 sub-packages (cc,
# config, core, nvmedit, serial, shared, transformers, zwave-js) -- and
# .yarnrc.yml sets `nodeLinker: pnpm`, which enforces strict resolution: the
# workspace root genuinely cannot see a package's own dependencies. Resolving
# from packages/config (which declares it) is therefore required; resolving
# from the root would fail even on a perfectly good install.
yarn node -e "require.resolve('ava', {{paths: ['/home/{pr.repo}/packages/config']}})"

# turbo IS a root devDependency (1.7.4), so this one resolves from the root.
# Asserted because `yarn build` -- which every stage runs, and which ava
# depends on since it executes compiled output -- is `turbo run build:turbo`.
yarn turbo --version

test -f .yarnrc.yml
test ! -f jest.config.js   # this era must not still be on jest

# node_modules/.yarn state and build output are gitignored, so the tree must
# still be pristine. Deliberately the last command: no `exit 0` follows it, so
# this script's exit status *is* this check's status.
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
export TURBO_TELEMETRY_DISABLED=1
export DO_NOT_TRACK=1

cd /home/{pr.repo}

{run_ava}
""".format(pr=self.pr, run_ava=_RUN_AVA),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
export TURBO_TELEMETRY_DISABLED=1
export DO_NOT_TRACK=1

cd /home/{pr.repo}
if ! git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_ava}
""".format(pr=self.pr, run_ava=_RUN_AVA),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
export TURBO_TELEMETRY_DISABLED=1
export DO_NOT_TRACK=1

cd /home/{pr.repo}

# Canonical stage order: gold tests first, fix patch on top.
if ! git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# At evaluation time this patch is the *agent's*, so every gold test file is
# excluded -- a fix patch that edits the tests grading it cannot take effect.
if ! git apply --exclude yarn.lock --whitespace=nowarn {gold_excludes} /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_ava}
""".format(pr=self.pr, gold_excludes=_gold_test_exclude_flags(self.pr.test_patch),
           run_ava=_RUN_AVA),
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


def parse_ava_tap(test_log: str) -> TestResult:
    """Classify every test from ava's TAP output, as forwarded by turbo.

    Each line may arrive prefixed by the workspace turbo ran it in
    (``@zwave-js/config:test:ts: ok 1 - ...``); that prefix is stripped first.
    The file a test belongs to comes from one of two places, in priority
    order:

    1. the description itself, when ava writes ``<file> > <title>`` (it does
       this whenever a run spans more than one file);
    2. otherwise the most recent ``# <path>`` TAP comment.

    Ids are ``<source file>::<title>``. A test whose file cannot be determined
    by either route is still recorded, keyed by title alone, rather than
    dropped -- losing it would silently shrink the stage and could fabricate a
    ``PASS -> NONE`` transition.
    """
    text = ANSI_ESCAPE.sub("", test_log or "")

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    current_file = ""

    for raw in text.splitlines():
        stripped = raw.strip()

        # Keep the workspace turbo ran this line in, rather than discarding it.
        # ava emits NO `# <path>.ts` comments in this repo (verified: zero in a
        # full 3361-result run), so the bare TAP description is the only other
        # identity available -- and two packages adding a test with the same
        # title would silently merge into one id, under-counting the stage. The
        # turbo prefix is a stable per-package qualifier present on every line
        # in every stage, so it is used as the file-equivalent.
        pkg_m = _TURBO_PREFIX_PKG_RE.match(stripped)
        pkg = pkg_m.group("pkg") if pkg_m else ""
        line = _TURBO_PREFIX_RE.sub("", stripped, count=1).strip()
        if not line:
            continue

        comment = _TAP_FILE_COMMENT_RE.match(line)
        if comment:
            current_file = comment.group("path")
            continue

        m = _TAP_LINE_RE.match(line)
        if not m:
            continue

        desc = m.group("desc")
        directive = _TAP_DIRECTIVE_RE.search(desc)
        desc = _TAP_DIRECTIVE_RE.sub("", desc).strip()
        if not desc:
            continue

        parts = _TITLE_SPLIT_RE.split(desc)
        if len(parts) > 1 and re.search(r"\.(ts|tsx|js|mjs|cjs)$", parts[0]):
            path = parts[0]
            title = " > ".join(parts[1:])
        else:
            path = current_file
            title = desc

        # Priority: an explicit path from ava > the turbo workspace > nothing.
        qualifier = path or pkg
        ident = _normalise_identity(
            f"{qualifier}::{title}" if qualifier else title
        )

        if directive:
            skipped.add(ident)
        elif m.group("ok") == "ok":
            passed.add(ident)
        else:
            failed.add(ident)

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


@Instance.register("zwave-js", "zwave_js_99999_to_5461")
class ZWAVE_JS_99999_TO_5461(Instance):
    """Instance handler for the ava + turbo era (PRs 5461+)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault99999To5461(self.pr, self._config)

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
        return parse_ava_tap(test_log)
