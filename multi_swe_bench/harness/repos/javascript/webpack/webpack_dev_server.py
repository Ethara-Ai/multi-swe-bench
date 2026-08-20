from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Node 14 is the newest version the v4-era CI matrix tests (10.x/12.x/14.x) and
# jest 26 + the 2020 package-lock resolve cleanly on it. bullseye rather than the
# default buster: buster left deb.debian.org for archive.debian.org, which is why
# the sibling webpack-cli config carries a sources.list rewrite. bullseye needs no
# such patching.
NODE_IMAGE = "node:14-bullseye"

# puppeteer@5 downloads its own Chromium at install time. Skipping that keeps the
# image ~300 MB smaller and the build far faster. The browser-driven suites under
# test/client and test/e2e then fail at runtime - but they fail identically in the
# run, test and fix stages, so they are consistently-failing noise and never get
# classified as fail-to-pass. Without this flag the download failure would abort
# `npm install` outright and no test would run at all.
NPM_ENV = "PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true PUPPETEER_SKIP_DOWNLOAD=true"

# Retried once: a 2020 lockfile points at a lot of old tarballs and a single
# registry hiccup should not sink the whole stage.
NPM_INSTALL = (
    f"{NPM_ENV} npm install --no-audit --no-fund --loglevel=error "
    f"|| {NPM_ENV} npm install --no-audit --no-fund --loglevel=error"
)

# Run explicitly rather than relying on the package.json `prepare` hook.
#
# `lib/utils/addEntries.js` does `require.resolve('../../client/default/')`, and
# client/ is generated - it is gitignored, built by babel+webpack from
# client-src/. `prepare` is supposed to build it on install but did not fire here,
# so client/ was absent and EVERY suite that touches addEntries died with
# "Cannot find module '../../client/default/'" - 1176 occurrences in the baseline
# stage alone. That is what produced 41 failing suites out of 67 and, worse, it
# masked the fail-to-pass signal: ModuleFederation.test.js failed in the fix stage
# for this reason instead of passing.
#
# Placed after every install because `build:client` starts with `rimraf ./client/*`
# and a re-install can re-trigger the hook; doing it explicitly makes the state
# deterministic no matter what npm decides to run on its own.
NPM_BUILD = "npm run build:client"

# The six suites under test/e2e/ drive a real Chromium through puppeteer
# (they call runBrowser()). Since NPM_ENV skips the Chromium download they can
# never pass, and - critically - they do not fail fast: puppeteer blocks until
# jest's 120 s per-test timeout fires. Measured on the 2026-08-19 run:
# TransportMode.test.js 721 s, Progress.test.js 484 s, 8 timeouts and climbing,
# with the container at 12% CPU just waiting. Three stages of that is hours.
#
# Note this only became visible after client/ started being built: before that
# these suites died instantly on the missing '../../client/default/', which hid
# the browser dependency entirely.
#
# Excluding them is safe for this PR - it touches lib/utils entry handling, and
# its fail-to-pass test is test/integration/ModuleFederation.test.js, which is
# plain supertest with no browser. The pattern is applied identically in all
# three stages, so the compared test set stays consistent.
#
# /node_modules/ must be repeated: passing --testPathIgnorePatterns REPLACES
# jest's default of ["/node_modules/"] rather than adding to it.
# Three more exclusions beyond /test/e2e/, added 2026-08-20 after three runs.
#
# proxy-option, static-directory-option and static-publicPath-option each start
# an express server plus a webpack compiler in `beforeAll` and intermittently
# exceed jest's 120 s hook timeout. When that happens jest prints no per-case
# lines, so parse_log's suite-level fallback collapses the whole suite into one
# bare-filename entry and every case in it becomes NONE.
#
# The victim ROTATES with load, which is why this is an exclusion and not a
# tuning problem:
#   suite                      11w runA    11w runB    4w runC
#   proxy-option               collapsed   collapsed   OK
#   static-directory-option    OK          collapsed   collapsed
#   static-publicPath-option   OK          OK          collapsed
#
# Raising the limit is impossible from here: the repo's setupTest.js calls
# jest.setTimeout(120000) from setupFilesAfterEnv, which runs after the CLI
# options and overrides --testTimeout.
#
# The real damage was misclassification, not lost coverage. static-directory-
# option timed out in run+test but ran in fix, so its 16 tests looked new after
# the fix patch; that file is not in test_patch_files, so they were filed as
# fix_patch_authored_candidates - a cheating signal - pushing fixed_tests 29->45.
#
# Costs ~54 p2p tests. The graded signal is untouched: f2p (17) and n2p (2) were
# byte-identical sets across all three runs.
JEST_IGNORE = (
    "--testPathIgnorePatterns "
    "'/node_modules/"
    "|/test/e2e/"
    "|/test/server/proxy-option"
    "|/test/server/static-directory-option"
    "|/test/server/static-publicPath-option'"
)

# `npx jest` rather than `npm test`: the package.json `test` script is
# `npm run test:coverage`, and its `pretest` hook runs the full lint suite first.
# Both would bury the test output the parser needs.
#
# Parallel workers are safe here - test/ports-map.js hands every suite its own
# port, which is exactly what it exists for - so no --runInBand.
# --forceExit matches the repo's own `test:only` script; several suites leave
# server handles open and jest would otherwise hang.
# `|| true` keeps a non-zero jest exit (any failing test) from aborting the stage:
# failures are the signal here, not an error.
# --verbose is REQUIRED, not cosmetic. Without it jest's default reporter prints
# per-test lines only for FAILING suites; a passing suite prints one
# "PASS <file>" line for the whole file. Every passing test then collapses into
# its filename and parse_log can only report file-level names.
# --maxWorkers=4 rather than jest's default of (cores - 1) = 11 here.
# proxy-option.test.js and static-directory-option.test.js pass at baseline
# (28.7 s against a 120 s budget) but exceed the hook timeout in the patched
# stages, where ModuleFederation.test.js adds load. Jest then emits no
# per-case lines and parse_log's suite-level fallback collapses 19 and 16
# cases into one bare-filename entry each - p2p swung 397 -> 381 between two
# runs while f2p stayed at 17.
#
# Raising the limit is NOT an option: the repo's setupTest.js calls
# jest.setTimeout(120000) from setupFilesAfterEnv, which runs after the CLI
# options and overrides --testTimeout. Changing it would mean patching a repo
# file, so the graded run would stop testing the repo as shipped.
#
# 12 CPUs / 7 GB in the container means 11 workers get ~640 MB each, and every
# suite starts a webpack compiler plus an express server. 4 workers cuts
# concurrent load ~2.75x and gives each ~1.75 GB. Drop to 2 if 4 still flakes.
JEST = f"npx jest --ci --forceExit --verbose --reporters=default --maxWorkers=4 {JEST_IGNORE} || true"


class webpackDevServerImageBase(Image):
    """Per-PR base image: toolchain + the repo pinned at this PR's base commit.

    `dependency()` returns a string, so DockerfileEnhancer runs on this file and
    `_inject_final_sanitize` appends Image._HARDENING_BLOCK (history scrub,
    submodule scrub, all four integrity asserts) just before the CMD. That is
    also the only place `REPO_URL`/`BASE_COMMIT` build args are supplied
    (build_dataset.py:625-629). A base that skips the clone therefore skips the
    scrub too, which is exactly how this repo shipped an image containing the
    full upstream history plus a live origin.

    Because the tree is pinned here, the tag must be per-PR. A shared `base`
    tag would be stuck on whichever PR built it first and every other base
    commit would die with `fatal: unable to read tree`.
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
        return NODE_IMAGE

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

        return f"""FROM {image_name}
# DEBIAN_FRONTEND and TZ are deliberately NOT set here. DockerfileEnhancer
# already emits both (plus LANG) in its own ENV block above, so repeating them
# only produced a second block that silently overrode the first.
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_SKIP_DOWNLOAD=true

WORKDIR /home/

# build-essential + python3 so node-gyp can build the native addons in the
# 2020 lockfile; git for the per-PR clone. ca-certificates already ships in
# node:14-bullseye and this is a no-op at build time, but naming it keeps the
# TLS dependency declared rather than inherited from an upstream image.
RUN apt-get update && apt-get install -y --no-install-recommends \\
      build-essential python3 git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# Spelled "${{REPO_URL}}" deliberately. _standardize_repo_fetch rewrites a
# hardcoded-URL clone into its own canonical block, which has NO fetch fallback -
# and this base commit needs one (see below). That regex carries a negative
# lookahead for "${{REPO_URL}}", so writing it this way keeps the clone under our
# control while _inject_final_sanitize still appends the scrub before the CMD.
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

# PR 2839's base commit sits on the long-deleted `v4` branch; upstream advertises
# only refs/heads/main, so a plain clone never brings the object down and the
# checkout below would die with "reference is not a tree". Fetching the SHA
# directly is the only way to get it. A no-op when it is already reachable.
RUN git cat-file -e ${{BASE_COMMIT}} 2>/dev/null \\
    || git fetch --no-tags "${{REPO_URL}}" ${{BASE_COMMIT}}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

CMD ["/bin/bash"]
"""


class webpackDevServerImageDefault(Image):
    """Thin PR layer: stages the patches and run-scripts on top of the base.

    Deliberately does NOT clone, check out or scrub - the base image owns all
    three (and is the only image the harness hands REPO_URL/BASE_COMMIT to).
    Its whole job is to COPY the seven artifacts and run prepare.sh once.
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

    def dependency(self) -> Image | None:
        # An Image, not a string, so the enhancer leaves the Dockerfile below
        # verbatim and the clone survives.
        return webpackDevServerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
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
cd /home/{pr.repo}
# reset + assert on BOTH sides of the checkout. check_git_changes.sh was already
# shipped in the image but nothing ever called it, so nothing verified the tree
# was pristine before the graded runs. The concrete risk that guards against:
# npm install can rewrite package-lock.json, and fix.patch touches that file, so
# a dirty tree makes `git apply` fail and kills the fix stage under `set -e`.
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
{npm_install}
{npm_build}
""".format(pr=self.pr, npm_install=NPM_INSTALL, npm_build=NPM_BUILD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{jest}
""".format(pr=self.pr, jest=JEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
# Deliberately re-installed AFTER the patch: the fix patch is what adds
# require-from-string to package.json, so at this stage the new integration test
# cannot resolve it and the whole file fails to load. That failure is the
# fail-to-pass signal, not a setup bug.
{npm_install}
{npm_build}
{jest}
""".format(pr=self.pr, npm_install=NPM_INSTALL, npm_build=NPM_BUILD, jest=JEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
# The fix patch touches package.json/package-lock.json (it adds
# require-from-string), so deps must be re-resolved before jest runs.
{npm_install}
{npm_build}
{jest}
""".format(pr=self.pr, npm_install=NPM_INSTALL, npm_build=NPM_BUILD, jest=JEST),
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

WORKDIR /home/
{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("webpack", "webpack-dev-server")
class webpackDevServerInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return webpackDevServerImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        """Parse `jest --verbose` output into pytest-style `file::test name` ids.

        jest --verbose prints a `PASS|FAIL <file>` header, then one line per
        case: `✓ title (5 ms)` / `✕ title` / `○ skipped title`.
        Leaf titles are NOT unique across files ("should handle the option"
        recurs in several suites here), so each is qualified with the file that
        produced it. That yields the same shape pytest emits
        (`path/to/file.js::test name`) and matches the convention the other jest
        configs in this repo already use.

        Suite-level fallback, deliberate: when a suite fails to LOAD, jest emits
        the `FAIL <file>` header and no case lines at all. That is exactly what
        happens to ModuleFederation.test.js in the test stage, where
        require-from-string is not installed until the fix patch adds it.
        Recording only case names would leave that file absent from the test
        stage, so it could never pair with the fix stage and the fail-to-pass
        link would be lost. So a header that produces zero case lines is
        recorded under its bare filename instead.
        """
        ansi = re.compile(r"\[[0-9;]*m")
        re_file = re.compile(r"^(PASS|FAIL)\s+(\S+\.(?:js|jsx|ts|tsx|mjs|cjs))")
        dur = r"(?:\s*\(\d+(?:\.\d+)?\s*m?s\))?"
        re_pass = re.compile(r"^[✓√✅]\s+(.+?)" + dur + r"$")
        re_fail = re.compile(r"^[✕✗×✘]\s+(.+?)" + dur + r"$")
        re_skip = re.compile(
            r"^[○✎]\s+(?:skipped\s+)?(.+?)" + dur + r"$"
        )

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        cur_file = ""
        cur_status = ""
        cur_cases = 0

        def flush() -> None:
            # Header with no case lines => the suite never ran (load error).
            if cur_file and cur_cases == 0:
                if cur_status == "FAIL":
                    failed_tests.add(cur_file)
                else:
                    passed_tests.add(cur_file)

        def qualify(name: str) -> str:
            name = name.strip()
            return f"{cur_file}::{name}" if cur_file else name

        for raw in test_log.splitlines():
            line = ansi.sub("", raw).strip()
            if not line:
                continue

            m = re_file.match(line)
            if m:
                flush()
                cur_status, cur_file = m.group(1), m.group(2)
                cur_cases = 0
                continue

            # Failures first: a `✕` line must never be mistaken for a pass.
            m = re_fail.match(line)
            if m:
                failed_tests.add(qualify(m.group(1)))
                cur_cases += 1
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(qualify(m.group(1)))
                cur_cases += 1
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(qualify(m.group(1)))
                cur_cases += 1

        flush()

        # Keep the buckets disjoint: a name that failed anywhere is a failure.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
