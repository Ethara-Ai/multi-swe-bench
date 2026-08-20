"""Harness config for sharkdp/fd (Rust).

Authored against DOCKERFILE_QC_PROMPT.md (BASE checklist D1-D18, PR checklist
P1-P9) and the Rust appendix at DOCKERFILE_QC_PROMPT.md:1078.

Why this file was rewritten
---------------------------
PR 861 was graded `valid = False` with:

    error_msg: "Before applying the fix patch, the test passed; however, after
                applying the fix patch, the test failed ... test_opposing::follow"

That is a FALSE POSITIVE. `test_opposing` is flaky by construction at this
base commit. `tests/testenv/mod.rs::assert_success_and_get_output` returns the
RAW `std::process::Output` with no sorting and no normalisation, and
`test_opposing` does:

    assert_eq!(out_no_flags, out_opposing_flags)

i.e. it byte-compares the stdout of two independent fd invocations. fd is a
parallel directory walker (`num_cpus` threads), so the two listings contain the
same paths in a different order. Observed proof from the graded run: a
*different random subset* failed at each of the three stages -- run:
{no_ignore_vcs, u}; test: {hidden, no_ignore}; fix: {absolute_path, follow,
no_ignore_vcs, u}. The `test_opposing` family is also absent from this PR's
test patch (which touches only `tests/tests.rs` and adds only
`test_strip_cwd_prefix`), so excluding it removes zero graded signal.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# fd is a bin-only crate with a single integration target (`tests/tests.rs`);
# plain `cargo test` is what produced the 198-test baseline, so no
# `--all-features` here.
CARGO_BASE = "cargo test"

# Tests excluded from grading because they are non-deterministic at this base
# commit, NOT because they are inconvenient. `--skip` is a substring match, so
# this covers the whole `test_opposing::*` family while leaving
# `test_strip_cwd_prefix` (the F2P test added by this PR) untouched.
#
# INVARIANT: this filter is applied to run.sh, test-run.sh AND fix-run.sh. If a
# skip is applied to only some stages the three runs stop being comparable and
# report.py mis-buckets every test (a skipped test reads as NONE, which the
# baseline-first classifier then disambiguates from the wrong stage).
ENV_BLOCKED_TEST_FILTERS: tuple[str, ...] = ("test_opposing",)

# `--no-fail-fast`: without it cargo aborts after the first failing test binary
# (`error: test failed, to rerun pass --test tests` was emitted in the graded
# run), which silently truncates the log and makes every test in a later binary
# read as NONE.
# `--test-threads=1`: serialises libtest so concurrent fd subprocesses do not
# contend for CPU, which shrinks the ordering-race window in the remaining
# output-comparing tests.
CARGO_TEST_CMD = (
    f"{CARGO_BASE} --no-fail-fast -- --test-threads=1 "
    + " ".join(f"--skip {name}" for name in ENV_BLOCKED_TEST_FILTERS)
).strip()


class FdImageBase(Image):
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
        # D2 (CRITICAL) + Rust appendix (1)(2)(6): pinned language runtime,
        # never `rust:latest`. 1.97.1 is the toolchain that `rust:latest`
        # actually resolved to during the graded run and that compiled
        # fd-find 8.2.1 cleanly, so this pin is empirically verified rather
        # than guessed.
        #
        # Returning a *str* here is load-bearing: DockerfileEnhancer only
        # rewrites images whose dependency() is a str and whose rendered
        # content lacks a `# syntax=` directive. That injection is what
        # supplies D1, D3, D4, D5, D6, D7, D8, D11, D12, D13, D14, D15 and
        # D16. Hand-writing any of those here would either duplicate them or
        # suppress injection entirely.
        return "rust:1.97.1"

    def image_tag(self) -> str:
        # D-series / P1: per-PR base. The injected hardening block detaches to
        # ${BASE_COMMIT} and prunes everything unreachable, so a base image
        # shared across PRs would leave other PRs pointing at pruned objects.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # D18: sanitise before interpolation so org/repo cannot inject shell.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        if self.config.need_clone:
            # D11 (CRITICAL): this exact literal shape is the pattern
            # DockerfileEnhancer._standardize_repo_fetch matches. Rewording it
            # (extra flags, different spacing, a different target dir) silently
            # skips the ${REPO_URL} rewrite AND the whole history-scrub
            # injection, which would turn D13/D14/D15 into FAILs.
            code = f"RUN git clone https://github.com/{org}/{repo}.git /home/{repo}"
        else:
            code = f"COPY {repo} /home/{repo}"

        # D10 (CRITICAL for git + ca-certificates) and Rust appendix (3).
        # fd 8.2.1 has no openssl-sys in its graph, so pkg-config/libssl-dev
        # are defensive: this repo ships no Cargo.lock, so the dependency graph
        # is re-resolved at build time and can acquire a C-linking crate.
        apt_commands = """RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    build-essential \\
    pkg-config \\
    libssl-dev \\
    && rm -rf /var/lib/apt/lists/*"""

        # Rust appendix (4)(5). Set on the BASE so the prepare.sh warm-up
        # compile and all three graded runs share one cargo fingerprint.
        #   CARGO_TERM_COLOR=never  -> ANSI escapes in `Running ...` headers
        #                              would break binary qualification in
        #                              parse_log below.
        #   RUSTFLAGS=--cap-lints=warn -> a crate with #![deny(...)] under a
        #                              newer rustc turns a new lint into a hard
        #                              compile error, which yields ZERO captured
        #                              tests instead of a test failure.
        #   CARGO_NET_RETRY         -> crates.io is reached through the MITM
        #                              proxy during evaluation.
        cargo_env_commands = """ENV CARGO_TERM_COLOR=never \\
    CARGO_INCREMENTAL=0 \\
    CARGO_NET_RETRY=5 \\
    CARGO_PROFILE_TEST_DEBUG=0 \\
    RUSTFLAGS=--cap-lints=warn \\
    RUST_BACKTRACE=1"""

        # D17 (layer ordering): the clone line MUST be last. The enhancer
        # replaces it with clone + WORKDIR + reset + checkout + scrub +
        # CMD ["/bin/bash"], so anything emitted after it would render below
        # CMD and break D16/D17.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{apt_commands}

{cargo_env_commands}

{self.clear_env}

{code}

"""


class FdImageDefault(Image):
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
        return FdImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # `--3way` survives context drift between the collected diff and the
        # checked-out base; plain `git apply` is kept as a last resort.
        git_apply_opts = "--binary --3way"

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                # P5: reset to the correct BASE_COMMIT with clean-tree asserts
                # on both sides of the checkout.
                #
                # The warm-up is `--no-run` (compile only), not `cargo test ||
                # true`. The old form executed the full suite at image-build
                # time and swallowed the exit code, so a genuine compile break
                # surfaced only as an empty TestResult ("no test results
                # captured") instead of as a test failure. `--no-run` also
                # writes Cargo.lock into the image, which freezes crates.io
                # resolution identically for all three graded stages -- the
                # only determinism lever available given this repo ships no
                # committed lockfile (Rust appendix (6)).
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{cargo_base} --no-run || true

""".format(pr=self.pr, cargo_base=CARGO_BASE),
            ),
            File(
                ".",
                "run.sh",
                # P7: identical graded command in all three scripts.
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{cmd} 2>&1

""".format(pr=self.pr, cmd=CARGO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                # P6: test patch ALONE at this stage.
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply {apply_opts} /home/test.patch \\
  || git apply --binary /home/test.patch \\
  || git apply --whitespace=nowarn /home/test.patch
touch -c src/*.rs tests/*.rs Cargo.toml 2>/dev/null || true
{cmd} 2>&1

""".format(pr=self.pr, cmd=CARGO_TEST_CMD, apply_opts=git_apply_opts),
            ),
            File(
                ".",
                "fix-run.sh",
                # P6: test patch + fix patch at this stage.
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply {apply_opts} /home/test.patch /home/fix.patch \\
  || git apply --binary /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn /home/test.patch /home/fix.patch
touch -c src/*.rs tests/*.rs Cargo.toml 2>/dev/null || true
{cmd} 2>&1

""".format(pr=self.pr, cmd=CARGO_TEST_CMD, apply_opts=git_apply_opts),
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

        # Deliberately minimal. P1: inherits mswebench/<org>_m_<repo>:base-pr-<N>.
        # P2: both patches COPY'd, separately. P3: all three run-scripts COPY'd.
        # P4: prepare.sh COPY'd and invoked exactly once. P8: no stray COPY.
        # P9: does NOT re-clone, re-apt, re-scrub or re-declare proxy/CA env --
        # every one of those is inherited from the base and re-implementing any
        # of them here is a P9 violation.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


_RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RE_TEST_LINE = re.compile(
    r"^test\s+(?P<name>.+?)\s+\.\.\.\s+(?P<status>ok|FAILED|ignored)\b"
)
_RE_RUNNING = re.compile(r"^\s*Running\s+(?P<rest>.+?)\s*$")
_RE_DOCTESTS = re.compile(r"^\s*Doc-tests\s+(?P<crate>\S+)\s*$")
_RE_DEP_HASH = re.compile(r"-[0-9a-fA-F]{6,}$")
_RE_DOCTEST_LINE_SUFFIX = re.compile(r"\s*\(line \d+\)\s*$")


def _binary_label(running_rest: str) -> str:
    """Turn a cargo `Running ...` header into a stable binary label.

    `Running unittests src/main.rs (target/debug/deps/fd-eff819342fcbb53c)`
    -> `fd`
    `Running tests/tests.rs (target/debug/deps/tests-6a589af4fe43e198)`
    -> `tests`

    The trailing `-<hash>` is cargo's dependency fingerprint and changes on
    every recompile, so it must be stripped or every stage would produce a
    disjoint set of test IDs.
    """
    m = re.search(r"\(([^()]+)\)\s*$", running_rest)
    token = m.group(1) if m else running_rest.split()[-1]
    stem = token.replace("\\", "/").rsplit("/", 1)[-1]
    return _RE_DEP_HASH.sub("", stem) or stem


@Instance.register("sharkdp", "fd")
class Fd(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FdImageDefault(self.pr, self._config)

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
        """Parse cargo/libtest output into per-binary-qualified test IDs.

        libtest names are unique only WITHIN one test binary. fd emits two
        (`unittests src/main.rs` and `tests/tests.rs`), so bare names can
        collide across binaries and a FAIL in one would erase a PASS of the
        same name in the other, corrupting the run/test/fix comparison that
        report.py depends on.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        label = ""
        in_doctests = False

        for raw_line in test_log.splitlines():
            line = _RE_ANSI.sub("", raw_line).rstrip()

            m_doc = _RE_DOCTESTS.match(line)
            if m_doc:
                label = f"doc-tests {m_doc.group('crate')}"
                in_doctests = True
                continue

            m_run = _RE_RUNNING.match(line)
            if m_run:
                label = _binary_label(m_run.group("rest"))
                in_doctests = False
                continue

            m_test = _RE_TEST_LINE.match(line.strip())
            if not m_test:
                continue

            name = m_test.group("name").strip()
            if in_doctests:
                # Doc-test IDs embed a source line number that shifts whenever
                # anything above the example is edited, so an untouched
                # doc-test would otherwise reappear as a bogus N2P.
                name = _RE_DOCTEST_LINE_SUFFIX.sub("", name)

            test_id = f"{label}::{name}" if label else name

            status = m_test.group("status")
            if status == "ok":
                passed_tests.add(test_id)
            elif status == "FAILED":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        # Failed wins: a retry or a duplicated summary line must never let a
        # test appear in two buckets (TestResult.__post_init__ asserts the
        # three sets are pairwise disjoint and would raise).
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
