from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Jest in the #1174-#1983 era (mid-to-late 2016) is a Lerna monorepo whose
# packages are linked by `lerna bootstrap` (invoked from the repo's own
# `scripts/postinstall.js`). npm's lifecycle postinstall does not reliably run
# here, so bootstrap is called explicitly -- without it every entry point dies
# with "Cannot find module 'jest-util'" and zero tests are collected.
#
# This era straddles jestjs/jest#1361 ("single jest run for all packages",
# commit 6c76202, 2016-08-04), which moved the test setup:
#
#   before #1361  no root `jest` config; every package carries its own
#                 {"rootDir": "./build"} block and is tested individually
#                 (the repo's scripts/test.js runs `npm test` per package).
#                 A single root jest run here collects BOTH the src/ and
#                 build/ copy of each test file -- duplicate suites with
#                 contradictory results -- and chokes on untranspiled Flow
#                 syntax in src/.
#
#   from #1361    root `jest` block with scriptPreprocessor (babel-jest) and
#                 testPathIgnorePatterns excluding packages/.*/build. One run
#                 from the repo root is correct; the per-package loop breaks
#                 because packages no longer carry their own config.
#
# PR 1361 is the boundary: PRs below it use the per-package loop, PRs at or
# above it use the single root run.
_SINGLE_RUN_MIN_PR = 1361

_BOOTSTRAP = "./node_modules/.bin/lerna bootstrap\n"
_BUILD = "node ./scripts/build.js\n"

# Pre-#1361: run each package's suite from inside the package so its own
# `rootDir: ./build` applies. Each block is prefixed with a marker line so
# parse_log can qualify test names by package -- several packages define
# identically-named tests, and unqualified names would collide.
_TEST_CMD_PER_PACKAGE = (
    "for pkg_dir in packages/*/; do\n"
    '  pkg_name=$(basename "$pkg_dir")\n'
    '  [ -f "$pkg_dir/package.json" ] || continue\n'
    '  echo "===PACKAGE:${pkg_name}==="\n'
    '  ( cd "$pkg_dir" && ../../packages/jest-cli/bin/jest.js --verbose 2>&1 ) || true\n'
    "done\n"
)

# From #1361: one run from the repo root, using the root jest config.
#
# No `|| true` here: a non-zero exit from jest itself is expected (the test
# stage is supposed to have failures) and the harness reads results from
# parse_log, not the exit code. But swallowing failures would also hide the
# case where jest never starts -- parse_log would then see an empty log and
# return 0/0/0, which Report.check() rejects as an invalid instance rather
# than surfacing as a build error. `|| true` belongs only in prepare.sh.
_TEST_CMD_ROOT = "node ./packages/jest-cli/bin/jest.js --verbose 2>&1\n"


def _test_cmd(pr: PullRequest) -> str:
    return _TEST_CMD_ROOT if pr.number >= _SINGLE_RUN_MIN_PR else _TEST_CMD_PER_PACKAGE


class JestImageBase(Image):
    """Heavy environment image: node:10 toolchain + the repo clone."""

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
        return "node:10-buster"

    def image_tag(self) -> str:
        # ONE shared base for every PR in this era. Images are deduplicated on
        # image_full_name() (Image.__hash__/__eq__), so a constant tag collapses
        # all PRs onto a single build of the heavy apt+clone layer instead of
        # one per PR.
        #
        # The base is therefore pinned to whichever PR's BASE_COMMIT won the
        # dedup race, and the enhancer's history scrub prunes every other commit
        # (all refs and remotes are deleted there). That is safe because each PR
        # layer's prepare.sh fetches its own BASE_COMMIT back from origin before
        # checking it out -- see prepare.sh below.
        return "base"

    def workdir(self) -> str:
        return "base"

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

        # buster is EOL: its apt endpoints moved to archive.debian.org, and
        # buster-updates no longer exists at all. python2.7/make/g++ are needed
        # for this era's node-gyp native builds.
        #
        # NOTE: this deliberately ends at the clone. DockerfileEnhancer's
        # _standardize_repo_fetch() REPLACES the clone line with the full
        # parameterized block -- clone "${REPO_URL}", WORKDIR, git reset,
        # git checkout ${BASE_COMMIT}, the history-scrub/integrity asserts, and
        # CMD ["/bin/bash"]. Emitting our own WORKDIR/checkout/CMD here would
        # duplicate all of it AFTER the scrub, re-dirtying the tree the scrub
        # just verified and leaving two CMD lines.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN sed -i "s/deb.debian.org/archive.debian.org/g" /etc/apt/sources.list \\
 && sed -i "s/security.debian.org/archive.debian.org/g" /etc/apt/sources.list \\
 && sed -i "/buster-updates/d" /etc/apt/sources.list \\
 && apt-get update \\
 && apt-get install -y ca-certificates git python2.7 make g++ \\
 && rm -rf /var/lib/apt/lists/*

{self.clear_env}

{code}
"""


class JestImageDefault(Image):
    """Thin PR layer: stages the patches + run scripts on top of the base image."""

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
        test_cmd = _test_cmd(self.pr)
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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

# The base image is shared by every PR in this era, so it is pinned to a single
# BASE_COMMIT and the enhancer's history scrub pruned every other commit (all
# refs and remotes are deleted there). Fetch this PR's own base commit back
# before checking it out. No-op when the commit is already present.
if ! git cat-file -e {sha}^{{commit}} 2>/dev/null; then
    git fetch --no-tags --depth 1 https://github.com/{org}/{repo}.git {sha}
    git checkout --detach FETCH_HEAD
else
    git checkout --detach {sha}
fi
bash /home/check_git_changes.sh

# Warm the install/link/build caches so the graded runs do not pay for them.
# `|| true` belongs here and ONLY here -- a cold-cache failure is recoverable
# at run time, but a swallowed failure in a graded run would be silent.
npm install || true
./node_modules/.bin/lerna bootstrap || true
node ./scripts/build.js || true
""".format(org=self.pr.org, repo=repo, sha=sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{bootstrap}{build}{test_cmd}""".format(
                    repo=repo,
                    bootstrap=_BOOTSTRAP,
                    build=_BUILD,
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
# lerna bootstrap rewrites every packages/*/package.json, so the tree is dirty
# from the warm-cache step in prepare.sh. git apply refuses to patch a modified
# file, which silently produced a no-op fix stage (identical test/fix results,
# zero f2p) before this reset was added.
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{bootstrap}{build}{test_cmd}""".format(
                    repo=repo,
                    bootstrap=_BOOTSTRAP,
                    build=_BUILD,
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
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{bootstrap}{build}{test_cmd}""".format(
                    repo=repo,
                    bootstrap=_BOOTSTRAP,
                    build=_BUILD,
                    test_cmd=test_cmd,
                ),
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
{self.clear_env}

RUN bash /home/prepare.sh
"""


@Instance.register("jestjs", "jest_1983_to_1174")
class Jest_1983_to_1174(Instance):
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
_PACKAGE_RE = re.compile(r"^===PACKAGE:(.+)===$")

# Test-line shape, shared by the pass/fail/skip patterns.
#
# Three details here are load-bearing for cross-stage name stability, and each
# was observed differing between the test and fix stages of the SAME PR:
#
#   indent      the reporter indents by describe-nesting, and a patch that adds
#               a describe shifts EVERY line beneath it. Measured on pr-1234:
#               run stage {4:101, 6:188, 8:8} vs fix stage {4:1, 6:101, 8:188,
#               10:8} -- the whole tree moved 2 columns right. Indentation
#               therefore carries no information about whether a line is a real
#               result, and must not be used to filter one out: an earlier
#               1-6 space cap silently dropped the 8 deepest tests of the fix
#               stage, which had passed in both stages, making them look like
#               they vanished. Nested integration-test transcripts are excluded
#               by the explicit OUTPUT: guard in the parser instead.
#
#   keyword     "✓it name" (glued, #1174-era reporter) must lose the keyword,
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
_NESTED_END_RE = re.compile(r"^\s*(Ran all tests|Test Summary)\b")

# Suite headers ("PASS <path>") sit at column 0-1. Jest's own integration_tests
# spawn a nested jest and echo its entire output inside a failure block, indented
# well past that -- an unanchored `^\s*` would count those inner lines as real
# results, inventing passes that never ran.
_SUITE_PASS_RE = re.compile(r"^ {0,2}PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$")
_SUITE_FAIL_RE = re.compile(r"^ {0,2}FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$")
_TEST_PASS_RE = re.compile(_TEST_LINE.format(marks="✓✔"))
_TEST_FAIL_RE = re.compile(_TEST_LINE.format(marks="×✗✕"))
_TEST_SKIP_RE = re.compile(_TEST_LINE.format(marks="○●"))


def _parse_jest_log(test_log: str) -> TestResult:
    # Strip ANSI first -- the reporter colorizes ✓/✕ and suite headers, and the
    # patterns below will not match otherwise.
    log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    package = ""
    in_nested = False

    def qualify(name: str) -> str:
        name = name.strip()
        # Package prefix keeps names unique across the per-package loop: several
        # packages define identically-named tests (e.g. "it works").
        return f"{package}::{name}" if package else name

    for line in log.splitlines():
        line = line.rstrip()

        m = _PACKAGE_RE.match(line.strip())
        if m:
            package = m.group(1).strip()
            in_nested = False
            continue

        # Suppress the echoed transcript of a nested jest run (see _NESTED_*).
        if _NESTED_START_RE.match(line):
            in_nested = True
            continue
        if in_nested:
            if _NESTED_END_RE.match(line):
                in_nested = False
            continue

        m = _SUITE_FAIL_RE.match(line)
        if m:
            failed_tests.add(qualify(m.group(1)))
            continue

        m = _SUITE_PASS_RE.match(line)
        if m:
            passed_tests.add(qualify(m.group(1)))
            continue

        m = _TEST_SKIP_RE.match(line)
        if m:
            skipped_tests.add(qualify(m.group(1)))
            continue

        m = _TEST_FAIL_RE.match(line)
        if m:
            failed_tests.add(qualify(m.group(1)))
            continue

        m = _TEST_PASS_RE.match(line)
        if m:
            passed_tests.add(qualify(m.group(1)))
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
