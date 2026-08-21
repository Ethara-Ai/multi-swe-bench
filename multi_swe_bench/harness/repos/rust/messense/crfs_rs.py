import re
from typing import Optional

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

# crfs-rs declares `crfsuite = "0.3.1"` in [dev-dependencies] (the benchmark
# compares this pure-Rust port against the original C implementation). Cargo
# builds the WHOLE dev-dependency set for any target compiled in test mode, so
# `crfsuite` -> `crfsuite-sys` -> `liblbfgs-sys` is built even by a plain
# `cargo test --lib`. `liblbfgs-sys`'s build script shells out to `cmake`, which
# the official `rust:` images do not ship:
#
#   failed to execute command: No such file or directory (os error 2)
#   is `cmake` not installed?
#
# Without cmake every stage fails to compile and the instance captures zero
# tests. clang / libclang-dev / pkg-config cover the bindgen and pkg-config
# probes the same `-sys` build scripts can reach into.
#
# MULTI-ARCH: every package here is a Debian multi-arch package resolved by apt
# for whatever ${TARGETARCH} is being built, and the `-sys` crates compile their
# vendored C from source via cmake/cc rather than downloading a prebuilt binary.
# There is therefore nothing arch-specific to branch on -- no per-arch apt pin,
# no arch-suffixed tarball fetch, no TARGETARCH conditional. Verified built and
# green on linux/amd64; an arm64 build has not been exercised, and only an actual
# `docker buildx build --platform linux/arm64` can close that gap -- it is not
# something a config edit can assert.
_EXTRA_PACKAGES = """RUN apt-get update && apt-get install -y --no-install-recommends \\
    cmake \\
    clang \\
    libclang-dev \\
    pkg-config \\
    && rm -rf /var/lib/apt/lists/*"""

# ─────────────────────────────────────────────────────────────────────────────
# Shared test runner. Written verbatim (no str.format) so its `${...}`, `$(...)`
# and awk `{...}` braces need no escaping.
#
# THE PROBLEM THIS SOLVES
# `cargo test` runs in two phases: it builds EVERY selected target, then runs
# them. A build failure in one target aborts the entire invocation, so a single
# `cargo test --lib --tests` lets an unbuildable integration target erase the
# library's unit-test results -- results that are unrelated to it and would have
# passed. On this PR that is exactly what happens: the gold test patch adds three
# tests/*.rs files that reference `crfs::train` and `tempfile`, neither of which
# exists until the fix patch lands, so the test stage recorded (0, 0, 0) even
# though the crate's own 6 unit tests still compiled and passed.
#
# A stage must never report fewer tests than the stage before it merely because
# of how the build was batched. Two measures below guarantee that:
#
#   1. Every target is built and run in its OWN invocation, so a build failure
#      is confined to the target that caused it.
#   2. A target that fails to build has its tests recorded as FAILED rather than
#      dropped -- a test whose binary cannot be produced is, by definition, not
#      passing. The names come from a manifest that libtest itself generated at
#      image-build time (see build-manifest.sh); they are never guessed from
#      source text, and they are byte-identical to the ids a successful run of
#      that target emits, so a test transitions FAIL -> PASS across stages
#      instead of appearing from nowhere.
# ─────────────────────────────────────────────────────────────────────────────
_RUN_TESTS_SH = """#!/bin/bash
# Shared by run.sh / test-run.sh / fix-run.sh. The caller has already cd'ed into
# the repository; this script inherits that working directory.
#
# NOTE: deliberately no `set -e`. Every target must run even after an earlier one
# fails, so failures are collected in $rc and re-raised once at the end (`exit $rc`),
# which is what propagates a test failure to the caller. `pipefail` is set so that
# if a future edit ever pipes a cargo invocation (through tee, grep, ...) the
# runner's exit status reflects cargo, not the last stage of the pipe.
set -uo pipefail

export CARGO_TERM_COLOR=never
export CARGO_NET_RETRY=5
export RUST_BACKTRACE=1
# Signals a non-interactive automated run. Neither cargo nor libtest reads it, but
# build scripts in the dependency graph may, and it is the cross-language harness
# convention.
export CI=true

MANIFEST=/home/test_manifest.tsv
rc=0

# --- library unit tests -----------------------------------------------------
# Always first, and always on its own: these exist at every stage and must never
# be lost to an unrelated target's build failure.
cargo test --lib --no-fail-fast || rc=1

# --- integration test targets ----------------------------------------------
# crfs-rs declares no [[test]] sections, so cargo auto-discovers exactly the
# tests/*.rs files and this glob enumerates the same targets cargo would. The
# base commit has no tests/*.rs at all (tests/ holds only the model.crfsuite
# fixture), in which case the loop body never executes.
for src in tests/*.rs; do
    [ -e "$src" ] || continue
    target=$(basename "$src" .rs)

    # --no-run separates "did not build" from "built and some tests failed".
    # Its compile output stays in the log as the evidence for whichever verdict
    # follows; when it succeeds the run below is a pure cache hit.
    if cargo test --test "$target" --no-run; then
        cargo test --test "$target" --no-fail-fast || rc=1
        continue
    fi

    rc=1
    echo "### harness: target '$src' failed to build; recording its tests as FAILED ###"
    synth=$(awk -F'\\t' -v s="$src" '$1 == s { print "test " $2 " ... FAILED" }' "$MANIFEST" 2>/dev/null || true)
    if [ -n "$synth" ]; then
        echo "     Running $src (BUILD FAILED)"
        printf '%s\\n' "$synth"
    else
        echo "### harness: no manifest entry for '$src'; per-test verdicts unavailable ###"
    fi
done

exit $rc
"""

# Generates the manifest consulted above. Run ONCE at image-build time with both
# gold patches applied, which is the only state in which every integration target
# compiles and libtest can therefore be asked for the authoritative name of every
# test it contains.
#
# Using `--list` rather than grepping `#[test]` out of the source matters: libtest
# reports the fully-qualified name it will actually print at run time (module path
# included, macro-generated cases included), so a synthesized FAILED line and a
# real run of the same target produce the identical test id.
_BUILD_MANIFEST_SH = """#!/bin/bash
# Writes /home/test_manifest.tsv, one "<test source path>\\t<libtest name>" per
# line. The caller has already cd'ed into the repository.
#
# `pipefail` matters here specifically: the --list call below is a three-stage
# pipeline, and without it a cargo failure would be masked by a successful `sed`
# and silently yield an empty manifest.
set -uo pipefail

export CARGO_TERM_COLOR=never
export CARGO_NET_RETRY=5
export CI=true

MANIFEST=/home/test_manifest.tsv
: > "$MANIFEST"

# Compile everything up front so each --list below is a cache hit.
cargo test --lib --tests --no-run || true

for src in tests/*.rs; do
    [ -e "$src" ] || continue
    target=$(basename "$src" .rs)
    # libtest prints "<name>: test" for tests and "<name>: benchmark" for benches;
    # only the former is wanted.
    cargo test --test "$target" -- --list 2>/dev/null \\
        | sed -n 's/^\\(.*\\): test$/\\1/p' \\
        | while IFS= read -r name; do
              [ -n "$name" ] || continue
              printf '%s\\t%s\\n' "$src" "$name" >> "$MANIFEST"
          done
done

echo "### harness: generated test manifest ###"
cat "$MANIFEST"
"""


class CrfsRsImageBase(Image):
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
        # crfs-rs's Cargo.toml declares `edition = "2024"`, which no toolchain
        # before 1.85 can parse. 1.90 is pinned rather than `rust:latest`
        # because the repo .gitignore's Cargo.lock: every build re-resolves the
        # dependency graph, and a floating toolchain would silently change what
        # gets compiled. Verified in-container: the run, test and fix stages all
        # behave as expected under 1.90.
        return "rust:1.90"

    # The base is tagged per PR, not with a constant `base`. This image is
    # hardened to exactly ONE ${BASE_COMMIT} (`git checkout --detach` followed by
    # `git gc --prune=now`), and build_dataset skips rebuilding an image that
    # already exists -- so a repo-constant tag makes every crfs-rs PR resolve to
    # the same name and the second instance silently inherits a tree pinned to
    # the first one's commit. `repos/rust/tower_rs/tower.py` documents that exact
    # failure measured in-container: 1 of 8 base shas resolves, 7 report MISS.
    # workdir() moves with it so the build context lands in images/base-pr-<N>/.
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

        # `dependency()` returns a str, so DockerfileEnhancer rewrites the clone
        # line below into the standard `git clone "${REPO_URL}"` +
        # `git checkout ${BASE_COMMIT}` + history-hardening + CMD block, and
        # prepends the BuildKit syntax directive, the TARGETARCH / REPO_URL /
        # BASE_COMMIT and proxy ARGs, the shared ENV block, the OCI labels and
        # the CA-certificate symlinks. The apt layer sits before the clone so it
        # is cached independently of the checked-out commit.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{_EXTRA_PACKAGES}

{code}

{self.clear_env}

"""


class CrfsRsImageDefault(Image):
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
        return CrfsRsImageBase(self.pr, self._config)

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
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "run-tests.sh",
                _RUN_TESTS_SH,
            ),
            File(
                ".",
                "build-manifest.sh",
                _BUILD_MANIFEST_SH,
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

# Warm the baseline build: resolve the manifest, populate the crates.io cache
# and fill target/ so the graded `run` stage does not have to compile the whole
# dependency tree (crfsuite's vendored C sources included) from scratch.
bash /home/run-tests.sh || true

# Apply BOTH gold patches. This is the only state in which every integration
# target compiles, which buys two things at once:
#
#   * the crates.io cache and target/ are warmed for the graded fix stage -- the
#     fix patch adds three dependencies (anyhow, liblbfgs, tempfile) the base
#     manifest cannot resolve, so without this the fix stage would be the first
#     thing in the pipeline to reach the network;
#   * build-manifest.sh can ask libtest for the authoritative name of every test
#     in every integration target, which run-tests.sh later uses to name the
#     tests of a target that fails to build.
#
# Both patches are then reversed in reverse order and check_git_changes.sh
# asserts the working tree is pristine again -- `set -e` makes a failed reverse
# fail the image build loudly here rather than silently shipping a dirty
# checkout. Cargo.lock and target/ are .gitignore'd, so neither step dirties the
# tree, and the manifest is written outside the repo (/home).
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/build-manifest.sh
git apply -R --whitespace=nowarn /home/fix.patch /home/test.patch
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
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
bash /home/run-tests.sh

""".format(pr=self.pr),
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
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


_RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# libtest result line: `test model::tests::test_model_new ... ok`. Also matches
# the synthetic `test <name> ... FAILED` lines run-tests.sh emits for a target
# that failed to build -- by construction they are the same shape.
_RE_TEST_LINE = re.compile(
    r"^test\s+(?P<name>.+?)\s+\.\.\.\s+(?P<status>ok|FAILED|ignored)\b"
)
# Cargo announces each test binary before executing it. run-tests.sh emits a
# `Running <src> (BUILD FAILED)` header in the same shape for an unbuildable
# target, so both paths yield the same label.
_RE_RUNNING = re.compile(r"^\s*Running\s+(?P<rest>.+?)\s*$")
_RE_UNITTESTS = re.compile(r"^unittests\b")
_RE_DOCTESTS = re.compile(r"^\s*Doc-tests\s+(?P<crate>\S+)\s*$")
_RE_DEP_HASH = re.compile(r"-[0-9a-fA-F]{6,}$")
# rustdoc names a doc-test after the source line it starts on; the line number is
# not part of the test's identity. Doc-tests are not selected by run-tests.sh,
# but the suffix is stripped anyway so an override run command cannot leak
# unstable ids.
_RE_DOCTEST_LINE_SUFFIX = re.compile(r"\s*\(line \d+\)\s*$")


def _binary_stem(running_rest: str) -> str:
    """Hash-free stem of the executable named in a cargo `Running ...` line.

    Handles both spellings:
        Running unittests src/lib.rs (target/debug/deps/crfs-77f9765aa21e4a2a)
        Running target/debug/deps/crfs-77f9765aa21e4a2a      (older cargo)
    """
    m = re.search(r"\(([^()]+)\)\s*$", running_rest)
    token = m.group(1) if m else running_rest.split()[-1]
    stem = token.replace("\\", "/").rsplit("/", 1)[-1]
    return _RE_DEP_HASH.sub("", stem) or stem


def _target_label(running_rest: str) -> str:

    rest = running_rest.strip()
    if _RE_UNITTESTS.match(rest):
        return _binary_stem(rest)
    head = rest.split(" (", 1)[0].strip()
    if head.endswith(".rs"):
        return head
    return _binary_stem(rest)


@Instance.register("messense", "crfs-rs")
class CrfsRs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CrfsRsImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Current test binary; empty until cargo announces one, in which case
        # ids fall back to the bare libtest name rather than being dropped.
        label = ""

        for raw in test_log.splitlines():
            line = _RE_ANSI.sub("", raw).strip()

            m = _RE_DOCTESTS.match(line)
            if m:
                label = f"doc-tests {m.group('crate')}"
                continue

            m = _RE_RUNNING.match(line)
            if m:
                label = _target_label(m.group("rest"))
                continue

            m = _RE_TEST_LINE.match(line)
            if not m:
                continue

            name = m.group("name").strip()
            if label.startswith("doc-tests"):
                name = _RE_DOCTEST_LINE_SUFFIX.sub("", name)
            test_id = f"{label}::{name}" if label else name
            status = m.group("status")

            if status == "ok":
                passed_tests.add(test_id)
            elif status == "FAILED":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        # A test that failed anywhere is failed, even if an earlier line said ok
        # (a retried or re-reported case), so TestResult's disjointness
        # invariants always hold.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
