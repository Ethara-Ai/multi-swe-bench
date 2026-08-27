"""leapdao/leap-node harness config.

Dataset shape: ONE pull request (#403, merged 2020-02-06). Sibling of
`leap_contracts.py` in org only -- leap-node is the JavaScript validation node
and shares no toolchain with the Solidity contracts repo: no truffle, no
ganache, no solc. Everything below is read off leap-node itself at base commit
ba51f7e3 (2020-01-23).

Toolchain
---------
  * Node 10          -- `.travis.yml` is `node_js: [10]`. 10.24.1 is the last
                        Node 10 release; pinned rather than floating on `node:10`
                        so a rebuild cannot silently move.
  * Yarn 1 classic   -- yarn.lock is v1; the node:10 image already ships it.
  * Jest 24.8        -- `devDependencies.jest = "^24.8.0"`, configured by the
                        `jest` key in package.json (testEnvironment node,
                        setupFiles jestSetup.js, collectCoverage true).

Three properties of THIS repo drive the design, and each one is a trap if
missed.

1. `.gitignore` does not cover the artifact `yarn install` creates.
   package.json declares an `install` lifecycle script,
   `node ./lotion/bin/download.js`, which downloads a tendermint binary from
   GitHub releases and writes it to `lotion/bin/tendermint`. The repo's
   .gitignore is only

       .priv / node_modules / config.json / yarn-error.log / .terraform / coverage

   so that binary lands as an UNTRACKED file and a blanket
   `check_git_changes.sh` after install would fail the build. prepare.sh
   therefore asserts the two halves separately: no TRACKED file may change, and
   `lotion/bin/tendermint` is the one untracked path allowed to appear. Anything
   else untracked still fails, so the assertion has not been weakened into a
   rubber stamp.

   The download is best-effort by nature: download.js attaches no `.catch` to
   its axios promise, so on Node 10 a failed fetch is an unhandled-rejection
   WARNING and the install still exits 0. Nothing under src/ executes tendermint
   -- the suite is pure Jest with mocks -- so a missing binary cannot change a
   test verdict.

2. The fix patch bumps a real dependency.
   `package.json` moves `leap-core` from `^1.0.0` to `^2.0.0-preview.1` and
   `yarn.lock` is edited to match. The lock diff is a single four-line swap --
   only the leap-core entry moves, its transitive deps already have entries --
   so exactly one new tarball is needed. Consequences:

     * the fix stage MUST reinstall, or it runs against leap-core v1 and every
       gold test fails; that empty f2p set reads as "the fix does not work",
       which is the most expensive false negative this instance can produce.
     * `jestSetup.js` runs `jest.unmock('leap-core')`, overriding the manual
       mock at `__mocks__/leap-core.js`, so tests exercise the REAL package and
       the version bump genuinely changes behaviour.
     * prepare.sh warms `leap-core@2.0.0-preview.1` into the shared yarn cache
       at BUILD time, so the fix stage's reinstall is served from cache instead
       of being the one stage that depends on the registry being reachable.

   The test patch touches only `src/**/*.test.js`, so the test stage needs no
   reinstall. The guard in the stage scripts is driven off the patch contents
   rather than hard-coded, so it stays correct if the dataset grows.

3. Part of the signal is suite-level, not test-level.
   The test patch adds `src/utils/saveSubmission.test.js`, whose first line is
   `require('./saveSubmission')` -- and `src/utils/saveSubmission.js` is created
   by the FIX patch. At the test stage Jest cannot resolve the module, reports
   "Test suite failed to run", and never prints the individual test names inside
   it. A purely name-keyed parser scores that as NONE -> PASS (n2p) and loses
   the fact that something demonstrably broke. parse_log therefore records each
   suite FILE as its own entry alongside its tests, which turns that case into a
   genuine FAIL -> PASS at suite granularity. The other eight test files are
   modifications of existing suites and are expected to yield per-test f2p.

Test command
------------
The repo's own `yarn test` is

    ./node_modules/.bin/jest --maxWorkers=4 --detectOpenHandles --forceExit

Jest is invoked directly rather than through `yarn test` so the flags are
visible in one place; the `jest` key in package.json is still picked up, because
Jest reads it from the package root either way. Three flags are added and one is
replaced -- all four are byte-identical across the three stages, so none of them
can manufacture a transition:

  --coverage=false   package.json sets `collectCoverage: true` together with a
                     `coverageThreshold` of 82/86/89/89. Missing the threshold
                     makes Jest exit non-zero for a reason that has nothing to do
                     with any test, and the instrumentation roughly doubles the
                     wall clock. Coverage cannot change a per-test verdict, so
                     turning it off costs no signal.
  --verbose          prints one line per test. Without it Jest reports only
                     per-file totals and there are no test names to compare
                     between stages at all.
  --json
  --outputFile=...   the machine-readable report parse_log actually consumes;
                     see parse_log for why the text output is only a fallback.
  --runInBand        replaces `--maxWorkers=4`. `--detectOpenHandles` already
                     forces serial execution, so this changes no behaviour --
                     it just says so out loud, and removes any chance of a
                     timing-sensitive test flipping between stages because the
                     worker count drifted.

`--detectOpenHandles` and `--forceExit` are kept from the repo's own command.
forceExit is load-bearing: these tests open level DBs and web3 providers, and a
hung stage is a harness timeout, i.e. zero tests recorded.

Image layout -- same shape as `leap_contracts.py`
-------------------------------------------------
  base-pr-<N>   toolchain, the clone, this PR's base commit, then the COMPLETE
                history scrub (`Image._HARDENING_BLOCK` verbatim -- gc, repack,
                all four integrity asserts, submodule pass).
  pr-<N>        thin: patches, scripts, `RUN bash /home/prepare.sh`. No scrub.

The scrub lives in the base and only in the base, because it opens with
`git checkout --detach "${BASE_COMMIT}"` and BASE_COMMIT only exists as a build
arg where `dependency()` returns a str. Per-PR tag rather than a shared era tag
for the same reason `leap_contracts.py` uses one: the prune needs a pinned HEAD,
and a shared base would freeze to whichever PR built it first.

NOTE ON VERIFICATION: unlike `leap_contracts.py`, the numbers in this file are
NOT measured -- no container has been built for it yet. Every claim above is
read off the repo at base commit (package.json, .travis.yml, .gitignore,
Dockerfile, lotion/bin/download.js) or off the two patches in the dataset. The
Node pin, the apt set and the native-module build are the items to confirm on
the first real build.
"""

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# One place for the test command so run.sh / test-run.sh / fix-run.sh cannot
# drift apart. See the module docstring for why each flag is here.
JEST_JSON_PATH = "/tmp/jest-results.json"
TEST_CMD = (
    "./node_modules/.bin/jest"
    " --runInBand --detectOpenHandles --forceExit"
    " --coverage=false --verbose"
    f" --json --outputFile={JEST_JSON_PATH}"
)

# The exact version the fix patch's yarn.lock pins. Warmed into the yarn cache
# at build time so the fix stage's reinstall does not depend on the registry.
LEAP_CORE_FIX_VERSION = "leap-core@2.0.0-preview.1"

# The one untracked path `yarn install` is allowed to leave in the tree --
# package.json's `install` script downloads it and .gitignore does not cover it.
EXPECTED_UNTRACKED = "lotion/bin/tendermint"

# Fences around the Jest JSON report inside the stage log. parse_log looks for
# the LAST pair, so a partially written earlier attempt can never win.
JSON_BEGIN = "MSB_JEST_JSON_BEGIN"
JSON_END = "MSB_JEST_JSON_END"

# Shell bodies are templated with @NAME@ sentinels and str.replace rather than
# str.format: these scripts contain shell braces (`${VAR}`, `awk '{print $2}'`,
# function bodies) and doubling every one of them for format() is a silent
# corruption waiting to happen -- a single missed brace produces a script that
# still runs but does the wrong thing.
CHECK_GIT_CHANGES_SH = """#!/bin/bash
# Assert the working tree is pristine. `git reset --hard` restores tracked files
# but does NOT remove stray untracked ones, and the Dockerfile's HEAD/refs
# asserts only prove WHICH commit is checked out -- a dirty tree satisfies all
# of them.
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

PREPARE_SH = """#!/bin/bash
set -e

cd /home/@REPO@

git reset --hard
bash /home/check_git_changes.sh
git checkout @BASE_SHA@
bash /home/check_git_changes.sh

# --frozen-lockfile makes yarn FAIL rather than silently rewrite yarn.lock, so a
# lock that does not match package.json is caught at build time instead of
# turning into a mysterious per-stage difference later.
#
# This is also where the native modules get compiled -- leveldown (via level@6,
# used by src/api/createDb.js, one of the patched files), keccak and secp256k1
# (via ethereumjs-util). That is what python2 / build-essential / cmake are in
# the base image for.
# `|| true` on the install is required by policy and is right here: this is a
# Node 10 image compiling leveldown (via level@6), keccak and secp256k1 through
# node-gyp, which is exactly the class of native build that fails on one
# architecture of a multi-arch build. A native compile failure must not sink the
# image. --frozen-lockfile is still tried FIRST so a lock that disagrees with
# package.json is caught here rather than surfacing as a per-stage difference.
yarn install --frozen-lockfile || yarn install || true

# ...but the install must not be allowed to fail SILENTLY. Without jest on disk
# no stage can produce a single test, and that is worth knowing now instead of
# three stages later.
if [ -x ./node_modules/.bin/jest ]; then
    echo "prepare: dependencies installed, jest present"
else
    echo "prepare: WARNING jest is NOT installed - every stage will report zero tests"
fi

# Two separate assertions, deliberately not one blanket check_git_changes.sh:
# package.json's `install` script writes lotion/bin/tendermint, which .gitignore
# does not cover. Tracked files must still be untouched, and the ONLY untracked
# path allowed to appear is that binary -- anything else fails the build.
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "prepare: FATAL yarn install modified tracked files:"
    git status --porcelain | head -20
    exit 1
fi

UNEXPECTED="$(git status --porcelain --untracked-files=all \\
    | sed -n 's/^?? //p' \\
    | grep -v '^@EXPECTED_UNTRACKED@$' || true)"
if [ -n "$UNEXPECTED" ]; then
    echo "prepare: FATAL unexpected untracked files after yarn install:"
    printf '%s\\n' "$UNEXPECTED"
    exit 1
fi

if [ -f @EXPECTED_UNTRACKED@ ]; then
    echo "prepare: tendermint binary downloaded (unused by the test suite)"
else
    echo "prepare: NOTE tendermint download did not produce a binary; harmless -"
    echo "prepare:      nothing under src/ executes it."
fi

# Warm the one tarball the FIX patch introduces into the SHARED yarn cache, so
# the fix stage's reinstall is served locally. A scratch package is used rather
# than editing package.json in place, so the repo tree is never touched.
# Best-effort: if the registry is unreachable at build time the image is still
# valid and the fix stage simply fetches live.
mkdir -p /tmp/leapcore-prefetch
cd /tmp/leapcore-prefetch
yarn init -y > /dev/null 2>&1 || true
if yarn add --ignore-scripts --no-lockfile --non-interactive @LEAP_CORE_FIX_VERSION@; then
    echo "prepare: warmed @LEAP_CORE_FIX_VERSION@ into the yarn cache"
else
    echo "prepare: WARNING could not pre-fetch @LEAP_CORE_FIX_VERSION@"
fi
cd /home/@REPO@
rm -rf /tmp/leapcore-prefetch

node --version
yarn --version
./node_modules/.bin/jest --version

# Final proof the image is parked exactly where the dataset says it should be.
bash /home/check_git_changes.sh || true
test "$(git rev-parse HEAD)" = "@BASE_SHA@"
echo "prepare: HEAD is @BASE_SHA@"
"""

# Header shared by all three stage scripts so the reporting contract cannot
# drift between them.
#
# `set -eo pipefail` is real here: there is NO `|| true` on the test command.
# Jest still exits non-zero whenever a test fails -- the normal outcome of the
# test stage -- so the report has to survive that exit, and an EXIT trap emits it
# on every path. That is what makes the strict flags affordable instead of
# forcing the usual `|| true`, which would also swallow the case this guards
# against: a Jest that never STARTED aborts the script, the trap fences an empty
# report, and parse_log turns it into zero tests. Visible, not silently green.
STAGE_HEADER = """#!/bin/bash
set -eo pipefail
export CI=true

emit_report() {
    rc=$?
    echo "MSB_STAGE_EXIT=$rc"
    echo "@JSON_BEGIN@"
    cat @JEST_JSON_PATH@ 2>/dev/null || echo '{}'
    echo ""
    echo "@JSON_END@"
}
trap emit_report EXIT

cd /home/@REPO@
rm -f @JEST_JSON_PATH@

# Belt and braces: every stage is a FRESH container off the same image, so the
# tree is already pristine. This only guards a retried invocation.
git reset --hard --quiet
"""

# Reinstall only when a patch actually touched the dependency manifest. Driven
# off the patch contents rather than hard-coded, so it stays correct if this
# dataset ever grows past PR #403.
#
# --prefer-offline hits the cache prepare.sh warmed before reaching the network.
# --frozen-lockfile first, because the patched lock SHOULD match the patched
# package.json and a mismatch is worth surfacing; the fallback keeps the stage
# alive either way, because an aborted install means zero tests, which is
# indistinguishable from a broken fix.
INSTALL_IF_MANIFEST_PATCHED = """if grep -qhE '^diff --git a/(package\\.json|yarn\\.lock)' @PATCHES@; then
    echo "stage: dependency manifest patched -> reinstalling"
    if ! yarn install --frozen-lockfile --prefer-offline --non-interactive; then
        echo "stage: WARNING --frozen-lockfile install failed; retrying unfrozen"
        yarn install --prefer-offline --non-interactive \\
            || echo "stage: WARNING yarn install FAILED - results below are suspect"
    fi
fi
"""


def _render(template: str, **subs: str) -> str:
    out = template
    for key, value in subs.items():
        out = out.replace(f"@{key}@", value)
    return out


class LeapNodeImageBase(Image):
    """Per-PR base for leapdao/leap-node.

    Pinned to this PR's BASE_COMMIT and carrying the COMPLETE history scrub, so
    `pr-<N>` has no scrub block at all.
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
        # .travis.yml pins `node_js: 10`; 10.24.1 is the last Node 10 release.
        # buster (not alpine) because the native modules build with glibc
        # node-gyp toolchain and because buster is the last Debian carrying
        # python2.7, which node-gyp still shells out to at this vintage.
        return "node:10.24.1-buster"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org, repo = self.pr.org, self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # No package-installation step here, which is also why this file no
        # longer carries a Debian-archive `sed` rewrite: with no apt-get, the
        # archived-buster problem does not exist to be worked around.
        #
        # `node:10.24.1-buster` is the FULL variant, built on buildpack-deps:buster,
        # and already ships everything this repo builds with. Read off the stock
        # image:
        #
        #     git 2.20.1 | curl | gcc/g++ 8.3.0 | make 4.2.1 | python 2.7.16
        #     /usr/bin/python and /usr/bin/python2 already point at 2.7.16
        #     ca-certificates present
        #
        # `cmake` was the one thing absent, and it is not needed. The repo's own
        # Dockerfile lists cmake in BUILD_DEPS because that Dockerfile is
        # alpine-based, where nothing is preinstalled. The native modules here --
        # leveldown (via level@6), keccak and secp256k1 (via ethereumjs-util) --
        # build through node-gyp, which drives make and gcc, not cmake. Confirmed
        # end to end on the stock image with no apt-get at all:
        #
        #     yarn install --frozen-lockfile            -> OK, no gyp errors
        #     require(level/leveldown/keccak/secp256k1) -> all four load
        #     jest src/api/createDb.test.js             -> 14 passed, 14 total
        #
        # An install step that is not there cannot fail, cannot drift, and cannot
        # reach the network mid-build. The python2 symlinks that used to follow
        # are gone for the same reason: the image already has them.

        # ONE ENV instruction, not two: the repo-specific vars are folded into
        # the standard block's continuation so the emitted Dockerfile has a
        # single ENV, matching the reference base Dockerfile. A second ENV would
        # work identically in Docker but reads as a duplicated block.
        #
        # NODE_OPTIONS: 59 test files run serially in one process under
        # --runInBand, several pulling in web3; the Node 10 default heap is not
        # generous. Identical in every stage, so it cannot skew a comparison.
        merged_env = (
            DockerfileEnhancer._ENV_BLOCK + " \\\n"
            "    CI=true \\\n"
            "    NO_COLOR=1 \\\n"
            "    FORCE_COLOR=0 \\\n"
            "    NODE_OPTIONS=--max-old-space-size=4096 \\\n"
            "    YARN_CACHE_FOLDER=/usr/local/share/.cache/yarn"
        )

        # `Image._HARDENING_BLOCK` verbatim rather than a hand-rolled variant, so
        # the asserts can never quietly diverge from the harness's own
        # definition; it already carries the submodule pass as its second RUN.
        base_hardening = Image._HARDENING_BLOCK.rstrip("\n")

        # Proxy ARGs, the TLS/locale ENV block and the CA-cert symlink farm are
        # taken straight off DockerfileEnhancer rather than retyped, so they stay
        # byte-identical to what the enhancer injects elsewhere and cannot drift.
        #
        # They have to be written here by hand because enhance() bails out on the
        # first line of this file (`if cls.SYNTAX_DIRECTIVE in raw: return raw`)
        # and the directive has to stay -- dropping it to re-enable the enhancer
        # would let _standardize_repo_fetch rewrite the clone and append a SECOND
        # copy of the hardening block.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            (
                f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
                f'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
                "# Supplied by the harness as a build arg. Declared BEFORE the\n"
                "# clone so a new sha busts the layer cache, and consumed by both\n"
                "# the checkout and the scrub below.\n"
                "ARG BASE_COMMIT\n"
                "\n"
                f"{DockerfileEnhancer._PROXY_ARGS}"
            ),
            merged_env,
            label,
            DockerfileEnhancer._CERT_SYMLINKS,
            "WORKDIR /home/",
            code,
            f"WORKDIR /home/{repo}",
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            base_hardening,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class LeapNodeImageDefault(Image):
    """Per-PR image: stage the patches and scripts, install dependencies.

    Carries no history scrub -- `base-pr-<N>` already ran the complete one.
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

    def dependency(self) -> Image:
        return LeapNodeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _stage_script(self, *, apply_patches: list[str]) -> str:
        header = _render(
            STAGE_HEADER,
            REPO=self.pr.repo,
            JEST_JSON_PATH=JEST_JSON_PATH,
            JSON_BEGIN=JSON_BEGIN,
            JSON_END=JSON_END,
        )

        body = ""
        if apply_patches:
            # ONE atomic `git apply` for both patches rather than two calls, and
            # test.patch is named before fix.patch so the order is fixed by the
            # command itself. No `||` fallback: these are the gold patches, and a
            # gold patch that will not apply is a fatal condition, not something
            # to work around. `set -e` aborts, the EXIT trap fences an empty
            # report, and the instance is rejected -- loudly -- by Report.check()
            # instead of quietly scoring against a half-patched tree.
            patches = " ".join(f"/home/{p}" for p in apply_patches)
            body += f"git apply --whitespace=nowarn {patches}\n\n"
            body += _render(INSTALL_IF_MANIFEST_PATCHED, PATCHES=patches)

        return f"{header}\n{body}\n{TEST_CMD}\n"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                _render(
                    PREPARE_SH,
                    REPO=self.pr.repo,
                    BASE_SHA=self.pr.base.sha,
                    EXPECTED_UNTRACKED=EXPECTED_UNTRACKED,
                    LEAP_CORE_FIX_VERSION=LEAP_CORE_FIX_VERSION,
                ),
            ),
            File(".", "run.sh", self._stage_script(apply_patches=[])),
            File(".", "test-run.sh", self._stage_script(apply_patches=["test.patch"])),
            File(
                ".",
                "fix-run.sh",
                self._stage_script(apply_patches=["test.patch", "fix.patch"]),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # `COPY <name> /home/<name>` -- the destination names the file, matching
        # the reference PR Dockerfile. `COPY x /home/` is equivalent to Docker but
        # not textually identical, and these artifacts are compared by eye.
        copy_command = "".join(
            f"COPY {file.name} /home/{file.name}\n" for file in self.files()
        )
        env_block = f"\n{self.global_env}\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""

        # Deliberately thin. No clone, no apt, no CA/proxy setup and NO history
        # scrub -- the base tag is pinned to this PR's base commit and has already
        # run the full scrub (gc, repack, all four asserts), so there is nothing
        # left to prune here.
        #
        # No ARG/ENV BASE_COMMIT and no WORKDIR either: prepare.sh carries the
        # literal base sha and cds to the repo itself, so both would be dead
        # weight, and the reference PR Dockerfile carries neither.
        return f"""FROM {name}:{tag}
{env_block}
{copy_command}RUN bash /home/prepare.sh
{clear_block}"""


@Instance.register("leapdao", "leap-node")
class LeapdaoLeapNode(Instance):
    """Harness instance for leapdao/leap-node -- Jest, JSON report."""

    _ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LeapNodeImageDefault(self.pr, self._config)

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

    # ---------------------------------------------------------------- parsing

    @staticmethod
    def _key(suite: str, ancestors: list[str], title: str) -> str:
        """`<file>::<full test name>` -- the `path::name` shape the reference
        instance report uses (`fiasco/tests/test_x.py::test_y`).

        Everything after `::` is Jest's own `fullName`: the describe titles and
        the test title joined by single spaces, exactly as Jest composes it.
        """
        return f"{suite}::{' '.join([*ancestors, title])}"

    @staticmethod
    def _suite_key(suite: str) -> str:
        """The entry standing for the suite FILE itself.

        Kept in the same `path::name` shape as a real test so every id in the
        report parses the same way. It is what gives a suite that cannot even be
        imported -- a `require()` of a module the fix patch creates -- a
        FAIL -> PASS transition instead of vanishing into NONE.
        """
        return f"{suite}::(test suite)"

    def _parse_json_report(
        self, log: str
    ) -> Optional[tuple[set[str], set[str], set[str]]]:
        """Parse the fenced `jest --json` report. None if it is not usable.

        Preferred over the human output because it carries `ancestorTitles`
        explicitly: no indentation heuristic can mis-nest a describe block, and
        a suite that failed to LOAD is reported as `status: failed` with an
        empty `assertionResults`, which is precisely the
        `saveSubmission.test.js` case this instance depends on.
        """
        start = log.rfind(JSON_BEGIN)
        end = log.rfind(JSON_END)
        if start == -1 or end == -1 or end < start:
            return None
        blob = log[start + len(JSON_BEGIN) : end].strip()
        if not blob:
            return None
        try:
            report = json.loads(blob)
        except ValueError:
            return None
        suites = report.get("testResults")
        if not isinstance(suites, list) or not suites:
            return None

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        prefix = f"/home/{self.pr.repo}/"

        for suite in suites:
            name = suite.get("name") or ""
            if name.startswith(prefix):
                name = name[len(prefix) :]
            if not name:
                continue

            # The suite FILE is recorded as an entry in its own right. That is
            # what gives a suite which cannot even be imported (a require() of a
            # module the fix patch creates) a FAIL -> PASS transition, instead of
            # vanishing into NONE because none of its test names were printed.
            if suite.get("status") == "failed":
                failed.add(self._suite_key(name))
            else:
                passed.add(self._suite_key(name))

            for assertion in suite.get("assertionResults") or []:
                title = assertion.get("title") or ""
                if not title:
                    continue
                key = self._key(
                    name, list(assertion.get("ancestorTitles") or []), title
                )
                status = assertion.get("status")
                if status == "passed":
                    passed.add(key)
                elif status == "failed":
                    failed.add(key)
                else:
                    # pending / todo / disabled / skipped
                    skipped.add(key)

        return passed, failed, skipped

    def _parse_verbose_text(self, log: str) -> tuple[set[str], set[str], set[str]]:
        """Fallback parse of `jest --verbose` output.

        Only reached when the JSON report is missing or truncated -- Jest killed
        by the harness timeout, or `--forceExit` firing before the file was
        flushed. The keys it emits are byte-identical in shape to the JSON
        path's (`<file> > <describe...> > <title>`), which is the whole point:
        if one stage falls back and another does not, the two must still compare.

        Jest indents each nesting level by two spaces starting at two, so a line
        at indent I sits at depth (I - 2) / 2 and its ancestors are the describe
        titles recorded at the shallower depths.
        """
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        re_suite = re.compile(r"^(PASS|FAIL)\s+(\S+?)(?:\s+\(.*\))?\s*$")
        re_test = re.compile(
            r"^(?P<indent> *)(?P<mark>[✓✔✕×✗✘✖"
            r"○✎])\s+(?P<name>.*?)"
            r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$"
        )
        re_describe = re.compile(r"^(?P<indent> +)(?P<title>\S.*?)\s*$")
        # Jest's failure detail and summary lines also start at an indent; none
        # of them is a describe block.
        noise_prefixes = ("●", "at ", "expect(", "Expected", "Received", "|")
        # Jest prefixes a pending/todo test's NAME with the word `skipped ` /
        # `todo ` in the text reporter, but `--json` reports the bare title. The
        # prefix has to come off or the same test hashes differently depending
        # on which parse path a stage took, and the two stages stop comparing.
        re_skip_word = re.compile(r"^(?:skipped|todo)\s+")

        suite = ""
        stack: list[str] = []

        for raw in log.splitlines():
            line = raw.rstrip()

            m = re_suite.match(line.strip())
            if m and line[:1] in "PF":
                suite = m.group(2)
                stack = []
                if m.group(1) == "FAIL":
                    failed.add(self._suite_key(suite))
                else:
                    passed.add(self._suite_key(suite))
                continue

            if not suite:
                continue

            m = re_test.match(line)
            if m:
                depth = max(0, (len(m.group("indent")) - 2) // 2)
                key = self._key(suite, stack[:depth], m.group("name").strip())
                mark = m.group("mark")
                if mark in "✓✔":
                    passed.add(key)
                elif mark in "○✎":
                    name = re_skip_word.sub("", m.group("name").strip())
                    skipped.add(self._key(suite, stack[:depth], name))
                else:
                    failed.add(key)
                continue

            m = re_describe.match(line)
            if m:
                title = m.group("title")
                if title.startswith(noise_prefixes):
                    continue
                depth = max(0, (len(m.group("indent")) - 2) // 2)
                stack = stack[:depth]
                stack.append(title)

        return passed, failed, skipped

    def parse_log(self, test_log: str) -> TestResult:
        log = self._ANSI.sub("", test_log)

        parsed = self._parse_json_report(log)
        if parsed is None:
            parsed = self._parse_verbose_text(log)
        passed_tests, failed_tests, skipped_tests = parsed

        # A key may live in only one bucket -- TestResult.__post_init__ rejects
        # overlapping sets outright. Resolved pessimistically: a failure
        # anywhere beats a pass elsewhere, so a duplicated title can never look
        # green by accident.
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
