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


# apply_patch.sh -- plain `git apply` first, `--3way` only as a fallback, so a
# genuine apply failure is still counted from the primary attempt. Neither
# patch in this instance carries binary hunks, so there is nothing to lift out
# of git blobs before applying.
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/surrealdb
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


# cargo_test_report.py -- turn one `cargo test` run into per-test results.
#
# THE CRASH PROBLEM (why this is not a plain libtest-line scraper):
# PR 7121 fixes a stack overflow inside the `SurrealValue` derive macro's
# generated `kind_of()`. Before the fix, `RecursiveEnum::kind_of()` recurses
# until the thread's stack is exhausted, and Rust's stack-overflow handler
# calls `abort()` -- which kills the WHOLE test process, not just the offending
# test. libtest therefore never prints a `test <name> ... FAILED` line for it,
# and every test that had not yet run prints nothing at all.
#
# A naive scraper would see the "test" act produce only the handful of lines
# printed before the crash, classify the crashed test as NONE rather than
# FAILED, and -- worse -- silently lose the tests that never got to run. The
# F2P signal this instance exists to capture would be destroyed.
#
# So the runner enumerates the expected tests up front (`--list`) and passes
# that manifest here. Any test that was enumerated but produced no result line
# is reported FAILED, with the reason recorded. That is the truthful reading:
# the binary was asked to run it and the run did not complete.
#
# Names are qualified with the binary name (hash stripped, since it changes on
# every recompile) plus the source path, so a name is byte-identical across the
# three acts -- a name that drifts between acts manufactures a false F2P.
_CARGO_TEST_REPORT_PY = '''"""Turn one `cargo test` run into per-test results.

usage: cargo_test_report.py [--print-missing] <cargo-output> [<expected-manifest>]

With --print-missing, print the "<binary>\\t<source>\\t<name>" records for
expected tests that produced NO result line, and exit -- this is what
run_tests.sh feeds into its isolation re-run. Without it, emit the per-test
report described below.

Emits one line per test in the trailing-keyword form parse_log reads:

    cargo:surreal_value_tests::tests/surreal_value_tests.rs > derive::x PASSED
    cargo:surreal_value_tests::tests/surreal_value_tests.rs > derive::y FAILED

<expected-manifest> is the `cargo test -- --list` enumeration captured BEFORE
the run (one "<binary>\\t<source>\\t<test name>" record per line). Any expected
test with no result line in the output is emitted FAILED -- that is what a
process-aborting stack overflow looks like from the outside, and it is the
whole reason this instance's fix is observable.

Exit status mirrors a test runner: 0 = everything passed, 1 = at least one test
failed, 2 = the run produced no tests AT ALL (the runner never started).
"""
import re
import sys

ANSI = re.compile(r"\\x1b\\[[0-9;]*[a-zA-Z]")
RUNNING = re.compile(
    r"^\\s*Running (?:unittests )?(\\S+) \\((?:.*/)?([A-Za-z0-9_]+)-[0-9a-f]+\\)\\s*$"
)
DOC_TESTS = re.compile(r"^\\s*Doc-tests (\\S+)\\s*$")
RESULT = re.compile(r"^test (.+?) \\.\\.\\. (ok|FAILED|ignored)\\b")

# NOTE: the trailing " (line NNN)" on a doctest name is deliberately KEPT.
# `--list` and the real run both print it, so stripping it here would (a) make
# every manifest key miss its result key and mark all 16 doctests FAILED, and
# (b) collapse the nine distinct README.md doctests onto ONE key, silently
# discarding eight real results.

STATUS = {"ok": "PASSED", "FAILED": "FAILED", "ignored": "SKIPPED"}


def load_expected(path):
    """Read the --list manifest into ordered [(key, binary, source, name)]."""
    expected = []
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except IOError:
        return expected
    with fh:
        for line in fh:
            parts = line.rstrip("\\n").split("\\t")
            if len(parts) != 3:
                continue
            binary, source, name = parts
            key = "cargo:%s::%s > %s" % (binary, source, name)
            expected.append((key, binary, source, name))
    return expected


def scan(log):
    """Scan cargo output into ordered [(key, status)] plus the key set."""
    target, seen, results = "unknown", set(), []
    for line in log.splitlines():
        m = RUNNING.match(line)
        if m:
            target = "%s::%s" % (m.group(2), m.group(1))
            continue
        m = DOC_TESTS.match(line)
        if m:
            target = "%s::doc" % m.group(1)
            continue
        m = RESULT.match(line)
        if not m:
            continue
        name = m.group(1)
        status = STATUS[m.group(2)]
        key = "cargo:%s > %s" % (target, name)
        # LAST result wins, not first: the isolation re-run appends a second,
        # authoritative result for a test the bulk run left unreported or
        # mis-scored. Recording it here overwrites the earlier entry in place.
        if key in seen:
            for i, (k, _s) in enumerate(results):
                if k == key:
                    results[i] = (key, status)
                    break
            continue
        seen.add(key)
        results.append((key, status))
    return results, seen


def main():
    argv = sys.argv[1:]
    print_missing = False
    if argv and argv[0] == "--print-missing":
        print_missing = True
        argv = argv[1:]

    with open(argv[0], "r", encoding="utf-8", errors="replace") as fh:
        log = ANSI.sub("", fh.read())

    expected = load_expected(argv[1]) if len(argv) > 1 else []
    results, seen = scan(log)

    if print_missing:
        for key, binary, source, name in expected:
            if key not in seen:
                print("%s\\t%s\\t%s" % (binary, source, name))
        return 0

    failed = any(status == "FAILED" for _k, status in results)

    # Any test the manifest promised that even the isolation re-run could not
    # produce a result for did not complete -- its binary never built. That is
    # a failure of that test, not an absence.
    missing = 0
    for key, _binary, _source, _name in expected:
        if key in seen:
            continue
        seen.add(key)
        results.append((key, "FAILED"))
        failed = True
        missing += 1

    if missing:
        sys.stderr.write(
            "cargo_test_report: %d expected test(s) produced no result line; "
            "reporting them FAILED (binary did not build)\\n"
            % missing
        )

    if not results:
        sys.stderr.write(
            "cargo_test_report: no test executed; the crate did not compile "
            "or the runner never started\\n")
        return 2

    for key, status in results:
        print("%s %s" % (key, status))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


# list_tests.sh -- enumerate the tests the run is ABOUT to execute.
#
# `cargo test -- --list` builds the same test binaries the real run uses and
# asks each to print its test names without running them. Enumeration cannot
# stack-overflow, because the overflow lives in test BODIES, not in
# registration -- so this manifest survives the very crash it exists to
# describe. `--format terse` gives "<name>: test" lines under the same
# "Running <src> (<binary>)" headers the real run prints, so the same
# binary/source qualification applies and the keys line up exactly.
_LIST_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/surrealdb
export CARGO_TERM_COLOR=never

RAW=/tmp/cargo_list.out
: > "$RAW"
cargo test --locked --offline -p surrealdb-types --no-fail-fast -- --list --format terse >> "$RAW" 2>&1 || true

python3 - "$RAW" "$1" <<'PYEOF'
import re, sys

ANSI = re.compile(r"\\x1b\\[[0-9;]*[a-zA-Z]")
RUNNING = re.compile(
    r"^\\s*Running (?:unittests )?(\\S+) \\((?:.*/)?([A-Za-z0-9_]+)-[0-9a-f]+\\)\\s*$"
)
DOC_TESTS = re.compile(r"^\\s*Doc-tests (\\S+)\\s*$")
# `--list --format terse` prints "<name>: test" (and "<name>: benchmark").
ENTRY = re.compile(r"^(.+): test$")

with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as fh:
    log = ANSI.sub("", fh.read())

target = "unknown"
rows, seen = [], set()
for line in log.splitlines():
    m = RUNNING.match(line)
    if m:
        target = (m.group(2), m.group(1))
        continue
    m = DOC_TESTS.match(line)
    if m:
        target = (m.group(1), "doc")
        continue
    m = ENTRY.match(line)
    if not m or target == "unknown":
        continue
    name = m.group(1)
    key = (target[0], target[1], name)
    if key in seen:
        continue
    seen.add(key)
    rows.append(key)

with open(sys.argv[2], "w", encoding="utf-8") as out:
    for binary, source, name in rows:
        out.write("%s\\t%s\\t%s\\n" % (binary, source, name))

sys.stderr.write("list_tests: enumerated %d test(s)\\n" % len(rows))
PYEOF
"""


# run_tests.sh -- the ONE graded command, identical in all three acts.
#
# Scope is `-p surrealdb-types`, the crate that owns both halves of this PR:
# the gold test file (surrealdb/types/tests/surreal_value/derive/mod.rs) and,
# through its `surrealdb-types-derive` proc-macro dependency, every file the
# fix patch touches. Building the whole workspace would additionally compile
# surrealdb-core/server (the storage engines, the query planner) -- none of
# which this fix can affect, at a cost of hours per act.
#
# --test-threads=1 is deliberate: libtest's default is one thread per test, and
# a stack overflow in ANY of them aborts the shared process. Serialising does
# not prevent the abort -- nothing can -- but it makes the ORDER deterministic,
# so the same tests are reached in every act. RUST_MIN_STACK is left at the
# default: the overflow under test must remain reachable.
#
# THE ABORT-CASCADE PASS (the reason this script has a second phase):
# `test_recursive_*::kind_of()` overflows the stack, and Rust's overflow
# handler aborts the PROCESS. Every test after it in that binary -- ~97
# long-standing, unrelated tests like test_simple_struct -- never runs and so
# never prints a result. Left there, those tests look FAILED in the "test" act
# and PASSED in the "fix" act, and the report credits ~91 phantom F2Ps to a fix
# that actually adds 8 tests. The F2P set would be almost entirely noise.
#
# So: after the bulk run, any enumerated test with no result line is re-run in
# its OWN process, one at a time. A test that is merely collateral passes there
# and is recorded PASSED; the genuinely-overflowing test aborts its private
# process too and is recorded FAILED, which is the truth. Only tests that
# survive even that -- their binary never built -- fall through to the
# reporter's FAILED default.
#
# Each step captures its own exit code rather than aborting the script, so a
# crash never discards the enumeration or the reporting step; the real verdict
# comes from cargo_test_report.py.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/surrealdb
export CARGO_TERM_COLOR=never
export RUST_BACKTRACE=1

OUT=/tmp/cargo_test.out
EXPECTED=/tmp/cargo_expected.tsv
: > "$OUT"
: > "$EXPECTED"

bash /home/list_tests.sh "$EXPECTED"

echo "----- cargo test -p surrealdb-types -----" >> "$OUT"
cargo test --locked --offline -p surrealdb-types --no-fail-fast -- --test-threads=1 >> "$OUT" 2>&1
rc=$?
echo "bulk cargo test exit=${rc}" >&2

# Second phase: isolate whatever the bulk run left unreported.
MISSING=/tmp/cargo_missing.tsv
python3 /home/cargo_test_report.py --print-missing "$OUT" "$EXPECTED" > "$MISSING" 2>/dev/null || true
missing_n=$(wc -l < "$MISSING" | tr -d ' ')
echo "unreported after bulk run: ${missing_n}" >&2

if [ "${missing_n}" -gt 0 ]; then
  echo "----- isolation re-run of ${missing_n} unreported test(s) -----" >> "$OUT"
  while IFS=$'\\t' read -r binary source name; do
    [ -z "${name:-}" ] && continue
    # Re-emit the header the reporter keys on, reusing the binary's real
    # filename so the hash-stripping regex produces the identical key.
    bin_path=$(ls -1 target/debug/deps/${binary}-* 2>/dev/null \\
                 | grep -v '\\.d$' | head -n1)
    if [ -z "${bin_path}" ]; then
      echo "isolation: no binary found for ${binary}, leaving ${name} unreported" >&2
      continue
    fi
    echo "     Running ${source} (${bin_path})" >> "$OUT"
    "${bin_path}" --exact "${name}" --test-threads=1 --nocapture >> "$OUT" 2>&1
    trc=$?
    # A process that aborts (stack overflow => SIGABRT/SIGSEGV, exit >128)
    # prints no libtest result line. Synthesise the FAILED line libtest never
    # got to print, so the crash is recorded as this test's own failure.
    if [ "${trc}" -ne 0 ]; then
      echo "test ${name} ... FAILED" >> "$OUT"
      echo "isolation: ${name} exited ${trc} (recorded FAILED)" >&2
    fi
  done < "$MISSING"
fi

cat "$OUT"
echo "cargo test exit=${rc}"
echo "expected tests enumerated: $(wc -l < "$EXPECTED")"
echo "----- per-test results -----"
python3 /home/cargo_test_report.py "$OUT" "$EXPECTED"
"""


class SurrealdbImageBase(Image):
    """Per-PR base image (`base-pr-<N>`).

    dockerfile() is deliberately NOT overridden. The harness's own
    Image.dockerfile() already emits the exact required shape -- apt toolchain
    -> clone "${REPO_URL}" -> checkout ${BASE_COMMIT} -> extra_setup ->
    history-scrub with all four integrity asserts -> submodule scrub -> CMD --
    and DockerfileEnhancer prepends the standard infra block (syntax pin,
    TARGETARCH/REPO_URL/BASE_COMMIT ARGs, proxy ARGs, ENV block, OCI labels,
    CA-cert symlink farm) because dependency() returns a str. Overriding
    dockerfile() here would bypass the apt block and the ordering guarantees.

    rust:1.91 matches the repo's own rust-toolchain.toml at this commit
    (`channel = "1.91"`), so the image's toolchain IS the one under test.
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
        return "rust:1.91"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Emit the canonical base-image structure, and nothing else.

        Deliberately overridden rather than inheriting Image.dockerfile(): the
        inherited version injects an apt-install block and an extra_setup()
        hook between the CA-cert farm and the clone. The required structure has
        no such block -- it goes CA farm -> WORKDIR /home/ -> clone -> checkout
        -> history scrub -> CMD -- so this returns exactly that sequence.

        Dropping the apt layer costs nothing here, verified rather than
        assumed: `rust:1.91` (Debian 13 trixie) already ships git 2.47.3,
        python3 3.13.5, cc and pkg-config, which is the entire set the clone,
        the history scrub, and the report scripts rely on. A from-scratch
        `cargo test -p surrealdb-types --no-run` on the untouched base image
        links all six test binaries, so none of the previously-installed
        -dev packages (libssl-dev, clang, libclang-dev, cmake) is load-bearing
        for the graded crate.

        DockerfileEnhancer still prepends the infrastructure block (syntax
        pin, TARGETARCH/REPO_URL/BASE_COMMIT ARGs, proxy ARGs, ENV block, OCI
        labels, CA-cert symlink farm) because dependency() returns a str, and
        it leaves the body below untouched.
        """
        base_img = self.dependency()
        repo = self.pr.repo

        return f"""FROM {base_img}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

CMD ["/bin/bash"]
"""


class SurrealdbImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and the act scripts, run
    prepare.sh -- and nothing else. The clone, the checkout and the history
    scrub already happened in the base."""

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
        return SurrealdbImageBase(self.pr, self._config)

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
            File(".", "cargo_test_report.py", _CARGO_TEST_REPORT_PY),
            File(".", "list_tests.sh", _LIST_TESTS_SH),
            File(".", "run_tests.sh", _RUN_TESTS_SH),
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

export CARGO_TERM_COLOR=never
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export CARGO_INCREMENTAL=0
rustc --version
cargo --version

# The base image carries no warm cargo cache (its structure is clone +
# checkout + scrub only), so this layer does the fetch and the warm-up build
# itself. `cargo fetch --locked` populates the registry for the whole
# workspace lockfile, which is what lets every graded act run `--offline` and
# stay independent of the network. Building the test binaries here means the
# three acts only rebuild what the patches actually change.
#
# NOT `|| true`: a warm-up that fails quietly would ship an image with an
# empty cache and leave every act at the mercy of the network.
cargo fetch --locked
cargo build --locked --offline --tests -p surrealdb-types
cargo test --locked --offline -p surrealdb-types --no-run
# Incremental state only speeds up repeated edit-rebuild cycles on a
# developer's machine; here every act starts from the same committed tree, so
# it is ~200 MB of pure image weight.
rm -rf target/debug/incremental

git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true

bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true

bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uo pipefail
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

        return f"""FROM {image_name}

{copies}
RUN bash /home/prepare.sh

"""


@Instance.register("surrealdb", "surrealdb")
class Surrealdb(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SurrealdbImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured status keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section the report script printed. The RAW cargo
        # output sits in the same log and libtest's own failure line --
        # `test <name> ... FAILED` -- also ends in FAILED, so a whole-log scan
        # would invent a second, bogus "test" for every real failure.
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what cargo_test_report.py prints. The
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
