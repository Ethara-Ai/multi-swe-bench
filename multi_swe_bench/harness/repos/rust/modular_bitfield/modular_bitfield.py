"""Repo config for modular-bitfield/modular-bitfield (Rust / cargo test + trybuild).

Dataset shape
-------------
ONE PR -- #25, "Allow non-power-of-two enums", merged 2020-10-25. Rule 4 row 1
therefore applies: a PER-PR base tagged ``base-pr-25`` carrying the COMPLETE
history scrub, and a thin ``pr-25`` layer that only stages patches and scripts.
There is nothing to share a base with, and a per-PR tag is what lets the prune
pin one HEAD (see rule 5 and the ``ModularBitfieldImageBase`` docstring).

What the PR does
----------------
``#[derive(BitfieldSpecifier)]`` previously demanded a variant count that is a
power of two. The fix adds a ``#[bits = N]`` helper attribute -- ``impl/src/lib.rs``
gains ``attributes(bits)`` on the derive, and ``impl/src/bitfield_specifier.rs``
gains ``parse_attrs`` plus the branch that uses the declared width instead of
deriving it from the variant count.

The gold test patch touches four files, all under ``tests/``:

* ``tests/06-enums.rs`` -- rewritten to declare a 6-variant ``SmallPrime`` enum
  with ``#[bits = 4]``. Without the fix the derive rejects it, so this file stops
  compiling. F2P.
* ``tests/28-single-bit-enum.rs`` -- new file, the degenerate ``#[bits = 1]``
  case. F2P (it RUNS and FAILS in the test stage; it is not an N2P).
* ``tests/08-non-power-of-two.stderr`` -- the expected diagnostic text changes,
  because the fix appends ", specify #[bits = 2] if that was your intent" to the
  error. F2P.
* ``tests/progress.rs`` -- registers case 28 with the trybuild driver.

The fix patch touches ONLY ``impl/src/``. It shares no file with the test patch,
so ``Report.check()`` step 5 (the cheating guard) has nothing to catch.

Runner
------
Upstream CI (``.github/workflows/rust.yml``, ``test`` job) is a bare
``cargo test`` on ``stable``. That is what runs here, but split into four
invocations for a mechanical reason -- output legibility, not test selection.
Every declared test still runs, exactly once, in all three graded stages.

``Cargo.toml`` sets ``autotests = false`` and declares a single
``[[test]] name = "tests" path = "tests/progress.rs"``. That one target holds:

* ``panic_tests`` -- four ordinary ``#[should_panic]`` libtest functions, and
* ``tests`` -- a single ``#[test] fn`` driving ``trybuild`` over 28 cases.

So a plain ``cargo test`` reports the whole trybuild suite as ONE result named
``tests``. That is a collapsed suite: F2P would be a single coarse id and the 26
unaffected cases would contribute no P2P at all. trybuild prints its own per-case
line, though::

    test tests/06-enums.rs [should pass] ... ok
    test tests/08-non-power-of-two.rs [should fail to compile] ... mismatch

``parse_log`` reads those, which is where the real granularity comes from.

Why four invocations
--------------------
Those trybuild lines are written from INSIDE the ``tests`` function, so libtest
captures them unless ``--nocapture`` is passed. But ``--nocapture`` also uncovers
the four ``#[should_panic]`` tests, whose panic messages then interleave
MID-LINE with libtest's own result lines. Measured 2026-08-27 on a combined
``cargo test -- --nocapture`` run::

    test panic_tests::invalid_access_a - should panic ...    0: ok
                 at ./tests/panic_tests.rs:test panic_tests::invalid_access_b - should panic ... 9:5ok

Two results destroyed on one line. ``--test-threads=1`` does not fix it: with a
single thread libtest still prints ``test NAME ... ``, runs the test (which
panics to the same stream), and only then prints ``ok``. A result that vanishes
in one stage but not another is exactly the ``PASS -> NONE -> FAIL`` anomaly
``Report.check()`` step 4 rejects, so this had to be designed out rather than
parsed around.

The split gives each kind of test the capture mode it needs:

1. ``--lib``                       -- 0 tests, kept so the target is not silently dropped.
2. ``--test tests ... panic_tests::``  -- the four panic tests, output CAPTURED (clean lines).
3. ``--doc``                       -- the two doc-tests in ``src/lib.rs``.
4. ``--test tests ... --exact tests --nocapture`` -- the trybuild driver ALONE,
   uncaptured. Nothing else is running, so nothing can interleave with it.

``RUST_BACKTRACE`` is deliberately NOT set. Pass 2 captures panic output anyway,
and turning backtraces on only inflates the log.

Environment
-----------
``rust:1.90-bookworm``, and this is a compromise that is worth stating plainly.

The contemporary toolchain would be 1.47 (stable on 2020-10-25). It cannot be
used. ``Cargo.lock`` is gitignored (``.gitignore``:11), so every resolve hits
today's crates.io, and cargo 1.47 cannot parse the manifests it gets back::

    error: failed to parse manifest at .../either-1.18.0/Cargo.toml
    Caused by: this version of Cargo is older than the `2021` edition

Three ways to force an era-appropriate dependency set were measured on
2026-08-27 and all three failed:

* ``cargo +nightly-2020-10-25 update -Z minimal-versions`` (upstream's own answer
  in the sibling ``JelteF/derive_more`` config) resolves, but the minima do not
  COMPILE -- ``crossbeam-utils 0.6.0`` dies on "unexpected end of macro
  invocation".
* Pinning each crate to its newest pre-2020-10-26 release with
  ``cargo update --precise`` fails on every semver-incompatible bump, and the
  bumps that DO apply re-resolve their own dependencies upward -- the run ended
  on ``rayon-core 1.13.0``, edition 2021 again.
* A newer-but-still-old cargo does not help either: 1.56 parses the manifests and
  then hits ``rayon-core v1.12.0 ... requires rustc 1.63 or newer``.

The cost of running on 1.90 is bounded and known. rustc's rendered diagnostics
are not stable across releases, and four ``compile_fail`` cases assert on them:

    tests/04-multiple-of-8bits.rs    path trimming: `checks::SevenMod8` -> `SevenMod8`
    tests/09-variant-out-of-range.rs same
    tests/11-bits-attribute-wrong.rs E0308 detail text changed
    tests/20-access-test.rs          "associated function `a` is private" -> "method `a` is private"

Those four report ``mismatch`` in ALL THREE stages. They are reported honestly as
failing rather than filtered out: failing identically everywhere, they cannot be
credited as F2P, cannot trip step 2 (no PASS -> FAIL), and cannot trip step 4
(their ``test`` status is FAIL, not NONE/SKIP).

The three cases the PR actually depends on are unaffected, because their
diagnostics come from this crate's own ``format_err!`` rather than from rustc:
``08-non-power-of-two`` matches on 1.90, and ``06-enums`` / ``28-single-bit-enum``
are ``t.pass`` cases that assert nothing about diagnostics.

Measured end to end in the built image, 2026-08-27, ``--network none``:

    F2P 3   06-enums, 08-non-power-of-two, 28-single-bit-enum
    P2P 26  22 trybuild cases + 4 panic tests (+2 doc-tests)
    N2P 0
    permanent failures 4  (the compile_fail cases above)

Dependency determinism
----------------------
Resolution happens ONCE, in ``prepare.sh``, at image-build time. All three graded
stages then read the same ``Cargo.lock`` under ``--locked``, so a crates.io
release landing between the baseline stage and the fix stage cannot masquerade as
the fix working. ``prepare.sh`` also runs the suite once to unpack every crate
into the registry cache and to materialise trybuild's nested project, which is
why the graded stages need no network at all -- verified with
``docker run --network none``.

``Cargo.lock`` and ``target/`` are both gitignored, so writing them leaves the
tree clean for the ``check_git_changes.sh`` asserts and for ``git apply``.

No ``apt-get`` layer is emitted: ``rust:*`` derives from
``buildpack-deps:*-scm``, which already carries git and ca-certificates, and this
dependency graph is pure Rust.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# libtest's watchdog prints this from the main thread while a test is still
# running, and it lands wherever the cursor happens to be -- which for a
# 75-second trybuild run is in the MIDDLE of a case line:
#
#     test tests/17-byte-conversions.rs [should pass] ... test tests has been running for over 60 seconds
#     ok
#
# Deleting the fragment AND its newline rejoins the split line. The
# `(?:(?!\.\.\.).)+?` body is load-bearing: a plain `.+?` would start matching at
# the FIRST `test ` on the line, swallow the case name and the `... ` marker, and
# delete the whole line -- silently dropping that case from one stage only, which
# is precisely the NONE-in-one-stage anomaly this parser exists to avoid.
_WATCHDOG = re.compile(r"test (?:(?!\.\.\.).)+? has been running for over \d+ seconds\n")

# `Running unittests src/lib.rs (target/debug/deps/modular_bitfield-124f23f4015bc66a)`
# The trailing build hash changes whenever a patch changes the sources, so only
# the target path is kept.
_RUNNING_LINE = re.compile(
    r"^\s*Running\s+(?:unittests\s+)?(?P<target>\S+)\s+\(target/[^)]*\)\s*$"
)
_DOCTEST_LINE = re.compile(r"^\s*Doc-tests\s+(?P<crate>\S+)\s*$")
_RUNNING_COUNT = re.compile(r"^running \d+ tests?$")
_RESULT_LINE = re.compile(r"^test result:")

# trybuild's own per-case line. Statuses seen in practice: ok / mismatch / error.
# Anything that is not `ok` is a failure.
_TRYBUILD_LINE = re.compile(
    r"^test (?P<path>\S+) \[should (?:pass|fail to compile)\] \.\.\. (?P<status>\S+)"
)
_CASE_LINE = re.compile(r"^test (?P<name>.+?) \.\.\. (?P<status>\S+)")

# libtest decorates `#[should_panic]` names with this suffix. Stripping it keeps
# the id equal to the source path of the function.
_SHOULD_PANIC = re.compile(r" - should panic$")

# The libtest test that merely DRIVES trybuild. Its 28 cases are already reported
# individually above, so counting it too would double-count them -- and it fails
# in every stage regardless (the four rustc-diagnostic mismatches), so keeping it
# would add a permanently-red id that says nothing the cases do not already say.
_TRYBUILD_DRIVER = "tests/progress.rs::tests"


def parse_cargo_test_log(log: str) -> TestResult:
    """Rebuild per-test ids from cargo + libtest + trybuild output.

    Ids are TOOL-FIRST -- ``trybuild::tests/06-enums.rs``, not
    ``tests/06-enums.rs::trybuild``. ``report.py`` splits a test name on ``::``
    and treats the head as a file path; with the path first, every id whose file
    the fix patch touched would land in ``guard_fix_patch_touched_tests`` and
    fail the whole instance. With ``trybuild`` / ``cargo-test`` as the head no id
    matches any file, ``_file_matcher_can_hit`` goes False, and both the cheating
    guard and the phantom-N2P matcher correctly stand down.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)
    clean = _WATCHDOG.sub("", clean)

    # `pending_target` is the most recently ANNOUNCED target; `current_target` is
    # the one whose result block is actually open. cargo can print the next
    # `Running` banner before the previous block closes, so the banner is only
    # promoted at the `running N tests` header.
    current_target = ""
    pending_target = ""

    for raw in clean.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _RUNNING_LINE.match(line)
        if m:
            pending_target = m.group("target")
            continue

        m = _DOCTEST_LINE.match(line)
        if m:
            pending_target = f"Doc-tests {m.group('crate')}"
            continue

        if _RUNNING_COUNT.match(line.strip()):
            if pending_target:
                current_target, pending_target = pending_target, ""
            continue

        # Must precede _CASE_LINE: the summary line also starts with `test `.
        if _RESULT_LINE.match(line):
            continue

        m = _TRYBUILD_LINE.match(line)
        if m:
            name = f"trybuild::{m.group('path')}"
            if m.group("status").lower() == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)
            continue

        m = _CASE_LINE.match(line)
        if m:
            leaf = _SHOULD_PANIC.sub("", m.group("name").strip())
            if not leaf:
                continue
            qualified = f"{current_target}::{leaf}" if current_target else leaf
            if qualified == _TRYBUILD_DRIVER:
                continue
            name = f"cargo-test::{qualified}"
            status = m.group("status").lower()
            if status == "ok":
                passed_tests.add(name)
            elif status == "ignored":
                skipped_tests.add(name)
            else:
                failed_tests.add(name)

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


# Common body shared by run.sh / test-run.sh / fix-run.sh.
#
# Byte-identical in all three by construction: the ONLY thing that differs
# between the graded stages is which patch was applied before this block runs.
# Anything that varied the command itself would make a FAIL -> PASS transition
# attributable to the command rather than to the fix.
#
# See the module docstring ("Why four invocations") for why this is four cargo
# calls rather than one. Every declared test runs exactly once.
#
# `--locked` on every call: prepare.sh already resolved the dependency graph at
# image-build time. A stage that silently re-resolved could pick up a crates.io
# release published since, and a version change between the baseline and fix
# stages is indistinguishable from the fix working.
#
# `set +e` rather than `|| true` per command: the suite MUST survive a failing
# stage (the baseline stage fails by design), but the run must still fail loudly
# if cargo never started at all. The trailing grep restores exactly that
# protection -- without it an empty log would parse to a silent 0/0/0 that looks
# like a legitimate result.
_TEST_BODY = """\
export CARGO_TERM_COLOR=never
OUT=/tmp/suite.out
rm -f "$OUT"

set +e
cargo test --locked --no-fail-fast --lib -- --test-threads=1 >> "$OUT" 2>&1
cargo test --locked --no-fail-fast --test tests -- --test-threads=1 panic_tests:: >> "$OUT" 2>&1
cargo test --locked --no-fail-fast --doc -- --test-threads=1 >> "$OUT" 2>&1
cargo test --locked --no-fail-fast --test tests -- --test-threads=1 --exact tests --nocapture >> "$OUT" 2>&1
set -e

# parse_log reads stdout, so the captured output has to land there.
cat "$OUT"

grep -q "^test result:" "$OUT"
"""


class ModularBitfieldImageBase(Image):
    """Per-PR base ``base-pr-25`` -- rust:1.90 with the repo cloned and scrubbed.

    ``dependency()`` returns a str, so two things follow that the rest of this
    file depends on:

    * ``build_dataset.py``:625-629 passes ``REPO_URL`` and ``BASE_COMMIT`` as
      build args. ``Image._HARDENING_BLOCK`` opens with
      ``git checkout --detach "${BASE_COMMIT}"``, so it can only run somewhere
      that value is real -- which is here, and NOT in the ``pr-25`` layer.
    * ``DockerfileEnhancer._standardize_repo_fetch`` rewrites the ``git clone``
      line below into clone + WORKDIR + reset + checkout + the FULL hardening
      block + ``CMD``. Nothing is emitted after the clone for exactly that
      reason: the enhancer appends ``CMD`` there and any later instruction would
      be stranded below it.

    The tag is ``base-pr-25`` rather than a shared era name because this dataset
    holds exactly one PR (rule 4 row 1). ``gc --prune=now`` needs one pinned
    HEAD; pinning a SHARED base would fix it to whichever PR built it first and
    every other PR in the era would then die on ``fatal: unable to read tree``.
    One PR, one base, one prune -- so the complete scrub lives here and the PR
    layer carries none of it (rule 5).
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
        # NOT the contemporary 1.47. See "Environment" in the module docstring
        # for the three measured attempts at an era-appropriate toolchain and
        # why each one is impossible; the residual cost is four compile_fail
        # cases that mismatch identically in all three stages.
        return "rust:1.90-bookworm"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # No apt layer: rust:* derives from buildpack-deps:*-scm, so git and
        # ca-certificates are already present, and nothing in this dependency
        # graph is a *-sys crate needing a C toolchain or pkg-config.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class ModularBitfieldImageDefault(Image):
    """Per-PR image ``pr-25`` -- stage the patches and run-scripts, resolve the
    dependency graph once, and warm the build.

    Deliberately thin. No clone, no apt, no CA/proxy setup and NO history scrub:
    ``base-pr-25`` is pinned to this PR's base commit and has already run the
    complete scrub (gc, repack, all four asserts), so there is nothing left to
    prune here and repeating it would only re-run an expensive no-op.
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
        return ModularBitfieldImageBase(self.pr, self._config)

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
cd /home/{pr.repo}
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: /home/{pr.repo} is not a git repository" >&2
    exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi
# `git status` can pass on stat-cache luck alone -- a file whose size and mtime
# are unchanged is never re-read. This forces a real content comparison against
# HEAD, so a build that quietly shipped a modified worktree fails here instead.
if ! git diff --quiet HEAD --; then
    echo "check_git_changes: tracked content differs from HEAD:" >&2
    git diff --stat HEAD -- >&2
    exit 1
fi
echo "check_git_changes: clean at $(git rev-parse HEAD)"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export CARGO_TERM_COLOR=never

# Resolve ONCE, here, while the network is still available.
#
# No Cargo.lock is committed -- it is gitignored (.gitignore:11) -- so without
# this every graded stage would resolve against crates.io independently and a
# release landing mid-run would be indistinguishable from the fix working.
# Writing the lock cannot dirty the tree for the asserts above or for the
# `git apply` in the run scripts, precisely because it is gitignored.
#
# No `|| true` anywhere below: this is the step that makes the graded stages
# deterministic and network-free, so it has to fail the BUILD if it fails, not
# leak a cold cache into the run.
cargo generate-lockfile
cargo fetch --locked

# Warm the build so the graded stages only recompile what a patch changed.
cargo test --locked --no-run

# Run the suite once as well. This is not redundant with the line above:
# trybuild compiles its 28 cases at RUN time inside a nested cargo project under
# target/tests/trybuild/, which does not exist until the driver has executed
# once. Materialising it here is what lets the graded stages run with no network
# at all -- verified with `docker run --network none` on 2026-08-27.
cargo test --locked --no-fail-fast --test tests -- --test-threads=1 --exact tests --nocapture > /tmp/warm.out 2>&1 || true
grep -q "^test result:" /tmp/warm.out

# The warm-up compiles into target/ (gitignored) and must not have touched a
# tracked file. `set -e` turns a failure here into a failed build rather than an
# image that silently ships a modified baseline.
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        file_names = " ".join(file.name for file in self.files())
        copy_command = f"COPY {file_names} /home/"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_command}

RUN bash /home/prepare.sh

CMD ["/bin/bash"]
"""


@Instance.register("modular-bitfield", "modular-bitfield")
class ModularBitfield(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ModularBitfieldImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_cargo_test_log(log)
