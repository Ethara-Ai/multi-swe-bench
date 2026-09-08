"""surrealdb/surrealdb -- era config for PRs 1957 .. 2559 (May-Aug 2023).

Ten PRs, ONE era, so ONE shared base image (rule 4, row 2). Every base commit
in this range sits on `main` between v1.0.0-beta.9 and v1.0.0-beta.10, has the
same `lib/` workspace layout, and is built by the same toolchain:

    lib/Cargo.toml  rust-version = "1.70.0"
    .github/workflows/ci.yml  toolchain: 1.71.1     (no rust-toolchain.toml exists)

so the base is `rust:1.71-bookworm`, which IS the toolchain CI used at these
commits.

WHY `-bookworm` AND NOT PLAIN `rust:1.71`. The plain tag resolves to a Debian 11
(bullseye) image, and that distribution's apt pool no longer serves the versions
its own package index advertises. A real build on 2026-09-07 died on it:

    E: Failed to fetch .../git_2.30.2-1+deb11u5_arm64.deb  404  Not Found
    ERROR: failed to solve: ... apt-get install ... exit code: 100

Reproduced directly in the image: apt offers candidate `1:2.30.2-1+deb11u5`
while the mirror has already dropped that file. Debian 12 (bookworm) is still
supported and its pool is consistent, so the `-bookworm` variant is used. The
Rust toolchain inside is identical (1.71.1); only the base distribution moves.

Every graded test in this dataset lives in `lib/tests/<suite>.rs` -- integration
test targets of the `surrealdb` package. The three acts therefore run

    cargo test --locked --package surrealdb \
        --no-default-features --features kv-mem,scripting,http \
        --test select --test create --test field --test update \
        --test insert --test define --test geometry --test script

which is the CI feature set (`ci-workspace-coverage` uses the root package's
`storage-mem,scripting,http`, and those map onto the library's
`kv-mem,scripting,http`) narrowed to the eight suites this dataset touches.
Narrowing is deliberate: the package has ~40 integration targets and each one
links the whole `surrealdb` rlib, so compiling all of them would cost several
GB and a long build per PR image for tests no PR in this dataset grades.

`--locked` without `--offline`: the lockfile still pins every version, so the
build stays reproducible, but a crate that is missing from the warm cache can
still be fetched instead of killing the act. That pairing is what lets
prepare.sh end its fetch steps with `|| true` the way the checklist requires --
with `--offline` a half-warmed cache would be silently fatal three acts later.
prepare.sh still warms the cache for BOTH lockfile states, so in practice the
acts need no network: PR 2355's fix patch bumps globset, libz-sys and
rustls-webpki in Cargo.lock, and that is the only case where the two differ.

STRUCTURE -- rule 9 (2026-09-03), which supersedes rules 4/5/8 on placement:

    base Dockerfile   toolchain, infra block, apt, WORKDIR /home/, git clone,
                      CMD. NOTHING after the clone -- no checkout, no scrub.
    PR Dockerfile     FROM base, COPY lines, ARG BASE_COMMIT, RUN prepare.sh,
                      then the FULL hardening block with all four asserts.
    prepare.sh        dependency fetch, checkout, warm-up build. No stripping.

Two harness details make that placement work, and both were checked in
multi_swe_bench/harness/image.py rather than assumed:

  * `DockerfileEnhancer.enhance()` returns the Dockerfile untouched when it
    already carries the BuildKit syntax directive (image.py:316). The base
    below emits that directive itself, so the enhancer's
    `_inject_final_sanitize()` -- which would otherwise append the hardening
    block before the CMD, putting the scrub right back into the base -- never
    runs. The infrastructure block is therefore produced here by calling
    `DockerfileEnhancer._infrastructure_block()` directly, so it cannot drift
    away from what every other image gets.
  * The scrub now runs in an Image-dependency layer, and those get NO build
    args (build_dataset.py:623-628 passes REPO_URL/BASE_COMMIT only when
    `dependency()` returns a str). So the PR Dockerfile carries the sha itself
    as `ARG BASE_COMMIT="<sha>"`, the way trivy_3855_to_81.py does.

Check before running, inverted from rule 5 exactly as rule 9 requires:
`grep -c 'rev-list --all --count'` must be 0 in the base Dockerfile and 1 in
the PR Dockerfile.
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


# The eight `lib/tests/*.rs` suites this dataset grades. Verified present at
# BOTH ends of the range -- ccc16fa9 (PR 1957, May 2023) and 4c61e3a5 (PR 2559,
# Aug 2023) -- so no PR in the set loses a target. run_tests.sh still guards
# each one with a file-existence test, because a silently-dropped target would
# turn a real failure into "no result" rather than into an error.
_TEST_SUITES = "select create field update insert define geometry script"

# The ONE place the cargo invocation is spelled out. All three acts call
# run_tests.sh, so they cannot drift apart.
_CARGO_FEATURES = "kv-mem,scripting,http"


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
# being papered over. Neither patch in this dataset carries binary hunks, so
# there is nothing to lift out of git blobs before applying.
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
# Written as a RAW string literal so every backslash in the regexes below is
# the literal character that reaches the file. A non-raw literal here would
# need each one doubled, and a missed one is a silent parser change.
#
# Two jobs:
#
#   --list <file>   read a `cargo test -- --list` enumeration and print one
#                   "<src>\t<name>" record per test. run_tests.sh captures this
#                   BEFORE the graded run.
#   <run> <manifest>  read the run output and print the per-test report.
#
# Why the manifest exists: a test whose process dies (a panic that aborts, an
# OOM, a stack overflow) never prints its own `... FAILED` line, and every test
# queued behind it prints nothing at all. A plain line-scraper would report
# those as absent rather than failed, quietly deleting the very signal an F2P
# instance is built on. Any test that was enumerated and produced no result
# line is therefore reported FAILED: the binary was asked to run it and the run
# did not complete.
#
# Names are qualified with the SOURCE PATH, not the binary name, because the
# binary name carries a hash that changes on every recompile and a name that
# drifts between acts manufactures a false F2P.
_CARGO_TEST_REPORT_PY = r'''"""Turn one `cargo test` run into per-test results.

usage:
    cargo_test_report.py --list <cargo --list output>
    cargo_test_report.py <cargo run output> <expected manifest>

The report form emits one line per test in the trailing-keyword shape parse_log
reads:

    cargo::lib/tests/select.rs::select_field_value PASSED
    cargo::lib/tests/create.rs::create_with_id FAILED

Exit status mirrors a test runner: 0 = everything passed, 1 = at least one test
failed, 2 = the run produced no tests at all (the runner never started, which
usually means the crate did not compile).
"""
import os
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# `     Running tests/select.rs (target/debug/deps/select-9a1b2c3d4e5f6071)`
# `     Running unittests src/lib.rs (target/debug/deps/surrealdb-0011223344)`
RUNNING = re.compile(r"^\s*Running (?:unittests )?(\S+) \(")

# `test select_field_value ... ok`   /  `... FAILED`  /  `... ignored`
RESULT = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)\b")

# `--list` prints `select_field_value: test` under each Running line.
LISTED = re.compile(r"^(.+?): test$")

STATUS = {"ok": "PASSED", "FAILED": "FAILED", "ignored": "SKIPPED"}

REPO_ROOT = "/home/surrealdb"


def normalize_src(src):
    """Return the path of a test target RELATIVE TO THE REPO ROOT.

    cargo prints the target's source path, but whether that path is relative to
    the workspace root (`lib/tests/select.rs`) or to the package root
    (`tests/select.rs`) depends on the cargo version and on where it was
    invoked. Rather than guess, resolve it against the checkout: whichever
    candidate actually exists on disk is the right one. If neither exists the
    printed value is returned unchanged, so an unexpected layout degrades to a
    slightly odd id instead of to a wrong one.
    """
    for candidate in (src, os.path.join("lib", src)):
        if os.path.exists(os.path.join(REPO_ROOT, candidate)):
            return candidate.replace(os.sep, "/")
    return src


def read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield ANSI.sub("", line).rstrip("\n")
    except IOError:
        return


def emit_list(path):
    """Print one "<src>\t<name>" record per enumerated test."""
    src = None
    count = 0
    for line in read_lines(path):
        run = RUNNING.match(line)
        if run:
            src = normalize_src(run.group(1))
            continue
        listed = LISTED.match(line.strip())
        if listed and src is not None:
            sys.stdout.write("%s\t%s\n" % (src, listed.group(1)))
            count += 1
    return 0 if count else 2


def load_expected(path):
    """Read the manifest into an ordered list of (src, name), de-duplicated."""
    expected = []
    seen = set()
    for line in read_lines(path):
        if "\t" not in line:
            continue
        src, name = line.split("\t", 1)
        key = (src, name)
        if key in seen:
            continue
        seen.add(key)
        expected.append(key)
    return expected


def emit_report(run_path, manifest_path):
    results = {}
    order = []
    src = None

    for line in read_lines(run_path):
        run = RUNNING.match(line)
        if run:
            src = normalize_src(run.group(1))
            continue
        res = RESULT.match(line.strip())
        if not res or src is None:
            continue
        key = (src, res.group(1))
        status = STATUS[res.group(2)]
        if key not in results:
            order.append(key)
        # A repeated name within one act is resolved pessimistically: a FAILED
        # sighting is never overwritten by a later PASSED one.
        if results.get(key) != "FAILED":
            results[key] = status

    expected = load_expected(manifest_path)
    missing = [key for key in expected if key not in results]
    for key in missing:
        results[key] = "FAILED"
        order.append(key)

    if not results:
        sys.stderr.write(
            "cargo_test_report: NO tests produced a result and none were "
            "enumerated -- the test runner never started.\n"
        )
        return 2

    for src, name in order:
        sys.stdout.write("cargo::%s::%s %s\n" % (src, name, results[(src, name)]))

    if missing:
        sys.stderr.write(
            "cargo_test_report: %d enumerated test(s) produced no result line "
            "and are reported FAILED:\n" % len(missing)
        )
        for src, name in missing:
            sys.stderr.write("  %s::%s\n" % (src, name))

    return 1 if any(v == "FAILED" for v in results.values()) else 0


def main(argv):
    if len(argv) >= 3 and argv[1] == "--list":
        return emit_list(argv[2])
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
# run_tests.sh -- the ONE definition of the graded test command, emitted
# WITHOUT comments by request. What the bare script no longer says:
#
#  * `set -e` is deliberately ABSENT here, and ONLY here. `cargo test` exits
#    non-zero whenever a test fails, which is the normal outcome of the
#    baseline and test acts. With `-e` the script would die at that line and
#    never print the per-test report, so parse_log would see nothing and the
#    instance would be discarded. The three ACT scripts that call this one DO
#    use `set -eo pipefail`, so a failed patch apply still aborts before here.
#  * the TARGETS loop guards each suite with a file-existence test. All eight
#    exist across the whole 1957..2559 range, so it should never fire; it is
#    there so a missing target is reported loudly rather than silently
#    shrinking the test set.
#  * `--list` runs first for each suite because it also does the compile, so the
#    graded run reuses the same build artefacts.
#  * cargo is never piped. A pipeline would hand back the exit code of the LAST
#    command rather than cargo's, and a failed run would read as a clean one.
#  * ONE CARGO INVOCATION PER SUITE, not one call carrying eight `--test` flags.
#    This matters and was a real defect. `--no-fail-fast` keeps cargo going past
#    a failing TEST, but a COMPILE error aborts the entire invocation -- and a
#    test patch that calls an API the fix introduces is exactly that. On PR 2465
#    the test patch calls `Session::for_scope`, which `fix.patch` adds to
#    lib/src/dbs/session.rs, so `lib/tests/field.rs` cannot build. With a single
#    invocation that killed all eight suites: the test act captured ZERO
#    results, and report.py had to fall back to the run act for every one of the
#    99 P2P tests (`reclassified_from_target` = 99). Per-suite invocation
#    confines the damage to the suite that genuinely cannot compile -- measured
#    in-container: 0 results -> 92 results, with only field.rs still absent.
#  * `--no-fail-fast` is LOAD-BEARING, and its absence was a real defect caught
#    on the first full run. `cargo test` runs the eight `--test` targets in
#    sequence and, by default, STOPS at the first target that fails:
#
#        test result: FAILED. 6 passed; 4 failed; ...
#        error: test failed, to rerun pass `-p surrealdb --test create`
#
#    In the TEST act that is guaranteed to happen -- the whole point of that
#    act is that test.patch introduces failures. On PR 2534 only create.rs ran
#    and the other seven suites never executed at all. The reporter correctly
#    called those enumerated-but-absent tests FAILED, so they all "recovered"
#    in the fix act and the instance reported 100 F2P tests instead of 4.
#    Valid-looking, and completely wrong. With --no-fail-fast every target runs
#    in every act, so the three acts compare like with like.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

cd /home/surrealdb

export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_BACKTRACE=1

CARGO_ARGS=(test --locked --no-fail-fast --package surrealdb --no-default-features --features {features})

OUT=/tmp/cargo_test.out
LIST=/tmp/cargo_test.list
EXPECTED=/tmp/expected_tests.tsv
: > "$OUT"
: > "$LIST"

rc=0
for suite in {suites}; do
  if [ ! -f "lib/tests/$suite.rs" ]; then
    echo "run_tests: WARNING lib/tests/$suite.rs is absent at this commit, skipping" >> "$OUT"
    continue
  fi
  cargo "${{CARGO_ARGS[@]}}" --test "$suite" -- --list >> "$LIST" 2>&1
  cargo "${{CARGO_ARGS[@]}}" --test "$suite" >> "$OUT" 2>&1
  suite_rc=$?
  if [ "$suite_rc" -ne 0 ]; then
    rc=$suite_rc
    echo "run_tests: suite $suite exited $suite_rc" >> "$OUT"
  fi
done

python3 /home/cargo_test_report.py --list "$LIST" > "$EXPECTED"

cat "$OUT"
echo "cargo test exit=${{rc}}"
echo "expected tests enumerated: $(wc -l < "$EXPECTED")"
echo "----- per-test results -----"
python3 /home/cargo_test_report.py "$OUT" "$EXPECTED"
""".format(suites=_TEST_SUITES, features=_CARGO_FEATURES)


class SurrealdbEraImageBase(Image):
    """Shared era base (`base-surrealdb_2559_to_1957`): toolchain + clone only.

    Rule 9: nothing after the `git clone`. The image therefore keeps the FULL
    git history, which is exactly what makes one base safe for all ten PRs --
    no commit can be missing, so no PR has to fetch anything back. The pin and
    the prune happen per PR, in the PR layer.
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
        # rust:1.71.x is the toolchain .github/workflows/ci.yml pins at every
        # commit in this range, and >= the lib's own 1.70.0 MSRV.
        #
        # The `-bookworm` suffix is load-bearing, not cosmetic: the default
        # rust:1.71 tag is Debian 11, whose apt pool 404s on the very package
        # versions its index advertises. See the module docstring for the exact
        # failure. Debian 12 is still supported and installs cleanly.
        return "rust:1.71-bookworm"

    def image_tag(self) -> str:
        return "base-surrealdb_2559_to_1957"

    def workdir(self) -> str:
        return "base-surrealdb_2559_to_1957"

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

        # apt on one line on purpose. A multi-line RUN in a Python template
        # needs every trailing backslash doubled, and one missed backslash
        # silently collapses the command -- a trap this project has been caught
        # by before.
        #
        # clang/libclang-dev: rquickjs 0.4.0-beta.4 runs bindgen for the
        # `scripting` feature. cmake/pkg-config/libssl-dev: cheap insurance for
        # native deps in this dependency graph.
        apt = (
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "ca-certificates curl git gnupg make python3 sudo wget build-essential "
            "clang libclang-dev cmake pkg-config libssl-dev "
            "&& rm -rf /var/lib/apt/lists/*"
        )

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_img}

{infra}

WORKDIR /home/

{apt}

ENV CARGO_TERM_COLOR=never
ENV CARGO_INCREMENTAL=0
ENV CARGO_PROFILE_DEV_DEBUG=0
ENV CARGO_PROFILE_TEST_DEBUG=0

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class SurrealdbEraImageDefault(Image):
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
        return SurrealdbEraImageBase(self.pr, self._config)

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
            File(".", "run_tests.sh", _RUN_TESTS_SH),
            File(
                ".",
                # prepare.sh follows rule 8's structure exactly:
                #
                #     dependency installs, cd /home/<repo>, git reset --hard,
                #     git checkout <sha>, with check_git_changes.sh asserts
                #     around it
                #
                # so `git reset --hard` appears ONCE, before the checkout, and
                # the assert appears on both sides of the pin. Then the
                # build/cache warm-up, as rule 8's long form describes.
                #
                # It is emitted WITHOUT comments (rule 10). The reasoning lives
                # here instead:
                #
                #  * dependency installs: for Rust there are none that can run
                #    before the checkout. The apt toolchain is already in the
                #    base image, and `cargo fetch` reads the Cargo.lock of the
                #    pinned commit, so it belongs to the warm-up below.
                #  * the pin needs no fetch-back: the base kept full git
                #    history (rule 9), so the commit is simply present.
                #  * `cargo fetch ... || true` and the `--no-run` build are a
                #    cache warm-up, not a correctness dependency. The acts run
                #    `--locked` WITHOUT `--offline`, so anything missed here is
                #    fetched at act time rather than being fatal. That is what
                #    makes the `|| true` the checklist asks for safe.
                #  * there is deliberately NO fix.patch rehearsal here. An
                #    earlier version applied fix.patch to warm the post-fix
                #    lockfile, but that dirties the tree and forced a second
                #    `git reset --hard`, breaking the structure above. Only PR
                #    2355's fix patch moves Cargo.lock (globset, libz-sys,
                #    rustls-webpki), and because the acts are not `--offline`
                #    its fix act simply fetches those three crates itself.
                #  * `rm -rf target/debug/incremental`: incremental state only
                #    helps repeated edit-rebuild cycles. Every act starts from
                #    the same committed tree, so here it is pure image weight.
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
bash /home/check_git_changes.sh

export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
rustc --version
cargo --version

cargo fetch --locked || cargo fetch || true

TARGETS=()
for suite in {suites}; do
  if [ -f "lib/tests/$suite.rs" ]; then TARGETS+=(--test "$suite"); fi
done
cargo test --locked --package surrealdb --no-default-features --features {features} "${{TARGETS[@]}}" --no-run || true

rm -rf target/debug/incremental
""".format(
                    pr=self.pr, suites=_TEST_SUITES, features=_CARGO_FEATURES
                ),
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
                # perfectly valid. Comment kept here, not in the shipped file.
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
        # remote for `cargo fetch`, and the scrub removes the remote.
        return f"""FROM {image_name}

{copies}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("surrealdb", "surrealdb_2559_to_1957")
class SURREALDB_2559_TO_1957(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SurrealdbEraImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section cargo_test_report.py printed. The raw
        # cargo output sits in the same log and libtest's own failure line --
        # `test <name> ... FAILED` -- also ends in FAILED, so a whole-log scan
        # would invent a second, bogus test for every real failure.
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what cargo_test_report.py prints. The
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
