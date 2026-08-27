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

# tantivy-py is a pyo3/maturin project: GitHub classifies it as Rust (216 KB Rust
# vs 134 KB Python), the graded artefact is a native extension compiled from
# `src/*.rs`, but every test in the suite is a pytest test under `tests/`.
# The image therefore needs BOTH toolchains:
#
#   * Rust -- `rust-toolchain.toml` pins `channel = "1.73.0"` and
#     .github/workflows/ci.yml installs exactly `1.73.0`, so the base image is
#     `rust:1.73.0-bookworm`. Because that image's rustup default toolchain is
#     literally `1.73.0`, the checked-in rust-toolchain.toml resolves to an
#     already-installed toolchain and never triggers a download at build time.
#   * Python -- `pyproject.toml` declares `requires-python = ">=3.8"`; bookworm
#     ships CPython 3.11, which pyo3 0.20 supports.
#
# Debian bookworm marks its system interpreter PEP 668 "externally managed", so
# pip cannot install into it. A venv at /opt/venv is created once here and put on
# PATH; every later `pip`/`python` in prepare.sh and the run scripts is that venv.
# `maturin develop` (not used today, but a likely future edit) additionally
# requires VIRTUAL_ENV to be set, so it is exported rather than relying on PATH.
#
# apt packages:
#   python3, python3-venv -- the interpreter and `ensurepip` for the venv above.
#   python3-dev           -- headers; pyo3's `extension-module` feature does not
#                            link libpython, but pyo3-build-config probes a full
#                            interpreter installation.
#   patchelf              -- maturin shells out to it when post-processing the
#                            built .so; a missing patchelf is a build-time error
#                            rather than a warning on some maturin code paths.
#   pkg-config            -- consulted by cc / `-sys` build scripts in the graph.
#
# MULTI-ARCH: `rust:1.73.0-bookworm` publishes linux/amd64 and linux/arm64, every
# apt package above is a normal Debian multi-arch package resolved for whatever
# ${TARGETARCH} is being built, and the Python distributions installed later
# (maturin, pytest, mktestdocs) are pure-Python or ship manylinux aarch64 wheels.
# Nothing here pins an architecture, fetches an arch-suffixed tarball, or needs a
# TARGETARCH conditional. Only an actual `docker buildx build --platform
# linux/arm64` can prove the arm64 build green; that is not something a config
# edit can assert.
_EXTRA_SETUP = """RUN apt-get update && apt-get install -y --no-install-recommends \\
    python3 \\
    python3-dev \\
    python3-venv \\
    patchelf \\
    pkg-config \\
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
ENV PATH=/opt/venv/bin:$PATH
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_INPUT=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN python3 -m venv /opt/venv && \\
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel"""


# -----------------------------------------------------------------------------
# Shared test runner, used verbatim by run.sh / test-run.sh / fix-run.sh so all
# three stages execute the identical command and their test ids line up.
#
# Written without str.format so its `$(...)` and `${...}` need no brace escaping.
#
# WHY THE REBUILD IS PART OF THE TEST COMMAND
# The fix patch for this repo edits `src/query.rs` -- Rust. Python tests can only
# observe that change after the pyo3 extension module has been recompiled and
# re-linked into `tantivy/`. A runner that only invoked pytest would execute the
# fix stage against the extension built from base sources, `Query.boost_query`
# would still be missing, and every test the gold patch adds would fail in the
# fix stage exactly as it does in the test stage -- Report.check() Rule 3 (some
# test must go !PASS -> PASS) would then reject the instance.
#
# `pip install -e . --no-build-isolation` is what the repo's own noxfile and CI
# run. `--no-build-isolation` makes the interpreter's maturin the build backend
# (prepare.sh pins it to the `maturin<=1.3.2` bound pyproject declares) and
# `--no-deps` keeps pip off the network: `[project]` declares no runtime
# dependencies, so there is nothing to resolve. For a maturin *mixed* layout the
# editable install drops the compiled module next to the Python sources, at
# `tantivy/tantivy.cpython-*.so` -- a path .gitignore already covers, so the
# rebuild never dirties the working tree for the next `git apply`.
#
# The build is NOT suffixed with `|| true`. If the extension cannot be built there
# are no meaningful test results to report, and swallowing that would hand
# parse_log a truncated log that silently looks like "everything passed".
# -----------------------------------------------------------------------------
_RUN_TESTS_SH = """#!/bin/bash
# Shared by run.sh / test-run.sh / fix-run.sh. The caller has already cd'ed into
# the repository; this script inherits that working directory.
#
# No `set -e`: a non-zero pytest exit on a failing test is data, not an error, and
# it is propagated by being the last command in the script. `pipefail` is set so a
# future edit that pipes pytest through tee/grep still reports pytest's status.
set -uo pipefail

export CI=true
export CARGO_TERM_COLOR=never
export CARGO_NET_RETRY=5
export RUST_BACKTRACE=1
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

echo "### harness: rebuilding the pyo3 extension from the current sources ###"
if ! pip install -e . --no-build-isolation --no-deps; then
    echo "### harness: native extension build FAILED; no tests can run ###"
    exit 1
fi

# `-p no:cacheprovider`: .pytest_cache/ is NOT in this repo's .gitignore, so
# letting pytest write it would leave the working tree dirty and break both the
# assertion in prepare.sh and any later `git apply`.
# `-v` gives one `<nodeid> <STATUS>` line per test, which is what parse_log reads.
# `--doctest-modules --durations=10` and testpaths (tests, tantivy, src) come from
# [tool.pytest.ini_options] in pyproject.toml and are deliberately left in place.
python -m pytest -v -p no:cacheprovider
"""


class TantivyPyImageBase(Image):
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
        # `rust-toolchain.toml` at the base commit pins `channel = "1.73.0"` and
        # CI installs `toolchain: 1.73.0`. Pinning the image to the same patch
        # release means rustup resolves that file to the toolchain already inside
        # the image instead of downloading another one at build time.
        return "rust:1.73.0-bookworm"

    # Tagged per PR rather than with a constant `base`. The image produced here is
    # hardened to exactly ONE ${BASE_COMMIT} (the enhancer appends
    # `git checkout --detach` + ref deletion + `git gc --prune=now`), and
    # build_dataset skips rebuilding an image whose name already exists -- so a
    # repo-constant tag would make every tantivy-py PR resolve to the same image
    # and later instances would silently inherit the first one's checkout.
    # `repos/rust/tower_rs/tower.py` documents that exact failure measured
    # in-container. workdir() moves with the tag so the build context lands in
    # images/base-pr-<N>/.
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
        # line below into the standard `git clone "${REPO_URL}"` + `WORKDIR` +
        # `git reset --hard` + `git checkout ${BASE_COMMIT}` + history-hardening +
        # `CMD ["/bin/bash"]` block, and prepends the BuildKit syntax directive,
        # the TARGETARCH / REPO_URL / BASE_COMMIT and proxy ARGs, the shared ENV
        # block, the OCI labels and the CA-certificate symlinks. None of those are
        # written here -- doing so would duplicate what the pipeline injects.
        # Everything toolchain-related sits BEFORE the clone so those layers cache
        # independently of the commit being checked out.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{_EXTRA_SETUP}

{code}

{self.clear_env}

"""


class TantivyPyImageDefault(Image):
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
        return TantivyPyImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# The repo's own dev requirements (maturin, pytest>=4.0, mktestdocs==0.2.1).
# `|| true` per harness convention: a partially satisfied install must not abort
# the image build here -- the run scripts fail loudly and visibly instead.
pip install --no-cache-dir -r requirements-dev.txt || true

# requirements-dev.txt leaves maturin unpinned, but pyproject.toml's build-system
# declares `requires = ["maturin<=1.3.2"]`. Under --no-build-isolation the
# interpreter's maturin IS the build backend, so it is pinned to the newest
# version satisfying the constraint the project itself states.
pip install --no-cache-dir "maturin==1.3.2" || true

# Warm the baseline: compile the extension, populate the crates.io registry cache
# and fill target/ so the graded `run` stage does not compile the whole dependency
# tree from scratch (and does not have to reach the network to do it).
bash /home/run-tests.sh || true

# Warm the fix stage the same way. The fix patch touches src/query.rs, so the fix
# stage recompiles the cdylib; doing it once here means only that one crate is
# rebuilt at grading time, with every dependency already in target/.
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run-tests.sh || true

# Reverse both patches in reverse order and assert the tree is pristine again.
# `set -e` makes a failed reverse fail the image build loudly here rather than
# silently shipping a dirty checkout to the graded stages. target/, build/ and
# tantivy/tantivy.*.so are all .gitignore'd, so neither warm-up dirtied the tree.
git apply -R --whitespace=nowarn /home/fix.patch /home/test.patch
bash /home/check_git_changes.sh

# Leave the in-tree extension built from BASE sources so the image's resting
# state matches its checkout. run-tests.sh rebuilds at every stage regardless, so
# this is a consistency measure rather than something the stages depend on.
pip install -e . --no-build-isolation --no-deps || true

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

# A pytest node id: a path ending in `.py`, then zero or more `::`-separated
# components. Anchoring on `.py` is what keeps the two patterns below from
# matching prose that happens to contain the word FAILED or ERROR.
_NODE_ID = r"[^\s]+\.py(?:::[^\s]+)*"

# `-v` progress line:
#   tests/tantivy_test.py::TestClass::test_boost_query PASSED   [ 42%]
# The name group stops at the first space, so the trailing `[ 42%]` -- the only
# variable metadata pytest puts on these lines, and one that shifts as the test
# count changes between the run / test / fix stages -- is never part of the id.
_RE_PROGRESS = re.compile(
    rf"^(?P<name>{_NODE_ID})\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XPASS|XFAIL)\b"
)

# "short test summary info" line -- pytest emits these for failures and errors by
# default (`-r fE`):
#   FAILED tests/tantivy_test.py::TestClass::test_boost_query - AttributeError: ...
# The ids are identical to the progress lines, so the sets simply merge.
_RE_SUMMARY = re.compile(
    rf"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XPASS|XFAIL)\s+(?P<name>{_NODE_ID})"
)


@Instance.register("quickwit-oss", "tantivy-py")
class TantivyPy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TantivyPyImageDefault(self.pr, self._config)

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

        for raw in test_log.splitlines():
            line = _RE_ANSI.sub("", raw).strip()

            match = _RE_PROGRESS.match(line) or _RE_SUMMARY.match(line)
            if not match:
                continue

            name = match.group("name")
            status = match.group("status")

            # XPASS is a test that ran and passed. XFAIL is one that was expected
            # to fail and did, which pytest reports as a non-failure -- it is
            # grouped with skips so it can never satisfy Report.check() Rule 3 on
            # its own.
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A test reported both ways (a progress PASSED plus a teardown ERROR, or a
        # rerun) is failed. Applied in this order so TestResult's disjointness
        # invariants hold no matter what the log contained.
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
