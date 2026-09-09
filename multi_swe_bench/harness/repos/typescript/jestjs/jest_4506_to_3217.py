from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.dataset import Dataset as _Dataset
from multi_swe_bench.harness.repos.typescript.jestjs.jest import (
    _ERA2_MIN_PR,
    JestImageDefault,
    _parse_jest_log,
)

# ---------------------------------------------------------------------------
# Node toolchain: one default, plus per-range overrides
# ---------------------------------------------------------------------------
# The interval defaults to node:8-stretch, which grades nine of its ten PRs
# valid. It cannot grade the tenth.
#
# pr-3217 ("Support AssertionError expected and actual values") adds
# `works with node assert` to integration_tests/__tests__/failures-test.js: it
# spawns a nested jest over a fixture that calls node's own `assert`, and
# snapshots the runner's whole stdout. The snapshot was recorded in 2017-04 on
# node 7 / V8 5.4. On node:8-stretch (V8 6.0) every assertion in that captured
# output grows two synthesised async frames:
#
#     at Object.<anonymous> (__tests__/node-assertion-error-test.js:16:3)
#   +          at new Promise (<anonymous>)
#   +          at <anonymous> thrown
#
# and nothing else differs -- the formatting the fix patch introduces via
# jest-jasmine2/src/assert-support.js is present and correct in the received
# output. So the snapshot mismatches in the test stage AND in the fix stage,
# test == fix exactly (921, 373, 0 both), f2p comes out empty, and Report.check()
# grades the PR invalid for a reason that has nothing to do with the patch. The
# frames are a property of the runtime, so no test flag can suppress them; only
# the era's own Node can. node:7 is 7.10.1 / yarn 0.24.4 / npm 4.2.0 / git 2.1.4
# on jessie -- yarn accepts --frozen-lockfile, --network-timeout and
# --ignore-scripts, and git 2.1.4 covers every command in image.py's hardening
# block, so the scripts below need no variation.
#
# WHY THE TAG MUST FORK WITH THE IMAGE
# ------------------------------------
# Images dedup on image_full_name() (Image.__hash__/__eq__) and
# build_dataset.py collects them into a set, so two Dockerfiles sharing one tag
# means only one is ever built and whichever PR wins that race decides the Node
# version for the whole interval -- the exact failure jest.py's header documents
# for the previous base fork. image_tag() therefore derives from the range, not
# from a constant. The default keeps its established `base-node8` tag so the
# nine already-built, already-valid PR images are not invalidated.
_DEFAULT_NODE_IMAGE = "node:8-stretch"
_DEFAULT_BASE_TAG = "base-node8"

# (lowest PR, highest PR, node image). Ranges are inclusive and must not
# overlap; anything they do not cover falls back to the default above.
_NODE_IMAGE_RANGES: tuple[tuple[int, int, str], ...] = (
    (3217, 3217, "node:7"),
)


def _node_range(number: int) -> Optional[tuple[int, int, str]]:
    for lo, hi, image in _NODE_IMAGE_RANGES:
        if lo <= number <= hi:
            return lo, hi, image
    return None


def _node_image(number: int) -> str:
    entry = _node_range(number)
    return entry[2] if entry is not None else _DEFAULT_NODE_IMAGE


# Node images whose bundled git predates 2.10 and so mangles binary files on
# checkout. jest's modern default branch carries a root .gitattributes of
# `* text=auto eol=lf`. Before git 2.10 the `eol` attribute was applied even to
# content auto-detected as binary; 2.10 fixed it to skip binaries. On node:7
# (git 2.1.4) the clone therefore lands with 87 assets under
# packages/*/assets/ and website/static/img/ already dirty -- every CR LF byte
# pair in them, including the CR LF inside the 8-byte PNG magic itself, has
# been rewritten to a bare LF. The PR layer's `RUN git checkout ${BASE_COMMIT}`
# then aborts with "Your local changes to the following files would be
# overwritten by checkout". node:8-stretch ships git 2.11 and is unaffected,
# which is why this never surfaced on the default base.
#
# Neutralised with `* -text` written to .git/info/attributes, which outranks
# any in-tree .gitattributes (gitattributes(5) precedence) and is itself
# untracked, so it cannot dirty the worktree. This changes nothing that is
# graded: base commit b93f9c6 (2017-04) predates the file -- that tree carries
# no .gitattributes at all, so no attribute applies to the checkout the tests
# actually run against. For text blobs, which git stores LF-normalised, -text
# and eol=lf write identical bytes on Linux anyway; the only difference is that
# binaries come out verbatim, i.e. exactly what git 2.11 already did.
#
# Verified in the built base: neutralise + `git checkout -f HEAD` leaves 0 dirty
# paths, the checkout to b93f9c6 then succeeds, and that tree is clean too.
_OLD_GIT_NODE_IMAGES = frozenset({"node:7"})


def _base_tag(number: int) -> str:
    """Base image tag, named for the PR range the base serves."""
    entry = _node_range(number)
    if entry is None:
        return _DEFAULT_BASE_TAG
    lo, hi, _ = entry
    return f"base-{hi}-to-{lo}"

_INSTALL = (
    "yarn install --frozen-lockfile --network-timeout 600000 --ignore-scripts"
    " || yarn install --network-timeout 600000 --ignore-scripts"
    " || echo 'yarn install reported failure; continuing -- the jest run is the arbiter'\n"
)

_LINK = (
    "if node -e \"const p=require('fs').readFileSync('package.json','utf8');"
    'process.exit(JSON.parse(p).workspaces?0:1)"; then\n'
    '  echo "link: yarn workspaces tree -- packages/* already linked by yarn install"\n'
    "else\n"
    '  echo "link: pre-workspaces tree -- bootstrapping with lerna"\n'
    "  rm -rf packages/*/node_modules\n"
    '  npm config set before "$(git show -s --format=%cI HEAD)" || true\n'
    "  ./node_modules/.bin/lerna bootstrap --npm-client npm --concurrency 1"
    " || { npm config delete before || true;"
    " ./node_modules/.bin/lerna bootstrap --npm-client npm --concurrency 1; }"
    " || ./node_modules/.bin/lerna bootstrap --concurrency 1"
    " || echo 'bootstrap reported failure; continuing -- the jest run is the arbiter'\n"
    "  npm config delete before || true\n"
    "fi\n"
)

_BUILD = "node ./scripts/build.js\n"

# Build tolerance per PR. `scripts/build.js` globs packages/*/src/** and calls
# `require(packages/<pkg>/package.json)` once per file, so a package directory
# that exists without its manifest takes down the whole build.
#
#   3559  "Move getType from jest-matcher-utils to separate package". The new
#         packages/jest-get-type/{package.json,.npmignore,src/index.js} are
#         product code and sit in fix.patch -- correctly; the shape split above
#         moves nothing on this PR. But test.patch creates
#         packages/jest-get-type/src/__tests__/index-test.js, so at the TEST
#         stage that package directory exists with no manifest and build.js dies
#         at build.js:90 with `Cannot find module
#         '/home/jest/packages/jest-get-type/package.json'`. test-run.sh runs
#         under `set -eo pipefail`, so the stage aborted before jest ever
#         started: the test stage read (0, 0, 0), the report graded 1008 p2p /
#         f2p 0, and the PR's one real transition was filed as n2p.
#
#         Tolerating that failure is sound because the build artifacts are not
#         lost. /packages/*/build/ is gitignored and every stage resets with
#         `git clean -fd` (no -x), so the build prepare.sh ran at image-build
#         time survives into all three stages. Packages ahead of the crash are
#         rebuilt from the stage's own tree; jest-get-type onward keep the
#         base-commit artifacts -- which is exactly right, since test.patch
#         touches no src/ of those packages. The artifact baseline is therefore
#         identical across run/test/fix, so the transition stays attributable to
#         the fix patch.
#
#         Rejected alternative: filing packages/jest-get-type/package.json as
#         test-side scaffolding so build.js succeeds. A package manifest is
#         product code, and product code in test.patch is the exact defect
#         _rebalance_patches exists to undo.
#
#         Ceiling: f2p = 1, the suite path
#         packages/jest-get-type/src/__tests__/index-test.js. Its twelve named
#         tests stay n2p because the suite cannot LOAD at the test stage
#         (`require('..')` finds no src/index.js), so their names never appear
#         there at all. Promoting them would require shipping part of the fix in
#         test.patch.
#
# NOT applied to the other nine: their builds complete in every stage, and a
# tolerated build failure would otherwise be graded as a test result.
_PR_SOFT_BUILD = frozenset({3559})


def _build_step(number: int) -> str:
    if number in _PR_SOFT_BUILD:
        return _BUILD.rstrip(chr(10)) + " || true" + chr(10)
    return _BUILD


_JEST_BIN = "node ./packages/jest-cli/bin/jest.js"

# Extra flags per PR. The three graded stages of a GIVEN PR always share one
# command -- that is what makes an f2p transition attributable to the fix patch
# -- but the flags need not be identical ACROSS PRs, and here they must not be.
#
#   3651  its target snapshots embed the <green>/<red> markers the repo's
#         convert_ansi serializer emits from chalk's ANSI codes. supports-color
#         2.0.0 disables chalk when stdout is not a TTY, so without --colors
#         every one of those snapshots mismatches in BOTH the test and fix
#         stages, test == fix exactly, and f2p comes out empty. --colors is read
#         from process.argv, so it only reaches the code under test when that
#         code runs in the main process -- hence --runInBand alongside it.
#         Measured on pr-3651: --colors alone on a parallel run changed 0 of
#         1382 results; with --runInBand the suite goes 355 -> 7 failures and
#         f2p becomes exactly the 9 target tests.
#
#   4114  integration_tests/__tests__/timeouts.test.js `does not exceed the
#         timeout` spawns a nested jest with jest.setTimeout(100) against a 20ms
#         timer -- an 80ms margin that loses to CPU contention from jest's own
#         workers. It flaked test=PASS -> fix=FAIL, which trips Report.check()
#         gate 2 and discards all 40 legitimate f2p. Serialising the outer
#         runner removes the contention. Measured unloaded: still P2F in
#         parallel, clean under --runInBand.
#
# NOT applied to the other eight. --runInBand deadlocks pr-3217's fix stage:
# integration_tests/__tests__/failures-test.js spawns the nested
# node-assertion-error-test.js fixture, which then sits at 0%% CPU forever
# (measured: no output for 18 minutes, and a 360s timeout exits 124). Its run
# and test stages complete; only the fix stage hangs. All eight grade valid on
# the bare command, so they keep it.
_PR_TEST_FLAGS = {
    3651: " --runInBand --colors",
    4114: " --runInBand",
}


def _test_cmd(number: int) -> str:
    return f"{_JEST_BIN} --verbose{_PR_TEST_FLAGS.get(number, '')} 2>&1" + chr(10)

_RESET = "git reset --hard\ngit clean -fd\n"

# ---------------------------------------------------------------------------
# Patch reclassification
# ---------------------------------------------------------------------------
#
# build_lht_dataset.split_patches() decides test-vs-fix with a substring scan --
# `any(kw in path.lower() for kw in ("test", "tests", "spec", ...))` -- over the
# WHOLE path. Against a repo whose product IS a test runner that misfires
# constantly. Every one of
#
#   packages/jest-runner/src/run_test.js                        (4506)
#   packages/jest-cli/src/TestNamePatternPrompt.js              (3386)
#   packages/jest-cli/src/TestPathPatternPrompt.js              (3386)
#   packages/pretty-format/src/plugins/react_test_component.js  (4114)
#   packages/jest-cli/src/get_no_test_found.js                  (4411)
#   packages/jest-cli/src/get_no_test_found_verbose.js          (4411)
#
# is product code that lands in test.patch on the strength of the substring
# "test".
#
# Source in the test patch is applied at the test stage, i.e. BEFORE the fix
# patch, so part of the fix is already in place when the pre-fix measurement is
# taken. Usually that merely erases f2p transitions the PR had earned -- a new
# snapshot already matches because the code that produces it is present. On 4506
# it is fatal: the PR renames the environment hook `dispose` -> `setup`/
# `teardown`, run_test.js is the caller and the jest-environment-* classes are
# the callees. Shipping the caller without the callees gives all 178 suites
# `TypeError: environment.setup is not a function` at load, so the test stage
# reports `Tests: 0 total` and the classifier reads that silence as 1132 p2p +
# 162 f2p -- of which exactly one, test_environment_async, is real.
#
# Reclassify by path SHAPE instead of by substring. A section is test-side iff
# it sits in a `__tests__` / `__mocks__` / `__snapshots__` directory, or anywhere
# under `integration_tests/` (this era keeps its integration fixtures -- plain
# index.js, package.json, style.css, a custom test-environment.js -- inside that
# tree, and they are inputs to the tests rather than product code), or is the
# repo-root `test_utils.js` helper those integration tests import. Everything
# else is product code and belongs in fix.patch. Checked against all ten PRs in
# the interval: it moves exactly the six files listed above and leaves the other
# forty-one test-side paths where split_patches() put them.
_TEST_DIR_SEGMENTS = frozenset({"__tests__", "__mocks__", "__snapshots__"})
_TEST_ROOTS = ("integration_tests/",)
_TEST_BASENAMES = frozenset({"test_utils.js"})

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def _is_test_path(path: str) -> bool:
    segments = path.split("/")
    if _TEST_DIR_SEGMENTS.intersection(segments[:-1]):
        return True
    if path.startswith(_TEST_ROOTS):
        return True
    return segments[-1] in _TEST_BASENAMES


def _diff_sections(patch: str) -> list[tuple[str, str]]:
    """Split a unified diff into one (path, text) pair per `diff --git` header."""
    sections: list[tuple[str, str]] = []
    path: Optional[str] = None
    lines: list[str] = []
    for line in (patch or "").splitlines(keepends=True):
        header = _DIFF_HEADER.match(line.rstrip("\n"))
        if header:
            if path is not None:
                sections.append((path, "".join(lines)))
            path = header.group("b")
            lines = [line]
        elif path is not None:
            lines.append(line)
    if path is not None:
        sections.append((path, "".join(lines)))
    return sections


def _rebalance_patches(fix_patch: str, test_patch: str) -> tuple[str, str]:
    """Move product-code sections out of test_patch onto the end of fix_patch.

    Byte-preserving: the two returned patches together hold exactly the sections
    that went in, so the fix stage -- which applies both -- sees an unchanged
    tree. If the test patch does not round-trip through _diff_sections (a
    preamble, a trailing fragment, anything the header regex does not account
    for) the split is returned untouched, since dropping bytes from a patch that
    must later `git apply` cleanly is the worse failure.
    """
    sections = _diff_sections(test_patch)
    if not sections:
        return fix_patch, test_patch
    if "".join(text for _, text in sections) != (test_patch or ""):
        return fix_patch, test_patch

    moved = [text for path, text in sections if not _is_test_path(path)]
    if not moved:
        return fix_patch, test_patch

    kept = [text for path, text in sections if _is_test_path(path)]
    fix = fix_patch or ""
    if fix and not fix.endswith("\n"):
        fix += "\n"
    return fix + "".join(moved), "".join(kept)



# Emitting the rebalance.
#
# Rewriting the two File objects fixes what the CONTAINER runs, but nothing
# downstream: gen_report.py:599 builds every resolved-jsonl row as
# `Dataset.build(self.raw_dataset[report.id], report)`, straight off the raw
# PullRequest, and dataset.py:72-73 copies pr.fix_patch / pr.test_patch verbatim.
# Left alone, the published record would carry the substring split while the
# f2p/p2p labels beside it were measured under the shape split -- and anyone
# replaying the row would reproduce the `Tests: 0 total` collapse the labels no
# longer describe.
#
# Patch Dataset.build, following repos/c/nanopb/nanopb.py: it is the one hook
# that sees the raw PullRequest on the way to the output, and it lands on the
# OUTPUT only, so instance routing (which keys off org/number_interval/tag) is
# untouched. Scoped by the same `number >= _ERA2_MIN_PR` predicate jest.py's
# _delegate() forks on, so the 2016 npm/lerna era keeps its own split.
#
# The class's OWN __dict__ carries the guard flag, not getattr(): Dataset
# subclasses PullRequest, so an inherited flag from another registry's patch
# would wrongly suppress this one. Chaining is order-independent -- each
# registry's wrapper is scoped to its own repo and delegates onward.
if not _Dataset.__dict__.get("_jest_era2_build_patched", False):
    _jest_era2_orig_build = _Dataset.build.__func__

    def _jest_era2_build(cls, pr, report):
        ds = _jest_era2_orig_build(cls, pr, report)
        if pr.org == "jestjs" and pr.repo == "jest" and pr.number >= _ERA2_MIN_PR:
            ds.fix_patch, ds.test_patch = _rebalance_patches(
                pr.fix_patch, pr.test_patch
            )
        return ds

    _Dataset.build = classmethod(_jest_era2_build)
    _Dataset._jest_era2_build_patched = True


class JestEra2ImageBase(Image):
    """Environment image: the era's Node toolchain + the repo clone. Nothing else.

    One base per Node toolchain, tagged for the PR range it serves -- see
    _NODE_IMAGE_RANGES above for why the tag has to fork alongside the image.

    Follows jest.py's base layout contract exactly -- it emits the syntax
    directive itself, which makes DockerfileEnhancer.enhance() return the file
    verbatim, so the base has to supply the proxy/CA/locale wiring on its own.
    It does that from image.py's own constants, so that wiring cannot drift away
    from every other repo. The Dockerfile stops at `git clone`: no checkout, no
    history scrub. Both belong to the PR layer.
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

    def dependency(self) -> str:
        return _node_image(self.pr.number)

    def image_tag(self) -> str:
        return _base_tag(self.pr.number)

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        org = self.pr.org
        node_image = self.dependency()
        repo_url = f"https://github.com/{org}/{repo}.git"

        # See _OLD_GIT_NODE_IMAGES: pre-2.10 git needs the eol attribute
        # neutralised before the PR layer can check out the base commit.
        attributes_fix = ""
        if node_image in _OLD_GIT_NODE_IMAGES:
            attributes_fix = (
                f"\nRUN echo '* -text' > /home/{repo}/.git/info/attributes"
                f" && git -C /home/{repo} checkout -f HEAD\n"
            )

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

FROM {node_image}

{build_args}

{DockerfileEnhancer._ENV_BLOCK}

{labels}

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}
{attributes_fix}
CMD ["/bin/bash"]
"""


class JestImagePR(JestImageDefault):
    """PR layer for the 2017 yarn era.

    Inherits jest.py's Dockerfile layout contract verbatim -- checkout, the
    clean-tree assert, the bulk COPY, prepare.sh, then the hardening block last
    -- and replaces only the base image and the four shell scripts.
    """

    def dependency(self) -> Image:
        return JestEra2ImageBase(self.pr, self._config)

    def dockerfile(self) -> str:
        """jest.py's PR layout, with its explanatory Dockerfile comments removed.

        The layout stays single-sourced in the parent so the two eras cannot
        drift apart; only lines that Docker itself ignores are dropped. A
        ``# syntax=`` parser directive is preserved, since it is a directive
        rather than a comment.
        """
        rendered = super().dockerfile()
        kept = [
            line
            for line in rendered.split("\n")
            if not line.lstrip().startswith("#")
            or line.lstrip().startswith("# syntax=")
        ]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(kept))

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        test_cmd = _test_cmd(self.pr.number)
        build = _build_step(self.pr.number)

        inherited = {f.name: f for f in super().files()}

        # jest.py writes self.pr.fix_patch / self.pr.test_patch through
        # verbatim. Rebalance them first -- see _rebalance_patches above for why
        # the dataset's split cannot be trusted in this repo.
        fix_patch, test_patch = _rebalance_patches(
            self.pr.fix_patch, self.pr.test_patch
        )

        return [
            File(".", "fix.patch", fix_patch),
            File(".", "test.patch", test_patch),
            inherited["check_git_changes.sh"],
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
test "$(git rev-parse HEAD)" = "{sha}"
echo "prepare: HEAD pinned at {sha}"

{install}
{link}
{build_soft}

npm config delete before || true
""".format(
                    repo=repo,
                    sha=sha,
                    install=_INSTALL.rstrip("\n"),
                    link=_LINK.rstrip("\n"),
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
{reset}{install}{link}{build}{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    install=_INSTALL,
                    link=_LINK,
                    build=build,
                    test_cmd=test_cmd,
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
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
{install}{link}{build}{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    install=_INSTALL,
                    link=_LINK,
                    build=build,
                    test_cmd=test_cmd,
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
{install}{link}{build}{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    install=_INSTALL,
                    link=_LINK,
                    build=build,
                    test_cmd=test_cmd,
                ),
            ),
        ]


@Instance.register("jestjs", "jest_4506_to_3217")
class Jest_4506_to_3217(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JestImagePR(self.pr, self._config)

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
