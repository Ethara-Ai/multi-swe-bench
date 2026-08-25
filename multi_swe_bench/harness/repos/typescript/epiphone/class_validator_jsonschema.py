import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


# apply_patch.sh -- the dataset's diffs carry an index hash but no binary
# payload. Measured on this dataset: 0 binary hunks in both patches, so the
# restore path never fires here; it is kept because it costs nothing. Plain
# `git apply` first, `--3way` only as a fallback -- the primary apply is what a
# failure must be counted from (FLOW VERDICT DISCIPLINE).
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/class-validator-jsonschema
EXCL=/tmp/excl.$$
restore_binaries() {
  local patch="$1" path="" new=""
  : > "$EXCL"
  while IFS= read -r line; do
    case "$line" in
      "diff --git "*) path="${line#*" b/"}" ;;
      "index "*)      new="${line#*..}"; new="${new%% *}" ;;
      "Binary files "*)
        printf -- '--exclude=%s\\n' "$path" >> "$EXCL"
        if [[ "$new" =~ ^0+$ ]]; then rm -f "$path"
        elif git cat-file -e "$new" 2>/dev/null; then
          mkdir -p "$(dirname "$path")"; git cat-file blob "$new" > "$path"
        else
          echo "apply_patch: WARNING blob $new for $path not available"
        fi ;;
    esac
  done < "$patch"
}
for patch in "$@"; do
  restore_binaries "$patch"
  EX=()
  if [ -s "$EXCL" ]; then mapfile -t EX < "$EXCL"; fi
  if ! git apply --whitespace=nowarn "${EX[@]}" "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "${EX[@]}" "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
rm -f "$EXCL"
"""


# jest_test_report.py -- turn Jest's machine-readable result file into the
# trailing-keyword lines parse_log reads.
#
# Why the JSON and not the console reporter: jest's verbose output encodes the
# suite path in a "PASS <file>" banner, the describe nesting in INDENTATION, and
# appends a per-test duration -- three things that must be reassembled by regex
# while workers interleave the blocks. `--json --outputFile` hands the same
# information over already structured (absolute path, ancestorTitles, title),
# with no ANSI and no duration, so a name is byte-identical across the three
# acts (FLOW audit 4B).
_JEST_TEST_REPORT_PY = '''"""Turn one Jest run into per-test results.

usage: jest_test_report.py <jest-json> <repo-root>

Emits one line per assertion, in the trailing-keyword form parse_log reads:

    jest:__tests__/options.test.ts > options > uses a custom schema PASSED

Exit status mirrors a test runner: 0 = everything passed, 1 = at least one test
failed, 2 = the run produced no tests at all (the runner never started -- NOT
"zero tests passed"). 2 is the case FLOW GATE 0 exists to catch.

NOTE on this repo: a ts-jest SUITE that fails to COMPILE reports zero
assertionResults, so its tests are ABSENT rather than FAILED. That is a real
property of the runner, not of this script -- see CLASS-VALIDATOR-JSONSCHEMA_CHANGES.md.
The suite-level counters are echoed to stderr so a vanished suite is visible in
the log instead of silent.
"""
import json
import os
import sys

STATUS = {
    "passed": "PASSED",
    "failed": "FAILED",
    "pending": "SKIPPED",
    "skipped": "SKIPPED",
    "todo": "SKIPPED",
    "disabled": "SKIPPED",
}


def main():
    path, root = sys.argv[1], sys.argv[2]
    if not root.endswith(os.sep):
        root += os.sep

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("jest_test_report: cannot read %s: %s\\n" % (path, exc))
        return 2

    lines, failed = [], False
    for suite in data.get("testResults", []):
        name = suite.get("name", "")
        rel = name[len(root):] if name.startswith(root) else os.path.basename(name)
        results = suite.get("assertionResults", [])
        if not results:
            # A suite that failed to compile yields no assertions. Make it loud.
            sys.stderr.write(
                "jest_test_report: SUITE PRODUCED NO TESTS (compile failure?): %s\\n" % rel)
        for a in results:
            status = STATUS.get(a.get("status", ""), "FAILED")
            parts = [rel] + list(a.get("ancestorTitles", [])) + [a.get("title", "")]
            lines.append("jest:%s %s" % (" > ".join(p for p in parts if p), status))
            if status == "FAILED":
                failed = True

    sys.stderr.write(
        "jest_test_report: suites total=%s failed=%s | tests total=%s\\n"
        % (data.get("numTotalTestSuites"), data.get("numFailedTestSuites"),
           data.get("numTotalTests")))

    if not lines:
        sys.stderr.write(
            "jest_test_report: the run reported no tests; the runner never started\\n")
        return 2

    for line in lines:
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''



# tsc_report.py -- score "does this file type-check" as one test case per file.
#
# WHY THIS EXISTS (read before trusting the f2p it produces):
#
# PR #59 adds ZERO new jest tests -- it rewrites imports in 5 existing test
# files, and those rewrites need `esModuleInterop`, which the FIX supplies. With
# only the jest suite the PR scores f2p=0 and is a SKIP.
#
# FLOW Issue 20 permits a non-unit-test suite ("a linter with per-file output...
# any of them can be a suite if it yields per-item pass/fail"). This scores the
# repo's own CI type-check (`npm run build` -> tsc) per file, which DOES
# transition: 4 test files fail to compile in the test act and compile in the fix
# act.
#
# THE EVIDENCE THIS PRODUCES IS COMPILE-EVIDENCE, NOT BEHAVIOURAL EVIDENCE.
# It asserts that a file type-checks, NOT that the lodash replacement is correct.
# It is therefore NOT comparable to a behavioural f2p of the same size, and it is
# only defensible while the jest suite runs alongside it as a p2p guard -- tsc
# alone could be satisfied by a type-correct stub. See
# CLASS-VALIDATOR-JSONSCHEMA_CHANGES.md 7 for the full argument, including the
# reasons AGAINST using it.
#
# Name stability (audit 4B): the scored set is `git ls-files src/*.ts
# __tests__/*.ts`, and neither patch adds or deletes a .ts file (measured: 0 new,
# 0 deleted in both), so all 13 names are byte-identical across the three acts.
# node_modules diagnostics are excluded -- they are not repo files.
_TSC_REPORT_PY = '''"""Score each tracked .ts file as PASSED/FAILED under `tsc --noEmit`.

usage: tsc_report.py <tsc-output-file>

Emits, for every file tsconfig covers:

    tsc:src/index.ts PASSED
    tsc:__tests__/options.test.ts FAILED

Exit 0 if every file type-checks, 1 if any file has a diagnostic, 2 if the file
list could not be determined (the runner never started -- FLOW GATE 0's case).
"""
import re
import subprocess
import sys

# `file.ts(line,col): error TSxxxx: message`  -- tsc's non-pretty format.
DIAG = re.compile(r"^(?P<file>[^(]+\\.ts)\\((?P<line>\\d+),(?P<col>\\d+)\\):\\s+error\\s+TS\\d+:")


def tracked_files():
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "src/*.ts", "__tests__/*.ts"],
            stderr=subprocess.DEVNULL).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.stderr.write("tsc_report: cannot list tracked files: %s\\n" % exc)
        return []
    return sorted(f.strip() for f in out.splitlines() if f.strip())


def main():
    files = tracked_files()
    if not files:
        sys.stderr.write("tsc_report: no tracked .ts files; the runner never started\\n")
        return 2

    bad = {}
    try:
        fh = open(sys.argv[1], "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.stderr.write("tsc_report: cannot read tsc output: %s\\n" % exc)
        return 2
    with fh:
        for line in fh:
            m = DIAG.match(line.strip())
            if not m:
                continue
            f = m.group("file")
            # Diagnostics inside node_modules are not repo files -- excluding
            # them keeps the scored set equal to the tracked set in every act.
            if "node_modules/" in f:
                continue
            bad.setdefault(f, 0)
            bad[f] += 1

    unknown = sorted(set(bad) - set(files))
    if unknown:
        sys.stderr.write(
            "tsc_report: diagnostics on %d untracked file(s), not scored: %s\\n"
            % (len(unknown), ", ".join(unknown)))

    sys.stderr.write("tsc_report: files=%d with-errors=%d\\n"
                     % (len(files), len([f for f in files if f in bad])))

    failed = False
    for f in files:
        if f in bad:
            print("tsc:%s FAILED" % f)
            failed = True
        else:
            print("tsc:%s PASSED" % f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''

# run_tests.sh -- the repo's own `npm test` is `jest --coverage`, and its CI
# (.github/workflows/test.yml) runs build + test:format + test:lint before it.
# Only jest yields per-test evidence; `test:format` (prettier --check) and
# `test:lint` (tslint) are whole-tree style gates, so a formatting nit anywhere
# in the PR's diff would abort the act before a single test ran and score the PR
# 0/0/0 -- the failure mode FLOW GATE 0 exists to catch. `--coverage` is dropped
# because it only adds time; it changes no test outcome.
#
# The jest binary is invoked from node_modules directly rather than through npx,
# so the act never consults the network to resolve it.
# TWO suites run here, and BOTH are load-bearing:
#
#   tsc  -> the repo's own CI type-check (`npm run build`), scored per file.
#           This is the suite that TRANSITIONS: 4 test files fail to compile in
#           the test act and compile in the fix act (FLOW Issue 20).
#   jest -> the repo's real behavioural suite, 19 tests.
#
# jest is NOT optional decoration. tsc alone asserts only that the code
# type-checks, which a type-correct stub would satisfy; the 19 jest tests run in
# every act as p2p GUARDS, so a "fix" that compiles but breaks behaviour is
# caught. Removing jest here would make the f2p meaningless. See
# CLASS-VALIDATOR-JSONSCHEMA_CHANGES.md 7.
#
# Neither command is `|| true`-guarded and neither aborts the act: `set -e` is
# absent (only -uo pipefail), because tsc exits non-zero in the test act BY
# DESIGN and jest exits non-zero whenever a suite fails. Under -e the act would
# die before the report scripts ran and score a silent 0/0/0 (GATE 0).
#
# `--pretty false` is required: tsc's pretty output is ANSI-decorated and wraps
# the `file(line,col): error TSxxxx` form that tsc_report.py keys on.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/class-validator-jsonschema
TSCOUT=/tmp/tsc.out
JSON=/tmp/jest.json
OUT=/tmp/jest.out
rm -f "$TSCOUT" "$JSON" "$OUT"
./node_modules/.bin/tsc --noEmit --pretty false -p tsconfig.json > "$TSCOUT" 2>&1
tsc_rc=$?
cat "$TSCOUT"
echo "tsc exit=${tsc_rc}"
./node_modules/.bin/jest --ci --json --outputFile="$JSON" > "$OUT" 2>&1
jest_rc=$?
cat "$OUT"
echo "jest exit=${jest_rc}"
echo "----- per-test results -----"
python3 /home/tsc_report.py "$TSCOUT"
python3 /home/jest_test_report.py "$JSON" /home/class-validator-jsonschema
"""


class ClassValidatorJsonschemaImageBase(Image):
    """Shared base image, one per PR (`base-pr-<N>`).

    dockerfile() is deliberately NOT overridden: the harness's own
    Image.dockerfile() already emits the mandated order (FROM -> apt -> clone ->
    WORKDIR -> reset -> checkout ${BASE_COMMIT} -> extra_setup -> hardening
    scrub + its four assertions -> CMD), and DockerfileEnhancer then prepends
    the BuildKit directive, the build ARGs (BASE_COMMIT left EMPTY), the env
    block, the labels and the cert links. Nothing commit-specific is written
    here: build_dataset passes BASE_COMMIT and REPO_URL as docker build args,
    read straight from the dataset.

    The tag carries the PR number because the base's CONTENT is per-PR (it is
    checked out at that PR's base commit). A shared `:base` tag would be one
    name for two different images -- FLOW Issue 25.
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

    # Node 14 is REQUIRED here, not merely preferred, and the reason is npm --
    # not the language runtime. This repo declares peerDependencies
    # (class-transformer, class-validator) that are absent from its committed
    # package-lock.json. npm 8 (Node 16+) auto-installs peer deps and therefore
    # refuses the lockfile as out of sync:
    #
    #   npm ERR! `npm ci` can only install packages when your package.json and
    #   package-lock.json ... are in sync.
    #   npm ERR! Missing: class-validator@0.12.2 from lock file
    #
    # npm 6 (Node 14) does not auto-install peers, so `npm ci` succeeds. The
    # repo's CI used `actions/setup-node@v1` with no version pinned, i.e. the
    # Node 14 default of that era. Measured on this tag: npm 6.14.18, `npm ci`
    # rc=0, 19/19 tests passing at base_commit. Publishes amd64 and arm64.
    def dependency(self) -> Union[str, "Image"]:
        return "node:14-bullseye-slim"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # The harness already installs ca-certificates, curl, build-essential,
        # git, gnupg, make, python3, sudo and wget. This suite is pure ts-jest --
        # no browser, no native addon -- so it needs nothing further.
        return []

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}`, so the lockfile is this PR's
        # own. Three things happen here and each is load-bearing:
        #
        # 1. `npm ci --ignore-scripts`. --ignore-scripts is required: the repo's
        #    `prepare` script runs `install-self-peers && npm run build`, and npm
        #    6 refuses lifecycle scripts as root ("cannot run in wd ..."), so it
        #    would be skipped anyway -- but silently, leaving a half-set-up tree.
        #    Skipping it explicitly and installing the peers ourselves is
        #    deterministic instead.
        #
        # 2. The peerDependencies, pinned. Without them EVERY suite fails to
        #    compile and the run reports 0 tests -- measured: 9 failed suites,
        #    0 tests. Versions come from package.json's own ranges
        #    (class-transformer "0.2.3 - 0.3.1", class-validator "^0.12.0").
        #
        # 3. The dependencies the FIX patch introduces. This PR replaces lodash
        #    with lodash.get/groupby/merge, so act 3 needs those AND their
        #    @types -- otherwise ts-jest fails with
        #    "TS7016: Could not find a declaration file for module 'lodash.get'"
        #    and act 3 collapses to 0 tests. Pre-installing them in the base
        #    keeps all three acts hermetic and offline.
        #
        # ALL of it is ONE `npm install`: npm 6 recomputes the tree on each
        # --no-save invocation and PRUNES packages added by a previous one, so
        # splitting this into several commands silently removes earlier
        # additions (measured: a second --no-save install removed jest itself).
        #
        # NOT `|| true`: this is the toolchain, not a nice-to-have cache. A
        # warm-up that fails quietly ships an image whose acts cannot run
        # (FLOW Issue 14, GATE 1). The version probes prove it took effect.
        return (
            "RUN npm ci --ignore-scripts && \\\n"
            "    npm install --no-save --ignore-scripts \\\n"
            '        "class-transformer@0.3.1" "class-validator@0.12.2" \\\n'
            '        "lodash.get@^4.4.2" "lodash.groupby@^4.6.0" "lodash.merge@^4.6.2" \\\n'
            '        "@types/lodash.get@^4.4.6" "@types/lodash.groupby@^4.6.6" \\\n'
            '        "@types/lodash.merge@^4.6.6" && \\\n'
            "    ./node_modules/.bin/jest --version && \\\n"
            "    node --version && \\\n"
            "    test -d node_modules/class-validator && \\\n"
            "    test -d node_modules/@types/lodash.get"
        )


class ClassValidatorJsonschemaImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and the act scripts, run
    prepare.sh -- and nothing else, which is the shape of
    Main_Tasks/pr_specific dockerfile.dockerfile. The clone, the checkout and
    the history scrub already happened in the base."""

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
        return ClassValidatorJsonschemaImageBase(self.pr, self._config)

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
            File(".", "tsc_report.py", _TSC_REPORT_PY),
            File(".", "jest_test_report.py", _JEST_TEST_REPORT_PY),
            File(".", "run_tests.sh", _RUN_TESTS_SH),
            # prepare.sh deliberately does NOT run `npm ci`.
            #
            # It did, and it silently destroyed the run. `npm ci` DELETES
            # node_modules and reinstalls from the lockfile -- which drops every
            # package the base added with `--no-save` (the two peerDependencies
            # and the six lodash/@types packages the fix needs). Measured:
            #
            #   base image : class-validator PRESENT  @types/lodash.get PRESENT
            #   pr image   : class-validator MISSING  @types/lodash.get MISSING
            #   -> every act reported 0 tests ("Cannot find module
            #      'class-transformer/storage'"), i.e. a silent 0/0/0.
            #
            # node_modules is gitignored, so `git clean -fdq` (no -x) leaves the
            # base's install intact. The `test -d` lines are the guard: if
            # node_modules is ever wiped again the IMAGE BUILD fails loudly
            # instead of the acts quietly scoring nothing (FLOW GATE 0).
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh
npm ls class-validator class-transformer >/dev/null 2>&1 || true
./node_modules/.bin/jest --version
node --version
test -d node_modules/class-validator
test -d node_modules/class-transformer
test -d node_modules/lodash.get
test -d node_modules/@types/lodash.get
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/class-validator-jsonschema
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/class-validator-jsonschema
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/class-validator-jsonschema
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

        return f"""FROM {image_name}

{copies}
RUN bash /home/prepare.sh

"""


@Instance.register("epiphone", "class-validator-jsonschema")
class ClassValidatorJsonschema(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ClassValidatorJsonschemaImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured status keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section the report script printed, when present.
        # The raw jest output shares the log, and its summary lines can end in a
        # status word, so a whole-log scan could invent tests. The fallback to
        # the whole text keeps a bare sequence of result lines parseable (the
        # config audit's 4C probe has no marker).
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what jest_test_report.py prints. The
        # name is captured non-greedily BEFORE the keyword, so no duration or
        # count can leak into it and manufacture a false transition between acts.
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
