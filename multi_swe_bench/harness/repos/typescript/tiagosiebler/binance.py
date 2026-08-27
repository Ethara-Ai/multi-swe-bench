"""tiagosiebler/binance harness config.

Toolchain: Node.js 16 (the repo's own .nvmrc pins v12.18.1, which is older than
any image that still ships a usable npm; 16 is the closest LTS that runs this
2022 dependency set unchanged, and its npm 8 reads the committed
`lockfileVersion: 2` package-lock.json natively), with Jest 27 running
TypeScript through the `ts-jest` preset declared in jest.config.js. No build
step is needed to test -- ts-jest compiles the sources in-process -- so the run
scripts never invoke `tsc`.

Image layout -- the `Borewit/music_metadata.py` shape
----------------------------------------------------
  base-pr-<N>   The heavy, self-contained environment. `dependency()` returns a
                STRING, so DockerfileEnhancer.enhance() engages and prepends the
                syntax directive, the TARGETARCH/REPO_URL/BASE_COMMIT ARGs, the
                proxy ARGs, the ENV block, the OCI labels and the CA-cert
                symlink farm -- and then `_standardize_repo_fetch()` rewrites the
                clone line into the canonical clone / WORKDIR / reset / checkout
                / `Image._HARDENING_BLOCK` / CMD tail.

                The tag is per-PR (`base-pr-<N>`), NOT a shared `base`. That is
                forced by the clone: a string-dependency image that clones gets
                pinned to one ${BASE_COMMIT} and history-scrubbed, so a SHARED
                base would be fixed to whichever PR built it first and every
                other PR in the range would die on `fatal: unable to read tree`.
                One PR, one base, one scrub. This dataset is a single PR, so it
                costs nothing: two images either way.

                Because the enhancer owns the whole tail, this Dockerfile body
                ends AT the clone line and declares no CMD of its own -- the
                replacement supplies one, and a second CMD here would be dead
                weight the reviewer has to reason about.

  pr-<N>        Deliberately thin. `dependency()` returns an Image, so enhance()
                returns this Dockerfile verbatim. It stages the two patches, the
                integrity guard and the four scripts, then runs `prepare.sh`
                once. No clone, no checkout-from-scratch, no apt, no scrub --
                all of that was earned in `base-pr-<N>` and must not be redone
                here.

Why prepare.sh re-pins a tree the base already pinned
-----------------------------------------------------
The base's `ARG BASE_COMMIT` is a build ARG, not an ENV, so it does NOT survive
into the `pr-<N>` build. prepare.sh therefore carries the SHA literally, via
`self.pr.base.sha` -- rendered from the PullRequest object, never typed by hand.
It resets, asserts a clean tree, checks out that SHA, asserts again, and only
then warms the npm cache. The base's scrub leaves HEAD detached at exactly this
commit with every ref deleted, so the checkout resolves against the one object
that survived.

Test reporting
--------------
jest.config.js already sets `verbose: true`, so every leaf test prints its own
marker line; `--verbose --ci` is passed anyway so the reporter cannot be flipped
by a local config change. Leaf names in this suite are NOT unique across files
("should keep alive user data key" appears under the spot, margin and
isolated-margin blocks, and again under futures), so parse_log qualifies every
test with the `PASS`/`FAIL` suite path that introduced it. Suite-level lines are
recorded as well: when ts-jest fails to compile a file, Jest prints only
`FAIL <path>` and no leaf lines, and that line is then the only evidence the
file ran.

`--forceExit` is set because test/websocket-client.test.ts opens sockets that
Jest's default teardown waits on; without it a run can sit at "Jest did not exit
one second after..." until the harness timeout instead of reporting.

NOTE on this dataset: test/ here is an INTEGRATION suite that talks to the live
Binance REST API and reads credentials from API_KEY_COM / API_SECRET_COM. Inside
a network-isolated container every case -- including the PR's own
`submitMultipleOrders()` case -- fails on connection or auth rather than on the
code under evaluation. The config below is correct for the repo; whether PR 214
can yield a non-empty f2p set is a property of the dataset, not of this file.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# One place for the test command so run.sh / test-run.sh / fix-run.sh cannot
# drift apart -- the f2p signal is only meaningful if the ONLY thing differing
# between the three graded runs is which patches were applied.
#
# Deliberately NOT suffixed with `|| true`: under `set -eo pipefail` that would
# neutralise the whole guard, and a jest that fails to START (missing binary,
# ts-jest config error) would be indistinguishable from a jest that ran and
# reported failures. parse_log would then see an empty log, return 0/0/0, and
# Report.check() rule 1 (`fix_patch_result.all_count > 0`) would reject the
# instance with no indication of why.
#
# --testTimeout raises jest's 2-tier default of 5000ms. Measured across a full
# three-stage run: `test/spot/public.test.ts > getExchangeInfo()` came in at
# 4508ms in one stage and hit the ceiling at exactly 5002ms in the other two.
# That is a live call to the Binance public exchangeInfo endpoint -- the largest
# payload the public API serves -- sitting right on the line, so it rotates
# between runs rather than being genuinely broken.
#
# Left at the default it produced a PASS(test) -> FAIL(fix) transition, which
# Report.check() rule 2 rejects outright, invalidating the whole instance and
# discarding 38 p2p and 3 f2p that were otherwise sound.
#
# This cannot mask a real failure: every other failing test in this suite throws
# an instant auth or connection error (no credentials in the container),
# nowhere near the
# limit, and a failing assertion still fails however long it is given.
TEST_CMD = "npx jest --verbose --ci --forceExit --testTimeout=30000"

# Prelude shared by all three run scripts. `pipefail` matters because npx pipes
# through a shim; CI=true is exported for the suite's own benefit (jest's own
# --ci flag governs jest, not the code under test).
RUN_SCRIPT_PRELUDE = """#!/bin/bash
set -eo pipefail
export CI=true
"""

# Integrity guard called twice by prepare.sh. Kept byte-identical to the
# reference shape: assert we are in a git work tree at all, then assert the
# tree is pristine. A dirty tree at either point means the graded runs would
# start from something other than the base commit.
CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: not inside a git work tree" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "check_git_changes: working tree is dirty:" >&2
  git status --porcelain >&2
  exit 1
fi

echo "check_git_changes: clean"
"""


class BinanceImageBase(Image):
    """The heavy per-PR environment image, tagged `base-pr-<N>`.

    `dependency()` returns a str, so the enhancer injects the infra block and
    rewrites the clone line into clone/WORKDIR/reset/checkout/scrub/CMD. See the
    module docstring for why the tag is per-PR rather than shared.
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
        return "node:16-bullseye"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        # The base stages nothing: patches and scripts belong to the thin PR
        # layer, which is the image that actually applies them.
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        # No `# syntax` directive and no proxy/ENV/LABEL/CA lines here -- the
        # enhancer owns all of that and injects it directly after FROM, i.e.
        # BEFORE the apt install below, which is what makes the first network
        # call trust the inspecting proxy.
        #
        # DEBIAN_FRONTEND / LANG / TZ are likewise NOT re-declared: the
        # enhancer's ENV block already sets them, and a second TZ here would
        # silently override the pipeline's value.
        #
        # node:16-bullseye already ships git, node and npm, but `git` and
        # `ca-certificates` are named explicitly anyway -- they are the two
        # packages the clone and every TLS call depend on, and relying on a
        # base image to keep shipping them is the kind of assumption that
        # breaks quietly on a base-image bump.
        #
        # This body ENDS at the clone line on purpose: DockerfileEnhancer
        # ._standardize_repo_fetch() replaces that single line with the
        # canonical clone / WORKDIR /home/<repo> / git reset --hard /
        # git checkout ${BASE_COMMIT} / Image._HARDENING_BLOCK / CMD tail.
        # Writing our own CMD after it would leave two CMD instructions.
        return f"""FROM {image_name}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
"""


class BinanceImageDefault(Image):
    """The thin PR layer, tagged `pr-<N>`, built on `base-pr-<N>`."""

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
        return BinanceImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                # Runs once, at PR-image build time.
                #
                # The SHA is inlined from `self.pr.base.sha` because the base's
                # `ARG BASE_COMMIT` is a build ARG and does not survive into
                # this build -- `${{BASE_COMMIT}}` would expand to the empty
                # string here and `git checkout` would silently become a no-op
                # on whatever HEAD happened to be.
                #
                # `set -e` (not `-eo pipefail`) is correct for prepare.sh: the
                # one command allowed to fail already carries `|| true`.
                #
                # That `|| true` on the install is REQUIRED -- an optional or
                # native dependency that will not build on arm64 is common and
                # non-fatal for this suite.
                #
                # No build step: jest.config.js uses the ts-jest preset, so the
                # sources compile in-process at test time and `tsc` output under
                # lib/ is never read by the tests.
                """#!/bin/bash
set -e
cd /home/{pr.repo}

git reset --hard
bash /home/check_git_changes.sh

git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm ci || npm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                # Baseline: no patch applied.
                """{prelude}cd /home/{pr.repo}
{test_cmd}
""".format(prelude=RUN_SCRIPT_PRELUDE, pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                # test.patch ONLY -- the new tests without the fix, which is the
                # run that must FAIL for the instance to carry signal.
                """{prelude}cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(prelude=RUN_SCRIPT_PRELUDE, pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                # test.patch BEFORE fix.patch, in one `git apply` so the pair is
                # applied atomically. fix.patch touches package.json, but only
                # for a version bump, so no reinstall is needed between stages.
                """{prelude}cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(prelude=RUN_SCRIPT_PRELUDE, pr=self.pr, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # One COPY per file, matching the reference shape, so a reviewer can see
        # at a glance that exactly the seven expected artifacts are staged and
        # nothing else.
        copy_lines = "\n".join(
            f"COPY {file.name} /home/" for file in self.files()
        )

        # Nothing from the base is re-done or undone here: no FROM on a language
        # runtime, no clone, no apt, no scrub, and no ENV that would disturb the
        # inherited proxy/CA trust. global_env/clear_env render empty unless the
        # pipeline was invoked with --global_env.
        return f"""FROM {name}:{tag}
{self.global_env}

{copy_lines}

RUN bash /home/prepare.sh
{self.clear_env}
"""


@Instance.register("tiagosiebler", "binance")
class Binance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BinanceImageDefault(self.pr, self._config)

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
        """Parse Jest 27 verbose output into pass/fail/skip sets.

        Jest verbose output looks like::

            PASS test/spot/public.test.ts
              Public Spot REST API Endpoints
                Misc Endpoints
                  ✓ getServerTime() (321 ms)
                  ✕ getExchangeInfo() (12 ms)
                  ○ skipped getUniversalTransferHistory()

            FAIL test/futures-usdm/private.test.ts

        Leaf names repeat across files in this suite, so each test is recorded
        as ``<suite path> > <leaf name>`` using the most recent PASS/FAIL line.
        The suite paths themselves are recorded too: a ts-jest compile error
        emits ``FAIL <path>`` with no leaf lines, and that is then the only
        evidence the file ran at all.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI before matching so the status word sits at a predictable
        # offset. The terminator class is the FULL `[a-zA-Z]`, not just `m`:
        # besides SGR colour codes, jest's progress output emits erase-line
        # (\x1b[2K), cursor-column (\x1b[1G) and cursor-hide (\x1b[?25l)
        # sequences, and an SGR-only pattern would leave those embedded in the
        # line and break every match on it.
        ansi_re = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

        # Leading `\s*` is load-bearing. jest builds the suite badge as
        # `chalk.reset.inverse.bold.green(" PASS ")` whenever chalk reports
        # colour support, and the PADDING SPACES inside the badge survive ANSI
        # stripping -- the line arrives as " PASS  test/spot/public.test.ts".
        # A `^PASS` anchor silently stops matching in that case, `current_suite`
        # goes stale, and every subsequent leaf is filed under the WRONG suite,
        # which surfaces downstream as Report.check() rule 4 anomalies rather
        # than as a parse error. Docker exec is non-TTY so chalk is normally
        # off, but this must not depend on that.
        suite_pass_re = re.compile(r"^\s*PASS\s+(\S+)(?:\s+\(.*\))?$")
        suite_fail_re = re.compile(r"^\s*FAIL\s+(\S+)(?:\s+\(.*\))?$")

        time_suffix = r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?"
        test_pass_re = re.compile(r"^\s+[✓✔]\s+(.+?)" + time_suffix + r"$")
        test_fail_re = re.compile(r"^\s+[✕✗×]\s+(.+?)" + time_suffix + r"$")
        test_skip_re = re.compile(
            r"^\s+[○✎]\s+(?:skipped|todo)\s+(.+?)" + time_suffix + r"$"
        )

        current_suite: Optional[str] = None

        def qualify(name: str) -> str:
            return f"{current_suite} > {name}" if current_suite else name

        for raw_line in test_log.splitlines():
            line = ansi_re.sub("", raw_line).rstrip()
            if not line.strip():
                continue

            m = suite_pass_re.match(line)
            if m:
                current_suite = m.group(1)
                if current_suite not in failed_tests:
                    passed_tests.add(current_suite)
                continue

            m = suite_fail_re.match(line)
            if m:
                current_suite = m.group(1)
                passed_tests.discard(current_suite)
                failed_tests.add(current_suite)
                continue

            # Failure wins over a pass of the same qualified name (Jest reprints
            # a retried test), and a pass or fail is never downgraded to skipped.
            m = test_fail_re.match(line)
            if m:
                name = qualify(m.group(1))
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue

            m = test_pass_re.match(line)
            if m:
                name = qualify(m.group(1))
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
                continue

            m = test_skip_re.match(line)
            if m:
                name = qualify(m.group(1))
                if name not in failed_tests and name not in passed_tests:
                    skipped_tests.add(name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
