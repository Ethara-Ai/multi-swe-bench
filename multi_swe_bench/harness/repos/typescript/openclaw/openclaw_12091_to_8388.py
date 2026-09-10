"""openclaw/openclaw -- era config for PRs 8388 .. 12091.

Twenty PRs, ONE era, so ONE shared base image (rule 4, row 2). The toolchain
was read from package.json at ALL TWENTY base commits, not sampled from one:

    engines.node    >=22.12.0      identical at all 20
    packageManager  pnpm@10.23.0   identical at all 20
    devDeps.vitest  ^4.0.18        identical at all 20
    scripts.test    node scripts/test-parallel.mjs   identical at all 20

Nothing shifts across the range, so a single base is correct and safe.

A config for this repo already exists -- `openclaw_1372_to_547.py`, covering
PRs 547..1372. It is a DIFFERENT era and is deliberately left untouched
(rule 12, 2026-09-08: never modify an existing repo config, always add a new
one for the dataset in hand).

WHY FOUR TEST GROUPS, AND WHY THAT IS NOT OPTIONAL
--------------------------------------------------
The repo does not have one test suite. `scripts/test-parallel.mjs` fans out
across three vitest configs, and a fourth exists for the e2e tier:

    vitest.unit.config.ts        src/** minus src/gateway/**, minus extensions/**
    vitest.extensions.config.ts  extensions/**
    vitest.gateway.config.ts     src/gateway/**
    vitest.e2e.config.ts         src/**/*.e2e.test.ts and test/**/*.e2e.test.ts

The first three inherit `exclude` from vitest.config.ts, which contains
`**/*.e2e.test.ts`. So a config that ran only the normal suite would never
execute a single `.e2e.test.ts` file -- and this dataset grades eight of them:

    PR 10415   3 graded files, ALL .e2e.test.ts   -> test_patch_result = 0
    PR  9855   4 graded files, ALL .e2e.test.ts   -> test_patch_result = 0
    PR 11356   1 e2e + 1 unit                     -> half the signal lost

That is precisely the zero rule 11 forbids. All four groups therefore run.
Every graded test file in all twenty PRs was classified against the four
include/exclude sets and every one lands in unit, gateway or e2e.

`.live.test.ts` stays excluded in all four groups. Those are the only tests
that reach the network, and they are gated behind OPENCLAW_LIVE_TEST anyway.
The `.e2e.test.ts` tier is in-process vitest, not a network tier -- test
setup.ts installs an isolated HOME and a stub channel registry -- so running
it inside the image needs nothing extra.

ONE VITEST INVOCATION PER GROUP (rule 11)
------------------------------------------
The four groups are run in four separate `vitest run` calls, not one call with
four configs. A group whose config or setup file blows up can then only take
itself down. This is the surrealdb pr-2465 lesson applied to vitest: one
invocation carrying every suite lets a single collection error zero the whole
stage.

Inside a group vitest already isolates each test file in its own fork, so a
file that fails to load is reported as a failed suite while the other files
still run. The per-group split guards the layer above that, which vitest does
not guard: the config, the setup file and the runner process itself.

EXPECTED-TEST MANIFEST
----------------------
Before each group runs, `vitest list` enumerates the tests that group intends
to run. Any test that was enumerated and then produced no result line -- a
worker OOM, a hard process exit, a hang killed by the timeout -- is reported
FAILED rather than silently dropped. Without that, a crashed worker deletes
its tests from the report entirely, which is the failure mode that quietly
manufactures false F2P transitions.

THE HONEST BOUNDARY, stated rather than papered over
-----------------------------------------------------
Six PRs add tests that import a symbol the fix patch introduces:

    12091  expandHomePrefix, resolveEffectiveHomeDir, resolveRequiredHomeDir
    11560  restoreEnvVarRefs
    11356  getDmHistoryLimitFromSessionKey
    10774  bindAbortRelay
    10176  classifySessionKeyShape, resolveRunWorkspaceDir
     8388  MIN_AUDIO_FILE_BYTES

In the TEST act those files cannot be imported, so their tests neither run nor
enumerate. They appear in the fix act only, which the harness classifies as
N2P (none-to-pass) -- the normal, correct category for a newly added test.
Nothing is invented for them. Because vitest isolates per file, the loss is
confined to those files: the other ~960 test files still report in full, so no
stage reports zero.

STRUCTURE -- rule 9 (2026-09-03), which supersedes rules 4/5/8 on placement:

    base Dockerfile   toolchain, infra block, apt, corepack/pnpm, WORKDIR
                      /home/, git clone, CMD. NOTHING after the clone.
    PR Dockerfile     FROM base, COPY lines, ARG BASE_COMMIT, RUN prepare.sh,
                      WORKDIR, then the FULL hardening block with all four
                      asserts.
    prepare.sh        checkout, then the dependency install. No stripping.

Two harness details make that placement work, both checked in
multi_swe_bench/harness/image.py rather than assumed:

  * `DockerfileEnhancer.enhance()` returns the Dockerfile untouched when it
    already carries the BuildKit syntax directive (image.py:316). The base
    below emits that directive itself, so the enhancer's
    `_inject_final_sanitize()` -- which would otherwise append the hardening
    block before the CMD, putting the scrub back into the base -- never runs.
    The infrastructure block is produced by calling
    `DockerfileEnhancer._infrastructure_block()` directly so it cannot drift
    from what every other image gets.
  * The scrub runs in an Image-dependency layer, and those receive NO build
    args (build_dataset.py:623-628 passes REPO_URL/BASE_COMMIT only when
    `dependency()` returns a str). So the PR Dockerfile carries the sha itself
    as `ARG BASE_COMMIT="<sha>"`.

Check before running, inverted from rule 5 exactly as rule 9 requires:
`grep -c 'rev-list --all --count'` must be 0 in the base Dockerfile and 1 in
the PR Dockerfile.

Every generated artifact ships WITHOUT comments (rule 10). The reasoning for
each one sits in a Python comment directly above its string literal.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# The four vitest project configs, in the order they run. Verified present at
# BOTH ends of the range -- 96ef6ea3 (PR 8388) and c95e6fe6 (PR 12091) -- so
# no PR in the set loses a group. run_tests.sh still guards each one with a
# file-existence test, because a silently-dropped group would turn a real
# failure into "no result" rather than into a loud error.
_TEST_GROUPS = "unit extensions gateway e2e"

# The e2e group runs ONLY for the three PRs whose test patch actually grades a
# `.e2e.test.ts` file. Everyone else gets the three stable groups.
#
# This is not a convenience. The e2e tier starts real gateway servers, binds
# real TCP ports and spawns processes, and it is where every unstable result in
# this dataset came from. Two measurements from the 2026-09-08/09 runs:
#
#  * Whole e2e FILES die under load, taking every test inside them with them.
#    pr-11356 lost 20 tests and pr-10415 lost 39 in the fix act alone -- six
#    files, every one of them `.e2e.test.ts`, no other group affected. Those
#    tests print no result line at all, so the act totals move between stages.
#  * Two of the five instances that failed validation were killed by an e2e
#    test unrelated to the PR: pr-11656 by gateway.e2e.test.ts and pr-8392 by
#    server.cron.e2e.test.ts. Report.check() rule 2 rejects ANY test that
#    passes in the test act and fails in the fix act, related or not, so one
#    flip destroys the whole instance.
#
# For the seventeen PRs that grade no e2e file, running that tier therefore
# buys zero graded signal and carries the dataset's single largest source of
# false failures. For the three that DO grade one it is mandatory -- without it
# pr-10415 and pr-9855 would report a test_patch_result of zero, which is the
# very thing rule 11 forbids.
#
# Keeping the choice keyed on the PR rather than deleting the group outright
# also means a future rebuild of pr-10415, pr-9855 or pr-11356 cannot silently
# strip their graded tests.
_E2E_GRADED_PRS = {10415, 9855, 11356}


def _groups_for(pr) -> str:
    """Return the space-separated vitest groups this PR should run."""
    if pr.number in _E2E_GRADED_PRS:
        return _TEST_GROUPS
    return "unit extensions gateway"


# Files that have been MEASURED unstable in this repo and that destroy an
# instance they have nothing to do with. Report.check() rule 2 rejects any test
# that passes in the test act and fails in the fix act, however unrelated, so a
# single flip in one of these files zeroes an otherwise perfect PR.
#
# Kill record across the 2026-09-08/09 runs, one different test each time:
#
#   pi-tools.workspace-paths.test.ts   pr-9789 (writes), pr-9870 (reads),
#                                      pr-8702 (writes, then edits) -- 4 kills
#   cli/program.smoke.test.ts          pr-9870 -- 1 kill
#   slack/monitor/media.test.ts        pr-11656 -- 1 kill
#
# Measured rather than assumed: pi-tools.workspace-paths.test.ts was run NINE
# times inside the already-built pr-8702 image with no code change at all and
# passed 8 of 9, varying within a single unchanged state. The cause is visible
# in the test itself -- it calls
#     vi.spyOn(process, "cwd").mockReturnValue(otherDir)
# mocking a PROCESS-GLOBAL while doing real filesystem work in temp dirs, which
# is order- and timing-sensitive. cli/program.smoke.test.ts spawns a child
# process; slack/monitor/media.test.ts falls through to a real DNS lookup when
# its vi.resetModules() races the dynamic import.
#
# The exclusion is keyed on the PR and NEVER applies to a file that PR grades.
# pr-9903's test patch covers pi-tools.workspace-paths.test.ts and
# program.smoke.test.ts, so pr-9903 keeps both and is unaffected. Cost elsewhere
# is about 8 pass-to-pass tests out of ~6,240 -- 0.1%.
#
# This is the escalation the project checklist already sanctions: "If the
# failing suite ROTATES between runs, stop tuning and exclude it." No test is
# edited and no graded test is ever dropped.
#
# `--exclude` APPENDS to the config's own exclude list rather than replacing it
# -- verified in-container on 2026-09-08 by listing the gateway group with and
# without the flag: 249 -> 245 tests, and zero `.e2e.test.ts` files leaked back
# in. So it cannot accidentally widen the graded set.
_FLAKY_FILES = (
    "src/agents/pi-tools.workspace-paths.test.ts",
    "src/cli/program.smoke.test.ts",
    "src/slack/monitor/media.test.ts",
)


def _graded_files(pr) -> set:
    """Test files this PR's own test patch touches. Never excluded."""
    return set(re.findall(r"^\+\+\+ b/(\S+)", str(pr.test_patch or ""), re.M))


def _excludes_for(pr) -> str:
    graded = _graded_files(pr)
    return " ".join(
        f'--exclude "{f}"' for f in _FLAKY_FILES if f not in graded
    )

# The ONE place the vitest flags are spelled out, so the three acts cannot
# drift apart.
#
#   --reporter=verbose  prints one line per test with a status marker; the
#                       default reporter prints per-FILE summaries only, which
#                       would collapse every suite into a single id.
#   --testTimeout       matches the repo's own vitest.config.ts (120s/120s), so
#                       a test that is merely slow here is not reported as
#                       failed when upstream CI would pass it.
#   --retry=2           openclaw's own suite is timing-sensitive under parallel
#                       load, and a single flaky test is enough to destroy an
#                       otherwise perfect instance: report.check() rule 2
#                       rejects ANY test that goes PASS in the test act and
#                       FAIL in the fix act, however unrelated it is to the PR.
#
#                       That is not hypothetical. The first full run on
#                       2026-09-08/09 lost FIVE of twenty instances that way,
#                       each to a different unrelated test:
#
#                         pr-11656  src/gateway/gateway.e2e.test.ts
#                         pr-9870   src/cli/program.smoke.test.ts
#                         pr-9789   src/agents/pi-tools.workspace-paths.test.ts
#                         pr-8702   src/agents/pi-tools.workspace-paths.test.ts
#                         pr-8392   src/gateway/server.cron.e2e.test.ts
#
#                       Both repeat offenders were re-run in their own built
#                       images to confirm flakiness rather than assume it. The
#                       workspace-paths test passed 8 of 9 attempts with NO code
#                       change, varying within a single unchanged state; the
#                       gateway one passed 4 of 4. At roughly a 10% per-attempt
#                       failure rate, three attempts fail together about once in
#                       a thousand runs.
#
#                       Vitest prints ONE line per test carrying the final
#                       outcome, annotated `(retry x1)`, not one line per
#                       attempt -- verified in-container. So the pessimistic
#                       de-duplication in vitest_test_report.py cannot cancel
#                       the retry out, and the `(retry xN)` suffix is stripped
#                       by the TRAILING pattern so ids stay identical across
#                       acts. No reporter change is needed.
#
#                       The old openclaw_1372_to_547.py config carries the same
#                       flag for the same reason; dropping it here was an
#                       omission, not a decision.
#
# >> WITHDRAWN 2026-09-09 AFTER MEASUREMENT. Everything above describes why
# >> --retry=2 was added; it was then tried on the five failing instances and
# >> it did NOT work. Keeping the reasoning so the same idea is not proposed a
# >> third time.
# >>
# >> What actually happened:
# >>   * pr-11656 failed again. Its killer test printed `(retry x2)` with three
# >>     identical errors -- the retries ran and all three failed, because the
# >>     fault (a vi.resetModules() race that falls through to a real DNS call)
# >>     persists for the whole act rather than being a per-attempt coin flip.
# >>   * pr-9870 failed again, killed by pi-tools.workspace-paths.test.ts --
# >>     the very test I had measured as 8-of-9 passing on an IDLE machine.
# >>     Under real pipeline load it failed all three attempts. Generalising
# >>     from an idle measurement to a loaded one was the error.
# >>   * Cost: act time went from ~25 min to ~59 min, a straight doubling,
# >>     because every one of the ~35 standing failures now ran three times.
# >>
# >> Nett: zero instances saved, runtime doubled. The condition common to every
# >> failure was CONTENTION -- two instances competing for ten cores -- so the
# >> remedy is fewer things running at once (--max_workers_run_instance 1) plus
# >> not running the e2e tier for PRs that do not grade it, which is what
# >> _groups_for() above now does.
_VITEST_FLAGS = "--reporter=verbose --testTimeout=120000 --hookTimeout=180000"

# Worker count is chosen PER GROUP, and the e2e number was MEASURED rather
# than reasoned about, because the obvious reasoning turned out to be wrong.
#
# The e2e group starts real gateway servers that bind real TCP ports, so the
# expectation was that parallel workers collide and produce failures that move
# between runs -- which would manufacture false F2P transitions. Both settings
# were run against the clean base commit c95e6fe6 on 2026-09-08:
#
#     --maxWorkers=4   19 of 53 files failed, 30 tests failed,  144 s
#     --maxWorkers=1   18 of 53 files failed, 29 tests failed,  314 s
#
# One file's difference. The failures are therefore NOT contention -- they are
# stable environment failures at that commit, and serialising costs 170 s per
# act (about 2.8 hours across three acts and twenty PRs) to buy essentially
# nothing. So e2e uses 2, which is exactly what the repo's own
# vitest.e2e.config.ts sets for CI. Matching upstream CI is the defensible
# choice: it is the configuration these tests are actually expected to pass in.
#
# The other three groups are in-process unit tests with an isolated HOME per
# file, so they parallelise safely. Three is the ceiling this box can hold:
# 10 cores, Docker capped at 8 GB, and the pipeline runs 2 instances at once,
# so 2 x 3 forks plus 2 main processes is already the limit before the kernel
# starts OOM-killing workers -- and an OOM-killed worker silently deletes its
# tests from the report.
_GROUP_WORKERS = {"unit": 3, "extensions": 3, "gateway": 3, "e2e": 2}


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


# apply_patch.sh -- plain `git apply` first, `--3way` only as a fallback, so a
# genuine apply failure is still visible from the primary attempt rather than
# being papered over by the fallback. No patch in this dataset carries binary
# hunks, so there is nothing to lift out of git blobs before applying.
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/openclaw
for patch in "$@"; do
  if ! git apply --whitespace=nowarn "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
"""


# vitest_test_report.py -- turn the four vitest runs into per-test results.
#
# Written as a RAW string literal so every backslash in the regexes below is
# the literal character that reaches the file. A non-raw literal would need
# each one doubled, and one missed backslash is a silent parser change.
#
# Two jobs, mirroring the cargo reporter used on surrealdb:
#
#   --list <file>     read the combined `vitest list` output and print one
#                     "<file>\t<name>" record per enumerated test.
#   <run> <manifest>  read the combined run output and print the per-test
#                     report in the trailing-keyword form parse_log reads.
#
# Why the manifest exists: a test whose worker dies -- an OOM kill, a hard
# process.exit, a hang the timeout terminates -- never prints its own status
# line, and every test queued behind it in that worker prints nothing at all.
# A plain line-scraper reports those as absent rather than failed, which
# deletes exactly the signal an F2P instance is built on. Any test that was
# enumerated and produced no result line is therefore reported FAILED: the
# runner was asked to run it and the run did not complete.
#
# Ids are `vitest::<test file>::<suite path > test name>`. The file comes from
# vitest's own output, so it is stable across acts; nothing derived from a
# worker id or a hash appears in an id, because a drifting id between acts
# manufactures a false F2P.
_VITEST_TEST_REPORT_PY = r'''"""Turn vitest verbose output into per-test results.

usage:
    vitest_test_report.py --list <combined vitest list output>
    vitest_test_report.py --collect-error <vitest list stderr>
    vitest_test_report.py <combined run output> <expected manifest>

The report form emits one line per test in the trailing-keyword shape parse_log
reads:

    vitest::src/utils.test.ts::resolveUserPath > returns undefined PASSED
    vitest::src/cron/store.test.ts::store > writes rows FAILED

Exit status mirrors a test runner: 0 = everything passed, 1 = at least one test
failed, 2 = the run produced no tests at all.
"""
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x00")

# A vitest test path always begins with the spec file it lives in.
SPEC = r"[^\s>]+\.(?:test|spec)\.[cm]?[jt]sx?"

# Verbose reporter, one line per test:
#   " ✓ src/utils.test.ts > group > name 12ms"
#   " × src/utils.test.ts > group > name 5ms"
#   " ↓ src/utils.test.ts > group > name [skipped]"
# The trailing group swallows vitest's duration and retry annotations so they
# cannot leak into the captured name.
TRAILING = r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s))?(?:\s*\(retry\s+x\d+\))?(?:\s*\[[^\]]*\])?"
CASE = re.compile(
    r"^\s*(?P<marker>[✓✔√×✕✖✗✘↓○])\s+"
    r"(?P<name>" + SPEC + r"\s+>\s+.*?)" + TRAILING + r"\s*$"
)

# `vitest list` prints the same "<file> > <suite> > <name>" path, one per line,
# with no marker and no timing.
LISTED = re.compile(r"^\s*(?P<name>" + SPEC + r"\s+>\s+.+?)\s*$")

# A file that fails to load never yields per-test lines. Vitest announces it as
# a failed suite instead. Captured so an unloadable file is visible in the
# report rather than vanishing from it.
SUITE_FAIL = re.compile(r"^\s*FAIL\s+(?P<file>" + SPEC + r")\s*(?:\[.*\])?\s*$")

PASS_MARKERS = set("✓✔√")
FAIL_MARKERS = set("×✕✖✗✘")
SKIP_MARKERS = set("↓○")


def read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield ANSI.sub("", line).rstrip("\n")
    except IOError:
        return


def split_path(full):
    """Split "<file> > <suite> > <name>" into (file, remainder)."""
    parts = full.split(" > ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return parts[0].strip(), ""


def emit_list(path):
    """Print one "<file>\t<name>" record per enumerated test."""
    count = 0
    seen = set()
    for line in read_lines(path):
        listed = LISTED.match(line)
        if not listed:
            continue
        src, name = split_path(listed.group("name"))
        if not name:
            continue
        key = (src, name)
        if key in seen:
            continue
        seen.add(key)
        sys.stdout.write("%s\t%s\n" % (src, name))
        count += 1
    return 0 if count else 2


def load_expected(path):
    """Read the manifest into an ordered, de-duplicated list of (file, name)."""
    expected = []
    seen = set()
    for line in read_lines(path):
        if "\t" not in line:
            continue
        src, name = line.split("\t", 1)
        key = (src.strip(), name.strip())
        if key in seen:
            continue
        seen.add(key)
        expected.append(key)
    return expected


def emit_report(run_path, manifest_path):
    results = {}
    order = []
    failed_files = []

    for line in read_lines(run_path):
        suite = SUITE_FAIL.match(line)
        if suite:
            f = suite.group("file")
            if f not in failed_files:
                failed_files.append(f)
            continue
        case = CASE.match(line)
        if not case:
            continue
        src, name = split_path(case.group("name"))
        if not name:
            continue
        key = (src, name)
        marker = case.group("marker")
        if marker in PASS_MARKERS:
            status = "PASSED"
        elif marker in FAIL_MARKERS:
            status = "FAILED"
        elif marker in SKIP_MARKERS:
            status = "SKIPPED"
        else:
            continue
        if key not in results:
            order.append(key)
        # A repeated name within one act is resolved pessimistically: a FAILED
        # sighting is never overwritten by a later PASSED one. Vitest reprints
        # a test on retry, and the pessimistic rule keeps a flaky test out of
        # the F2P set instead of letting a lucky retry create one.
        if results.get(key) != "FAILED":
            results[key] = status

    expected = load_expected(manifest_path)
    missing = [key for key in expected if key not in results]
    for key in missing:
        results[key] = "FAILED"
        order.append(key)

    # A file that could not even be loaded gets one synthetic entry, so the
    # report says so out loud. It is deliberately NOT expanded into per-test
    # entries: the tests inside were never enumerated, and inventing them
    # would be inventing data.
    for f in failed_files:
        key = (f, "<suite failed to load>")
        if key not in results:
            results[key] = "FAILED"
            order.append(key)

    if not results:
        sys.stderr.write(
            "vitest_test_report: NO tests produced a result and none were "
            "enumerated -- the runner never started.\n"
        )
        return 2

    for src, name in order:
        sys.stdout.write("vitest::%s::%s %s\n" % (src, name, results[(src, name)]))

    if missing:
        sys.stderr.write(
            "vitest_test_report: %d enumerated test(s) produced no result line "
            "and are reported FAILED:\n" % len(missing)
        )
        for src, name in missing:
            sys.stderr.write("  %s::%s\n" % (src, name))

    return 1 if any(v == "FAILED" for v in results.values()) else 0


def emit_collect_error(path):
    """Print the spec file that aborted a `vitest list`, or nothing.

    `vitest list` evaluates every matching module, so ONE file that throws at
    import time aborts the whole enumeration and prints no tests at all. The
    error names the file in a stack frame:

        ReferenceError: beforeEach is not defined
         > src/gateway/server.auth.e2e.test.ts:311:5

    run_tests.sh feeds that name back in as a file to drop, then re-runs the
    enumeration over the remaining files, so one broken module costs its own
    tests and nothing else.
    """
    for line in read_lines(path):
        m = re.search(SPEC, line)
        if m:
            sys.stdout.write(m.group(0) + "\n")
            return 0
    return 1


def main(argv):
    if len(argv) >= 3 and argv[1] == "--list":
        return emit_list(argv[2])
    if len(argv) >= 3 and argv[1] == "--collect-error":
        return emit_collect_error(argv[2])
    if len(argv) >= 3:
        return emit_report(argv[1], argv[2])
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''


# run_tests.sh -- the ONE definition of the graded test command. run.sh,
# test-run.sh and fix-run.sh differ only in which patches they apply first, so
# the three acts can never drift apart.
#
# Emitted WITHOUT comments (rule 10). What the bare script no longer says:
#
#  * `set -e` is deliberately ABSENT here, and ONLY here. vitest exits non-zero
#    whenever a test fails, which is the normal outcome of the baseline and
#    test acts. With -e the script would die at that line and never print the
#    per-test report, so parse_log would see nothing and the instance would be
#    discarded. The three ACT scripts that call this one DO use
#    `set -eo pipefail`, so a failed patch apply still aborts before here.
#  * ONE vitest invocation PER GROUP, never one call carrying all four. This
#    is rule 11, and it is the surrealdb pr-2465 lesson: a single invocation
#    lets one collection error zero the entire stage.
#  * `vitest list` runs before each group's graded run so the expected-test
#    manifest is built from the same commit and the same config. A test that
#    was enumerated and then produced no result line is reported FAILED by
#    vitest_test_report.py.
#  * the enumerate() helper exists because `vitest list` is ALL-OR-NOTHING: it
#    imports every matching module, so a single file that throws at import time
#    aborts the enumeration and prints zero tests. That is not hypothetical --
#    at the clean base commit c95e6fe6, src/gateway/server.auth.e2e.test.ts
#    calls `beforeEach` without importing it, and the whole e2e group enumerated
#    0 tests as a result (measured 2026-09-08). enumerate() therefore falls back
#    to `--filesOnly`, drops the file named in the error, and re-runs the
#    enumeration over the rest, up to eight rounds. One broken module then costs
#    its own tests and nothing else.
#  * the fallback uses POSITIONAL file arguments rather than `--exclude`.
#    Positional args are a filter applied on top of the config's own
#    include/exclude, so they cannot accidentally widen the set -- whereas a CLI
#    `--exclude` risks replacing the config's exclude list, which would drag
#    `**/*.live.test.ts` and `dist/**` back into a graded run.
#  * `--maxWorkers` is chosen per group by workers_for(). See _GROUP_WORKERS.
#  * vitest is never piped. A pipeline hands back the exit code of the LAST
#    command rather than vitest's, and a failed run would read as a clean one.
#  * NO_COLOR / FORCE_COLOR=0 because a coloured status marker never matches an
#    anchored regex; the reporter also strips ANSI, so this is belt and braces.
#  * CI=true is what makes vitest.config.ts pick its CI worker counts and what
#    the repo's own tests check for; the acts must look like CI, not like a
#    developer laptop.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

cd /home/openclaw

export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export SHELL=/bin/bash
export NODE_OPTIONS="--max-old-space-size=2560"

OUT=/tmp/vitest.out
LIST=/tmp/vitest.list
EXPECTED=/tmp/expected_tests.tsv
: > "$OUT"
: > "$LIST"

workers_for() {
  case "$1" in
__WORKER_CASES__
    *) echo 2 ;;
  esac
}

enumerate() {
  grp="$1"
  cfg="$2"
  tmp="/tmp/list.$grp"
  err="/tmp/list.$grp.err"
  files="/tmp/files.$grp"

  if pnpm exec vitest list --config "$cfg" > "$tmp" 2> "$err"; then
    cat "$tmp" >> "$LIST"
    return 0
  fi

  if ! pnpm exec vitest list --config "$cfg" --filesOnly > "$files" 2>/dev/null; then
    echo "run_tests: WARNING could not enumerate group $grp at all" >> "$OUT"
    return 1
  fi
  sed -i -e 's/^[[:space:]]*//' -e '/^$/d' "$files"

  for round in 1 2 3 4 5 6 7 8; do
    bad=$(python3 /home/vitest_test_report.py --collect-error "$err")
    if [ -z "$bad" ]; then
      break
    fi
    echo "run_tests: group $grp enumeration aborted on $bad, dropping it and retrying" >> "$OUT"
    grep -v -F "$bad" "$files" > "$files.next"
    mv "$files.next" "$files"
    if [ ! -s "$files" ]; then
      break
    fi
    if xargs -a "$files" pnpm exec vitest list --config "$cfg" > "$tmp" 2> "$err"; then
      cat "$tmp" >> "$LIST"
      return 0
    fi
  done

  echo "run_tests: WARNING group $grp enumeration is incomplete" >> "$OUT"
  cat "$tmp" >> "$LIST"
  return 1
}

rc=0
for group in __GROUPS__; do
  cfg="vitest.$group.config.ts"
  if [ ! -f "$cfg" ]; then
    echo "run_tests: WARNING $cfg is absent at this commit, skipping" >> "$OUT"
    continue
  fi
  enumerate "$group" "$cfg"
  pnpm exec vitest run --config "$cfg" __FLAGS__ __EXCLUDES__ --maxWorkers="$(workers_for "$group")" >> "$OUT" 2>&1
  group_rc=$?
  if [ "$group_rc" -ne 0 ]; then
    rc=$group_rc
    echo "run_tests: group $group exited $group_rc" >> "$OUT"
  fi
done

python3 /home/vitest_test_report.py --list "$LIST" > "$EXPECTED"

cat "$OUT"
echo "vitest exit=${rc}"
echo "expected tests enumerated: $(wc -l < "$EXPECTED")"
echo "----- per-test results -----"
python3 /home/vitest_test_report.py "$OUT" "$EXPECTED"
"""


def _run_tests_sh(pr) -> str:
    """Render run_tests.sh for one PR.

    Per-PR rather than a module constant because _groups_for() gives the three
    PRs that grade a `.e2e.test.ts` file a different group list from the other
    seventeen. Everything else in the script is identical for every PR, so the
    three acts still cannot drift apart within an instance.
    """
    groups = _groups_for(pr)
    cases = "\n".join(
        f"    {g}) echo {_GROUP_WORKERS[g]} ;;" for g in groups.split()
    )
    return (
        _RUN_TESTS_SH.replace("__WORKER_CASES__", cases)
        .replace("__GROUPS__", groups)
        .replace("__FLAGS__", _VITEST_FLAGS)
        .replace("__EXCLUDES__", _excludes_for(pr))
    )


class OpenclawEraImageBase(Image):
    """Shared era base (`base-openclaw_12091_to_8388`): toolchain + clone only.

    Rule 9: nothing after the `git clone`. The image therefore keeps the FULL
    git history, which is what makes one base safe for all twenty PRs -- no
    commit can be missing, so no PR has to fetch anything back. The pin and the
    prune happen per PR, in the PR layer.
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
        # package.json pins engines.node >=22.12.0 at every commit in this
        # range, and pnpm is activated through corepack below rather than
        # installed globally, so the node major is the only thing this tag has
        # to get right.
        #
        # The `-bookworm` suffix is load-bearing, not cosmetic. The plain
        # `node:22` tag has moved between Debian releases before, and an
        # unsupported Debian's apt pool 404s on the very package versions its
        # own index advertises -- exactly how the surrealdb base died on
        # 2026-09-07. Naming the distribution pins that away.
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base-openclaw_12091_to_8388"

    def workdir(self) -> str:
        return "base-openclaw_12091_to_8388"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()

        # Emitting the syntax directive ourselves makes DockerfileEnhancer a
        # no-op for this image (image.py:316), which is the only way to keep
        # the scrub OUT of the base -- its _inject_final_sanitize() would
        # otherwise append the hardening block before the CMD. The infra block
        # is generated by the enhancer's own helper so it stays identical to
        # what every other image in the tree receives.
        infra = DockerfileEnhancer._infrastructure_block(self, base_img).rstrip("\n")

        # apt on one line on purpose. A multi-line RUN inside a Python template
        # needs every trailing backslash doubled, and one missed backslash
        # silently collapses the command -- a trap this project has been caught
        # by before.
        #
        # build-essential and python3 are here because this dependency graph
        # contains native modules that build from source when no prebuilt
        # binary matches the platform (sharp, node-llama-cpp, protobufjs and
        # the matrix crypto binding all run postinstall steps). python3 also
        # runs vitest_test_report.py inside the acts.
        apt = (
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "build-essential ca-certificates curl git gnupg make python3 "
            "pkg-config wget "
            "&& rm -rf /var/lib/apt/lists/*"
        )

        # corepack is what turns packageManager: pnpm@10.23.0 into a real
        # binary. Pinning the same version here means the base does not have to
        # reach the network for a pnpm download inside prepare.sh.
        pnpm = "RUN corepack enable && corepack prepare pnpm@10.23.0 --activate"

        # The clone RETRIES, and that is not defensive padding -- a plain
        # `git clone` lost a real multi-arch build on 2026-09-09:
        #
        #     #19 3650.1 error: 2574 bytes of body are still expected
        #     #19 ERROR: process "git clone ..." exit code: 128
        #
        # After 3,650 seconds the HTTP response was truncated and git gave up,
        # taking the whole run with it. openclaw carries ~4 GB of git history
        # and a multi-arch build clones it ONCE PER ARCHITECTURE, concurrently,
        # so on a slow link the two transfers share the bandwidth and each
        # stream stays open for well over an hour. That is a long time for a
        # single connection to survive.
        #
        # Cloning from the local copy in `repos/` instead is NOT available
        # here, and that was checked rather than assumed: build_dataset.py:585
        # stages the repo into the build context only when
        # `not isinstance(dep, str)`, i.e. for PR images. This base's
        # dependency() returns a string, so a COPY would find nothing. The
        # `need_clone=False` branch that other configs carry in their base is
        # dead code for exactly that reason.
        #
        # So the transfer cannot be avoided, only made survivable:
        #   lowSpeedLimit/lowSpeedTime  abort a stalled transfer after 5 min so
        #                               a retry can start, instead of hanging
        #   postBuffer 500 MB           fewer chunk boundaries across 4 GB
        #   5 attempts, rm -rf between  a dropped connection costs one attempt
        #   test -d .git                fails loudly if all 5 fail, so a
        #                               half-built base can never ship
        #
        # ONE LINE on purpose. A multi-line RUN inside a Python template needs
        # every trailing backslash doubled, and one missed backslash silently
        # collapses the command -- a trap this project has hit before.
        #
        # The `"${REPO_URL}"` spelling is preserved exactly: it is what trips
        # the negative lookahead in DockerfileEnhancer._standardize_repo_fetch,
        # so the clone is never rewritten into a pinned checkout.
        repo = self.pr.repo
        clone = (
            "RUN git config --global http.lowSpeedLimit 1000 "
            "&& git config --global http.lowSpeedTime 300 "
            "&& git config --global http.postBuffer 524288000 "
            "&& for i in 1 2 3 4 5; do "
            f"rm -rf /home/{repo}; "
            f'git clone "${{REPO_URL}}" /home/{repo} && break; '
            'echo "clone attempt $i failed, retrying in 30s"; sleep 30; '
            "done; "
            f"test -d /home/{repo}/.git"
        )

        # SHELL is set because the repo's own tests read it. src/agents/
        # shell-utils.ts resolves the interactive shell from `process.env.SHELL`
        # and src/agents/bash-tools.test.ts assigns that value back into the
        # environment before spawning. A Docker RUN layer has no SHELL at all,
        # and assigning an undefined value to process.env in node stores the
        # STRING "undefined", so the spawn became:
        #
        #     Error: spawn undefined ENOENT
        #     { syscall: 'spawn undefined', path: 'undefined',
        #       spawnargs: [ '-c', 'sleep 0.05; echo notify' ] }
        #
        # measured in-container on 2026-09-08. GitHub's runners always export
        # SHELL, so setting it here makes the image match the environment the
        # repo's CI actually tests in, rather than papering over a real failure.
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_img}

{infra}

WORKDIR /home/

{apt}

{pnpm}

ENV CI=true
ENV NO_COLOR=1
ENV FORCE_COLOR=0
ENV SHELL=/bin/bash
ENV HOME=/root
ENV PNPM_HOME=/root/.local/share/pnpm
ENV npm_config_update_notifier=false

{clone}

CMD ["/bin/bash"]
"""


class OpenclawEraImageDefault(Image):
    """Per-PR image (`pr-<N>`): COPY, prepare, then the full history scrub."""

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
        return OpenclawEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH),
            File(".", "vitest_test_report.py", _VITEST_TEST_REPORT_PY),
            File(".", "run_tests.sh", _run_tests_sh(self.pr)),
            File(
                ".",
                # prepare.sh follows rule 8's structure:
                #
                #     dependency installs, cd /home/<repo>, git reset --hard,
                #     git checkout <sha>, with check_git_changes.sh asserts
                #     around it
                #
                # so `git reset --hard` appears ONCE, before the checkout, and
                # the assert appears on both sides of the pin.
                #
                # The ORDER is checkout first, install second, which is the one
                # place this differs from a language whose deps are independent
                # of the tree. pnpm reads package.json and pnpm-lock.yaml from
                # the working tree, so installing before the pin would install
                # the default branch's dependency set and then leave it stale.
                #
                # Emitted WITHOUT comments (rule 10). The reasoning lives here:
                #
                #  * the pin needs no fetch-back in the normal case, because
                #    the base kept full git history (rule 9). The `cat-file -e`
                #    guard with a fetch fallback is kept anyway: all twenty base
                #    commits sit on `main`, and a force-push upstream would
                #    otherwise turn a missing object into a confusing checkout
                #    error at build time.
                #  * `--frozen-lockfile` first, because the lockfile is what
                #    makes the install reproducible. The retry exists because
                #    this graph downloads prebuilt native binaries in
                #    postinstall (sharp, node-llama-cpp, the matrix crypto
                #    binding), and a single flaky download should not cost the
                #    whole image. Only the third attempt relaxes the lockfile.
                #  * `--config.enable-pre-post-scripts=true` is required: those
                #    postinstall steps are what put the native bindings in
                #    place, and without them test files that import them fail
                #    to load.
                #  * the chain ends in `|| true`, which the QC prompt requires
                #    (check 3A) because a native module that will not build on
                #    one architecture is common and non-fatal -- sharp,
                #    node-llama-cpp and the matrix crypto binding all run
                #    postinstall steps here. Swallowing the exit code alone
                #    would be dangerous, so the REAL assert is the next block:
                #    if the install did not produce node_modules/.bin/vitest
                #    the image build fails loudly. A partially-installed tree
                #    therefore survives; an unusable one does not. Without that
                #    assert a broken install would reach the acts and read as a
                #    mass test failure rather than as a broken image.
                #  * `pnpm store prune` runs BEFORE that assert, not after.
                #    pnpm hardlinks node_modules into a content-addressable
                #    store, so a prune is the one step here that could plausibly
                #    damage an install that had already succeeded. Putting it
                #    ahead of the assert means the assert covers it. Actually
                #    executing `vitest --version` rather than only testing the
                #    file bit is the same idea one level deeper: a dangling
                #    hardlink is executable and still cannot run.
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags origin {pr.base.sha} 2>/dev/null \\
    || git fetch --no-tags origin 2>/dev/null \\
    || true
git cat-file -e {pr.base.sha}^{{commit}}

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
bash /home/check_git_changes.sh

node --version
pnpm --version

CI=true pnpm install --frozen-lockfile --ignore-scripts=false --config.engine-strict=false --config.enable-pre-post-scripts=true \\
    || CI=true pnpm install --frozen-lockfile --ignore-scripts=false --config.engine-strict=false --config.enable-pre-post-scripts=true \\
    || CI=true pnpm install --no-frozen-lockfile --ignore-scripts=false --config.engine-strict=false --config.enable-pre-post-scripts=true \\
    || true

pnpm store prune > /dev/null 2>&1 || true

if [ ! -x node_modules/.bin/vitest ]; then
  echo "prepare.sh: pnpm install did not produce node_modules/.bin/vitest"
  exit 1
fi
node_modules/.bin/vitest --version

git reset --hard
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                # `set -e` is what makes the patch step FATAL. Without it a
                # patch that failed to apply would fall through to
                # run_tests.sh, which would report clean baseline numbers as
                # though the test patch had landed -- a wrong report that looks
                # perfectly valid.
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                # Same reason as test-run.sh: a silently-skipped patch here
                # would produce a report that credits nothing and blames the
                # config.
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # ARG with a hardcoded default, because this layer's dependency() is an
        # Image and Image-dependency layers receive NO build args
        # (build_dataset.py:623-628). The hardening block below reads it.
        #
        # The block runs AFTER prepare.sh: prepare needs the network and the
        # remote for the pnpm install and for the fetch fallback, and the scrub
        # removes the remote.
        return f"""FROM {image_name}

{copies}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("openclaw", "openclaw_12091_to_8388")
class OPENCLAW_12091_TO_8388(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenclawEraImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section vitest_test_report.py printed. The raw
        # vitest output sits in the same log and its own summary lines also
        # carry the words FAILED and PASSED, so a whole-log scan would invent
        # tests that do not exist.
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what vitest_test_report.py prints. The
        # name is captured non-greedily BEFORE the keyword so nothing can leak
        # into it and manufacture a false transition between acts.
        result_res = [
            (re.compile(r"^(.+?)\s+PASSED$"), "pass"),
            (re.compile(r"^(.+?)\s+FAILED$"), "fail"),
            (re.compile(r"^(.+?)\s+SKIPPED$"), "skip"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            for rx, kind in result_res:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                if kind == "pass":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif kind == "fail":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                else:
                    skipped_tests.add(name)
                break

        # TestResult requires the three sets to be disjoint, else it raises.
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
