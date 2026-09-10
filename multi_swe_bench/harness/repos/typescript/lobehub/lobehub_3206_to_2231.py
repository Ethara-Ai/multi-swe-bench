"""lobehub/lobehub PRs #2231-#3206 - ten PRs from Apr-Jul 2024, graded through vitest.

Every value below was read off the repo at the ten base commits, not inferred:

  PR    base sha      base date   packageManager  vitest configs present
  2231  834a7be2a0d5  2024-04-27  (none -> npm)   vitest.config.ts
  2238  d2872bdd31e1  2024-04-27  (none -> npm)   vitest.config.ts
  2312  a354e3548411  2024-04-30  (none -> npm)   vitest.config.ts
  2431  2630c86ebb54  2024-05-08  (none -> npm)   vitest.config.ts
  2364  a77f4fbaafd8  2024-05-13  (none -> npm)   vitest.config.ts
  2683  622b3908dcbf  2024-05-27  (none -> npm)   vitest.config.ts
  2690  10b8f0ef90c5  2024-05-27  (none -> npm)   vitest.config.ts
  2753  1f306143d59e  2024-06-14  (none -> npm)   vitest.config.ts
  3046  23f3efbd79a4  2024-06-26  (none -> npm)   vitest.config.ts + vitest.server.config.ts
  3206  b63209e1ef15  2024-07-17  pnpm@9.5.0      vitest.config.ts + vitest.server.config.ts

Five things are worth knowing before changing anything here.

1. THIS FILE REGISTERS A number_interval KEY, NEVER THE PLAIN REPO NAME. `Instance.register`
   is a bare `_registry[name] = cls` with no duplicate check and no warning, and whichever
   import in the package `__init__.py` runs last wins. lobehub already holds three keys - the
   two era configs and `lobehub_router.py` under the plain `lobehub/lobehub` - so a fourth
   plain registration here would silently replace the router and break dispatch for every PR
   already processed in both eras. The raw dataset records therefore carry
   `"number_interval": "lobehub_3206_to_2231"`.

2. IT DOES NOT BUILD ITS OWN BASE IMAGE. `LobeHubImageBase` is imported from
   lobehub_6452_to_71 and reused as-is, so the repo keeps exactly one `base` tag across all
   three configs and this range adds no second clone of a history layer over a gigabyte. That
   base already does exactly what a base must: it installs node 20, git and libvips, clones
   once, and stops - no checkout, no install, no hardening, all of which are per-PR facts. It
   also carries the comment marker that stops DockerfileEnhancer._inject_final_sanitize from
   pinning a shared base to one PR's commit.

   The import couples this file to that module's public names, and that is deliberate: a
   second base class here would be a second base image. It is also a real dependency to keep
   in mind - the class was renamed from LobeHubImageBaseEarly to LobeHubImageBase and its tag
   from `base-early` to `base` on 2026-09-08, which broke this import until it was updated.

   These ten PRs sit inside the 71-6452 window that config serves, and the router would route
   them there on PR number alone. A separate file exists so that a fix made for this batch
   cannot change the images of PRs that range has already processed.

3. NPM FOR NINE COMMITS, PNPM FOR ONE, AND THE TREE SAYS WHICH. `package.json` gains a
   `packageManager` field only at the 2024-07-17 commit, where it reads `pnpm@9.5.0`; the
   other nine have no such field and are npm trees. prepare.sh reads that field rather than
   branching on a PR number, so the split is handled by one code path and an eleventh commit
   added to this range needs no edit here. The pnpm version installed is the one the field
   names, not `latest`.

4. GRADED BY A BARE `vitest run`, WHICH IS THE APP CONFIG. From 2024-06-26 the project splits
   its suite in two: `vitest.config.ts` (environment happy-dom, excludes
   `src/database/server/**` and `src/server/modules/**`) and `vitest.server.config.ts`
   (environment node, includes ONLY `src/database/server/**/*.test.ts`, setup file
   `tests/setup-db.ts`). A bare `vitest run` picks up vitest.config.ts at every commit here,
   including the two that carry both.

   That is the correct target and not a shortcut: all ten test patches write under
   `src/app`, `src/chains`, `src/features`, `src/hooks`, `src/libs`, `src/server/routers`,
   `src/services` and `src/store`, and not one of them touches `src/database/server` or
   `src/server/modules`. Adding the server config would contribute no gradeable test while
   requiring a live database for `tests/setup-db.ts`, so every one of its tests would fail
   identically in all three stages.

5. NODE 20, NOT THE 18 THE REPO NAMES. `.nvmrc` says `lts/hydrogen` at all ten commits, which
   is Node 18. node:20-bookworm is used instead because it is what the sibling era config for
   these same commits already runs on, and because Node 18 left security support in 2025 while
   nothing in this range requires it. The base image is shared with that config, so this is not
   independently choosable here anyway - see point 2.
"""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.lobehub.lobehub_6452_to_71 import (
    LobeHubImageBase,
)

# Reads package.json the way the project itself does. `packageManager` is absent on nine of
# the ten commits and reads `pnpm@9.5.0` on the tenth, so this one expression covers the whole
# range with no PR number anywhere in it. A malformed or missing file falls back to npm, which
# is what every commit without the field is.
PM_PROBE = (
    "node -e \"try {{ const pm = require('./package.json').packageManager;"
    " console.log(pm && pm.startsWith('pnpm@') ? 'pnpm' : 'npm'); }}"
    " catch (e) {{ console.log('npm'); }}\""
)

# The pnpm version the tree names, so the one commit that pins 9.5.0 gets 9.5.0 rather than
# whatever `latest` resolves to years later.
PNPM_VERSION_PROBE = (
    "node -e \"try {{ const pm = require('./package.json').packageManager;"
    " console.log(pm.split('@')[1]); }} catch (e) {{ console.log('latest'); }}\""
)

# `vitest run` and not `npm test`: at the 2024-07-17 commit `npm test` chains test-app and
# test-server, and test-server needs a live database that no test patch in this range has any
# use for. See point 4 in the module docstring.
#
# --reporter=verbose is what names every individual test rather than only the failures.
VITEST_ARGS = "vitest run --reporter=verbose"

# Every graded stage starts with this. The stages are separate processes, so nothing set in
# prepare.sh is inherited. CI=true puts vitest in single-run mode and stops it opening a watch;
# the heap bump is needed because the app suite loads the whole Next.js module graph.
#
# NO_COLOR rather than a --no-color flag: the env var is honoured by picocolors, which is what
# vitest and every reporter in this tree colour through, and it cannot be rejected as an
# unknown CLI option by a vitest version that does not carry that flag. parse_vitest_log
# strips escapes anyway, but a plain log is far easier to read when a stage has to be
# debugged by hand.
SHELL_ENV = """export CI=true
export NO_COLOR=1
export NODE_OPTIONS="--max-old-space-size=4096"
export NEXT_TELEMETRY_DISABLED=1"""


class LobeHubImageDefault2024(Image):
    """PR image for lobehub #2231-#3206, on the shared early-era base."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return LobeHubImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        env = SHELL_ENV

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                f"""\
#!/bin/bash
set -e

{env}

cd /home/{repo}

# No git operations in here. The PR Dockerfile has already run `git reset --hard` and
# `git checkout <base.sha>` before this script, and it runs the hardening block after. This
# call only asserts the tree it was handed is clean.
bash /home/check_git_changes.sh

# Network settings for the install below. This tree pulls a few thousand packages, and under
# QEMU the arm64 install runs for ten to twenty minutes, so it is exposed to any drop in that
# window - four builds in this range died mid-install on 2026-09-09 with three different
# shapes of the same thing: ECONNRESET on @clerk/localizations, ENOTFOUND on registry.npmjs.org
# while Docker's internal DNS was restarting, and npm's own "Exit handler never called".
#
# npm retries a failed request five times here instead of the default two, and waits longer
# between attempts. That covers a dropped packet; it does not cover the registry being
# unreachable for a minute, which is what retry_install below is for.
export npm_config_fetch_retries=5
export npm_config_fetch_retry_mintimeout=20000
export npm_config_fetch_retry_maxtimeout=180000
export npm_config_fetch_timeout=600000

# Whole-install retry, on top of npm's per-request retries. An install that gets most of the
# way and then loses the network leaves node_modules half populated, and npm resumes from
# what is already on disk, so a second attempt is far cheaper than the first rather than a
# fresh ten-minute run. Three attempts with a pause between them; the last one's exit status
# is the step's status, so a genuine dependency error still fails the build rather than being
# retried into a false success.
retry_install() {{
  attempt=1
  while [ "$attempt" -le 3 ]; do
    echo "prepare: install attempt $attempt of 3"
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -eq 3 ]; then
      echo "prepare: install failed after 3 attempts" >&2
      return 1
    fi
    echo "prepare: install attempt $attempt failed, retrying in 30s" >&2
    sleep 30
    attempt=$((attempt + 1))
  done
}}

# Which package manager, read from the tree rather than from the PR number. See point 3.
PKG_MANAGER=$({PM_PROBE})
echo "prepare: package manager $PKG_MANAGER (from package.json)"

if [ "$PKG_MANAGER" = "pnpm" ]; then
  PNPM_VERSION=$({PNPM_VERSION_PROBE})
  echo "prepare: installing pnpm@$PNPM_VERSION"
  retry_install npm install -g "pnpm@$PNPM_VERSION"
  # --no-frozen-lockfile because no commit in this range commits a lockfile: the install has
  # to resolve, and with a frozen lockfile pnpm refuses outright instead.
  retry_install pnpm install --no-frozen-lockfile
else
  # --legacy-peer-deps because this tree predates npm's stricter peer resolution and several
  # of its @lobehub packages declare peers that no longer agree; without it npm aborts on a
  # conflict the project itself never had.
  retry_install npm install --legacy-peer-deps
fi

# Two gates, both cheap, and neither is implied by the install above: an install that half
# succeeded still leaves a runnable node_modules, and a broken one would otherwise build green
# and make all three graded stages report (0,0,0) with nothing naming the cause.
#
# Deliberately NOT a real vitest run. Executing the suite here would double the build time and
# prove nothing extra - the run stage executes it immediately afterwards anyway, and a suite
# that legitimately fails at the base commit would fail this gate for the wrong reason.
npx vitest --version

TEST_FILES=$(find src -name '*.test.ts' -o -name '*.test.tsx' | wc -l)
echo "prepare: $TEST_FILES test files under src/"
if [ "$TEST_FILES" -lt 1 ]; then
  echo "prepare: no test files found under src/, tree is not what this config expects" >&2
  exit 1
fi
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
npx {VITEST_ARGS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npx {VITEST_ARGS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npx {VITEST_ARGS}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        if isinstance(dep, str):
            raise ValueError("ImageDefault dependency must be an Image")

        # COPY lines are generated from files(), never hand-listed, so a file added there
        # cannot be left uncopied - which would surface at build time as
        # `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # The hardening block with the sha inlined rather than left as ${BASE_COMMIT}, so the
        # generated Dockerfile is self-describing and cannot drift from the sha checked out
        # three lines above it.
        #
        # Order matters: checkout -> prepare.sh -> hardening. prepare.sh installs against the
        # base commit, and the hardening then re-detaches at that same sha, deletes every
        # other ref, expires the reflog and asserts the result - so nothing later than the
        # base commit is reachable from inside the image and a graded stage cannot read the
        # real fix out of git history. That pruning also removes the full history this image
        # inherits from the shared base.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh

{hardening}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _clean_test_name(name: str) -> str:
    """Strip the timing and counts vitest appends, which differ between stages.

    A name carrying `(2 tests) 75ms` reads as a different test on the next run, which would
    manufacture transitions that never happened.
    """
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


# vitest's verbose reporter marks each test with a glyph. The variants are all accepted
# because which one is emitted depends on the terminal capability vitest detects, and a stage
# that fell back to ASCII would otherwise parse as an empty run.
_PASS_RE = re.compile(r"^[✓✔]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$")
_FAIL_RE = re.compile(r"^[×✕✗]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$")
_SKIP_RE = re.compile(r"^[↓○]\s+(.+?)(?:\s+\[skipped\])?$")
# A file that fails to transpile or import never reaches a test glyph; vitest names it on a
# FAIL line instead. Keeping it is correct - a module that cannot load is a real regression.
_FILE_FAIL_RE = re.compile(r"^FAIL\s+(.+?)$")


def parse_vitest_log(test_log: str) -> TestResult:
    """Read a vitest verbose run into the three buckets report.py compares between stages."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Strip ANSI FIRST. --no-color asks vitest not to colourise, but the glyphs still arrive
    # wrapped when a dependency writes through its own reporter, and a single escape sequence
    # in front of the glyph defeats every pattern below - the stage then reports 0/0/0 and
    # Report.check() rejects it with no clue why.
    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _PASS_RE.match(line)
        if m:
            passed_tests.add(_clean_test_name(m.group(1)))
            continue

        m = _FAIL_RE.match(line) or _FILE_FAIL_RE.match(line)
        if m:
            failed_tests.add(_clean_test_name(m.group(1)))
            continue

        m = _SKIP_RE.match(line)
        if m:
            skipped_tests.add(_clean_test_name(m.group(1)))

    # Worst result wins. TestResult.__post_init__ raises ValueError on any overlap, which
    # would abort the whole instance rather than mis-grade it.
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


@Instance.register("lobehub", "lobehub_3206_to_2231")
class LOBEHUB_3206_TO_2231(Instance):
    """Instance for lobehub PRs #2231-#3206 (Apr-Jul 2024, vitest app suite)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return LobeHubImageDefault2024(self.pr, self._config)

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
        return parse_vitest_log(test_log)
