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
# restore path never fires here; it is kept because it costs nothing and this
# repo ships assets/ (GIF/PNG) that a future PR could touch. Plain `git apply`
# first, `--3way` only as a fallback -- the primary apply is what a failure must
# be counted from (FLOW VERDICT DISCIPLINE).
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/vortix
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


# cargo_test_report.py -- turn one `cargo test` run into per-test results.
#
# Two properties of libtest's output make a naive regex unsafe here, and this
# script exists to neutralise both:
#
#   1. `cargo test` runs SEVERAL binaries (the lib's unit tests, each file under
#      tests/, and the doc-tests) and each prints bare `test <name> ... ok`.
#      A name is only unique WITHIN its binary, so every result is qualified
#      with the target that produced it, read from the preceding "Running" line.
#   2. That "Running" line names the binary as
#      `target/debug/deps/vortix-0bfa211eeecfd85a` -- the hash changes between
#      compilations, so it must NOT reach the test name. Only the stable source
#      path (`src/lib.rs`, `tests/integration.rs`) is kept. Doc-test names carry
#      a source line number for the same reason: the fix patch edits those very
#      files, so the line moves between acts and the same doc-test would look
#      like one test vanishing and another appearing.
_CARGO_TEST_REPORT_PY = '''"""Turn one `cargo test` run into per-test results.

usage: cargo_test_report.py <cargo-output>

Emits one line per test, in the trailing-keyword form parse_log reads:

    cargo:src/lib.rs > app::tests::test_cycle_sort_order PASSED
    cargo:tests/integration.rs > message_routing::toggle_zoom FAILED

Names are qualified with the target binary's SOURCE PATH (never the compiled
artifact, whose filename carries a per-build hash) and doc-test line numbers are
stripped, so a name is byte-identical across the three acts.

Exit status mirrors a test runner: 0 = everything passed, 1 = at least one test
failed, 2 = the run produced no tests AT ALL across every target -- i.e. the
runner never started, which is the defect GATE 0 exists to catch.

run_tests.sh invokes cargo once per target, so a target that cannot compile
(here the lib test, which references API the fix patch adds) contributes zero
lines while the targets that DO compile still report normally. A 2 therefore
means nothing ran anywhere, not merely that one target failed to build.
"""
import re
import sys

ANSI = re.compile(r"\\x1b\\[[0-9;]*[a-zA-Z]")
RUNNING_UNIT = re.compile(r"^\\s*Running unittests (\\S+) \\(")
RUNNING_TEST = re.compile(r"^\\s*Running (\\S+) \\(")
DOC_TESTS = re.compile(r"^\\s*Doc-tests (\\S+)\\s*$")
RESULT = re.compile(r"^test (.+?) \\.\\.\\. (ok|FAILED|ignored)\\b")
DOC_LINE = re.compile(r"\\s+\\(line \\d+\\)$")

STATUS = {"ok": "PASSED", "FAILED": "FAILED", "ignored": "SKIPPED"}


def main():
    with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as fh:
        log = ANSI.sub("", fh.read())

    target, seen, lines, failed = "unknown", set(), [], False
    for line in log.splitlines():
        m = RUNNING_UNIT.match(line)
        if m:
            target = m.group(1)
            continue
        m = RUNNING_TEST.match(line)
        if m:
            target = m.group(1)
            continue
        m = DOC_TESTS.match(line)
        if m:
            target = "doc-tests"
            continue
        m = RESULT.match(line)
        if not m:
            continue
        name = DOC_LINE.sub("", m.group(1))
        status = STATUS[m.group(2)]
        key = "cargo:%s > %s" % (target, name)
        if key in seen:
            continue
        seen.add(key)
        lines.append("%s %s" % (key, status))
        if status == "FAILED":
            failed = True

    if not lines:
        sys.stderr.write(
            "cargo_test_report: no test executed; the crate did not compile "
            "or the runner never started\\n")
        return 2

    for line in lines:
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


# run_tests.sh -- `cargo test` is what this repo's CI gates on
# (.github/workflows/ci.yml, job "test"), but it is invoked ONE TARGET AT A TIME.
#
# Why, and this is worth the extra lines: `--no-fail-fast` only continues past a
# *test* failure. A **compile** failure in any single target aborts the whole
# `cargo test` during the build phase, before libtest runs anything -- so one
# uncompilable target silently discards every other target's results.
#
# That is not hypothetical here. In the test act, `src/app/tests.rs` cannot
# compile (it calls `Message::ConnectSelected`, which the FIX patch introduces),
# and measured on pr-150 that took the whole act down with it:
#
#     cargo test                    -> rc=101, 0 result lines
#     cargo test --lib              -> rc=101, 0 result lines   (the real failure)
#     cargo test --test integration -> rc=101, 51 result lines, 1 FAILED
#     cargo test --doc              -> rc=0,    2 result lines
#
# The integration target compiles fine and its
# `message_routing::close_overlay_resets_all` genuinely FAILS on the test patch
# and PASSES after the fix -- a real f2p that the combined invocation scored as
# nothing at all, leaving the test act at a bare 0 (FLOW GATE 0).
#
# The exit status is captured into a variable per target rather than `|| true`,
# so a failure is still used, never swallowed; the act's real verdict comes from
# cargo_test_report.py, which still returns 2 when NOTHING ran anywhere.
# --locked refuses to silently re-resolve Cargo.lock, which would let an act
# drift from the dependency set baked into the image. CARGO_TERM_COLOR=never is
# belt-and-braces: parse_log strips ANSI anyway.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/vortix
export CARGO_TERM_COLOR=never
OUT=/tmp/cargo_test.out
: > "$OUT"
rc=0

run_target () {
  echo "----- cargo test $* -----" >> "$OUT"
  cargo test --locked --no-fail-fast "$@" >> "$OUT" 2>&1 || rc=$?
}

run_target --lib
run_target --bins
for f in tests/*.rs; do
  [ -e "$f" ] || continue
  run_target --test "$(basename "$f" .rs)"
done
run_target --doc

cat "$OUT"
echo "cargo test last-nonzero exit=${rc}"
echo "----- per-test results -----"
python3 /home/cargo_test_report.py "$OUT"
"""


class VortixImageBase(Image):
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

    # The repo pins its toolchain in rust-toolchain.toml (channel 1.91.0), so
    # the image only has to supply a rustup close enough that fetching that
    # exact channel is cheap. This tag ships 1.91.1 and publishes both
    # linux/amd64 and linux/arm64.
    def dependency(self) -> Union[str, "Image"]:
        return "rust:1.91-slim-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # What this repo's CI installs before `cargo test` on Linux
        # (.github/workflows/ci.yml): the suite drives WireGuard and OpenVPN
        # profiles, and its import/killswitch tests probe for those binaries.
        # Both package names are arch-neutral, so audit 2D stays clean.
        return ["wireguard-tools", "openvpn"]

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}`, so rust-toolchain.toml and
        # Cargo.lock are this PR's own. `rustup show` inside the work tree is
        # what materialises the pinned channel, so the acts never download a
        # toolchain; `cargo build --tests` then compiles the dependency graph
        # and the test harnesses without RUNNING anything -- the warm-up exists
        # to download and compile, not to test (FLOW BUILD TIME).
        #
        # NOT `|| true`: this is the toolchain, not a nice-to-have cache. A
        # warm-up that fails quietly ships an image with an empty registry and
        # leaves every act at the mercy of the network (FLOW Issue 14, GATE 1).
        return (
            "RUN rustup show && \\\n"
            "    rustc --version && \\\n"
            "    cargo --version && \\\n"
            "    cargo fetch --locked && \\\n"
            "    cargo build --tests --locked"
        )


class VortixImageDefault(Image):
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
        return VortixImageBase(self.pr, self._config)

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
cargo fetch --locked || true
rustc --version
cargo --version
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

cd /home/vortix
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/vortix
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

cd /home/vortix
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


@Instance.register("Harry-kp", "vortix")
class Vortix(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VortixImageDefault(self.pr, self._config)

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
        # The RAW runner output sits in the same log, and libtest's own failure
        # line -- `test <name> ... FAILED` -- also ends in FAILED, so a whole-log
        # scan invents a second, bogus test called `test <name> ...` and inflates
        # failed_count. Measured on vortix pr-150 the moment the test act first
        # produced a failure: the report claimed 54 results where 53 were
        # emitted, the extra one being that phantom.
        #
        # The fallback to the whole text matters: a bare sequence of result lines
        # with no marker (the config audit's parse_log probe, 4C) must still
        # parse, and a log that never reached the marker must still yield 0/0/0.
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
