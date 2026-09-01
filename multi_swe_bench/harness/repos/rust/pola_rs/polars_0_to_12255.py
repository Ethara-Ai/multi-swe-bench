"""pola-rs/polars PRs up to #12255 - Rust core graded through the py-polars pytest suite.

Every value below was read off the repo at base commit a9d25281, not inferred:

  rust-toolchain.toml     channel = "nightly-2022-11-24"
  Cargo.toml (root)       13-member workspace; py-polars is NOT a member
  py-polars/Cargo.toml    its own `[workspace]`; depends on polars-core and polars-lazy BY PATH
                          default = ["all", "nightly"]; "all" includes extract_jsonpath
  py-polars/pyproject.toml  requires-python >=3.7; build-backend maturin>=0.13,<0.14
  py-polars/requirements-dev.txt  maturin==0.13.7 · pytest==7.2.0 · hypothesis==6.57.1
                          numpy · pandas · pyarrow · xlsx2csv · deltalake
  py-polars/Makefile      `test:` = venv + build + `pytest tests/unit/`
  workflows/test-python.yaml  python 3.7 and 3.11 · toolchain nightly-2022-11-24
                          `maturin develop` with RUSTFLAGS="-C debuginfo=0" · then `pytest`

Four things are worth knowing before changing anything here.

1. THIS INSTANCE IS GRADED BY PYTEST, NOT BY `cargo test`. The previous revision ran
   `cargo test --workspace`, and for PR #5759 that command cannot observe its own test patch:
   the patch's only file is `py-polars/tests/unit/test_datelike.py`, and py-polars is not a
   member of the root workspace. All three stages returned an identical Rust suite, nothing
   transitioned, and Report.check() rejects at rule 3 ("no test cases transitioned from failed
   to passed"). Grading through py-polars fixes that without losing the Rust signal: py-polars
   depends on polars-core and polars-lazy by PATH, so `maturin develop` compiles the fix
   patch's changes under polars/polars-time/ into the extension the tests import. For #5759
   that is precisely the mechanism under test - `dt.truncate("1w")` in Python is the Rust
   `windows/duration.rs` change.

2. Every graded stage rebuilds. `maturin develop` runs in all three scripts, not just in
   prepare.sh, because the fix patch changes Rust source and the extension must be recompiled
   before the Python tests can see it. prepare.sh warms ~/.cargo and target/ so the later
   rebuilds are incremental. RUSTFLAGS="-C debuginfo=0" is copied from the project's own
   workflow, where it exists to keep the compile inside the runner's memory budget.

3. No jsonpath rewrite. The previous revision ran a `sed` over every Cargo.toml to replace
   polars-ops' `jsonpath_lib` git dependency, justified as "repo no longer exists". That is
   false - github.com/ritchie46/jsonpath and its `improve_compiled` branch both resolve
   (HTTP 200, checked). Worse, the sed mutated a tracked file and nothing restored it, so the
   shipped image had a dirty tree and no stage began at the base commit. The dependency is
   reachable and `extract_jsonpath` is in py-polars' default feature set, so it is left to
   resolve normally. If it ever does break, prepare.sh's warm-up records the failure in
   /home/.warm_status rather than silently rewriting the manifest.

4. The base image is pinned twice over. python:3.11-slim-bullseye fixes the interpreter (3.11
   is in the project's CI matrix), and rustup installs exactly nightly-2022-11-24 - the channel
   both rust-toolchain.toml and the workflow name. The previous revision used `rust:latest`,
   which floats the foundation under instances that were already built and verified.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Named once so the base image, prepare.sh and the graded stages cannot drift apart.
RUST_TOOLCHAIN = "nightly-2022-11-24"

# `pytest tests/unit/` is the project's own `make test` target, and the directory the test
# patch writes into. `-v -rA` is what names every test including the passing ones (`-rA`'s
# short summary is the only place a PASS gets its full node id). `-p no:cacheprovider` stops
# .pytest_cache appearing in the tree between stages. `-p no:randomly` is deliberately absent -
# the plugin is not in requirements-dev.txt.
PYTEST_CMD = "pytest tests/unit/ -v -rA --tb=no -p no:cacheprovider"

# Rebuild step, identical in all three graded stages. `--` separates maturin's args from
# cargo's; nothing extra is passed, so the default feature set (all + nightly) is used, exactly
# as the project's workflow does.
MATURIN_BUILD = "maturin develop"

# Exported by every script rather than declared as ENV in the base image, so the generated
# Dockerfile carries exactly one ENV instruction - the one DockerfileEnhancer injects.
#
#   PATH             Puts the venv and ~/.cargo/bin ahead of everything, which is what makes
#                    `pytest` and `maturin` resolve without sourcing an activate script.
#   VIRTUAL_ENV      maturin refuses to `develop` unless it can see a virtualenv; setting this
#                    alongside PATH is the non-interactive equivalent of `source activate`.
#   RUSTFLAGS        From the project's own workflow - keeps debug symbols out of a build that
#                    is already large enough to be the memory ceiling on a CI box.
#   CARGO_TERM_COLOR Cargo colourises on a terminal. parse_log strips ANSI anyway, but turning
#                    it off at the source keeps the captured log diffable between stages.
#   CI / PY_COLORS   Check 2C/3C baseline, and the pytest half of the same colour argument.
SHELL_ENV = """\
export CI=true
export PY_COLORS=0
export CARGO_TERM_COLOR=never
export RUSTFLAGS="-C debuginfo=0"
export VIRTUAL_ENV=/home/polars/py-polars/venv
export PATH="$VIRTUAL_ENV/bin:/root/.cargo/bin:$PATH\""""


class PolarsEarlyImageBase(Image):
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
        return "python:3.11-slim-bullseye"

    def image_tag(self) -> str:
        # `base-pr-<N>` - the literal form the Dockerfile QC expects. Per-PR, not a tag
        # shared across the era: dependency() returns a plain string, so DockerfileEnhancer
        # always rewrites this file, and _standardize_repo_fetch turns the clone line below
        # into `git clone ${REPO_URL}` + `git checkout ${BASE_COMMIT}` plus a hardening block
        # that detaches at that one commit. A shared tag would let whichever PR built first
        # pin the commit for all the others.
        #
        # No era qualifier is needed for uniqueness (Check 2F): a PR routes to exactly ONE era,
        # and the tag embeds that PR number, so this era and the late era
        # (polars_12256_to_99999, tag "base-late") can never mint the same tag.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

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

        # No ENV of its own: the enhancer already supplies DEBIAN_FRONTEND, LANG and TZ, and the
        # toolchain variables live in SHELL_ENV, which every script exports.
        #
        # apt: build-essential and pkg-config are required, not defensive - the -slim image has
        # no compiler, py-polars links jemallocator on linux, and several of the Python
        # dependencies build from source where no wheel matches the platform. cmake is needed
        # by the arrow/parquet crates' native build scripts.
        #
        # rustup pins the exact nightly that rust-toolchain.toml AND
        # .github/workflows/test-python.yaml both name, installed via --default-toolchain
        # rather than left to a directory override, so the image is self-describing and no
        # graded stage can silently trigger a download. The toolchain is resolved for the build
        # platform, so one Dockerfile serves amd64 and arm64.
        #
        # The trailing `&& rustc --version && cargo --version` is a hard gate, not a courtesy
        # print. A Dockerfile RUN executes under /bin/sh, which has no `pipefail`, so in
        # `curl ... | sh` the reported exit status is sh's and NOT curl's - a truncated or
        # failed download would exit 0 and leave a Rust-less image that only breaks much later
        # inside `maturin develop`. The `&&` chain is the only guard available at this layer.
        #
        # It lives here rather than in prepare.sh so the base image is self-describing for a
        # repos/rust/ instance and the toolchain is downloaded once per base image instead of
        # on every PR-image rebuild.
        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        build-essential ca-certificates cmake curl git pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \\
        | sh -s -- -y --profile minimal --default-toolchain {RUST_TOOLCHAIN} \\
    && /root/.cargo/bin/rustc --version \\
    && /root/.cargo/bin/cargo --version

{code}

{self.clear_env}

"""


class PolarsEarlyImageDefault(Image):
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
        return PolarsEarlyImageBase(self.pr, self._config)

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
                """\
#!/bin/bash
set -e

{shell_env}

# The Rust toolchain is supplied by the base image (see its dockerfile()), already gated on
# `rustc --version`. Re-asserted here so a stage that somehow lost it fails at this line
# rather than deep inside maturin.
rustc --version
cargo --version

cd /home/{pr.repo}
git reset --hard
# Assert the reset actually produced a clean tree rather than assuming it did. A stray modified
# file would flow into all three graded stages and corrupt the comparison with nothing in the
# log to explain why.
bash /home/check_git_changes.sh

git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# The venv lives where SHELL_ENV's VIRTUAL_ENV points, inside py-polars, matching the layout
# the project's own Makefile creates. It is gitignored, so it survives the `git reset --hard`
# at the end of this script and is still there for every graded stage.
cd /home/{pr.repo}/py-polars
python -m venv venv

# Warm the wheel cache and ~/.cargo into this image layer so the graded stages neither pay for
# the download nor depend on the network.
#
# `timeout` is not belt-and-braces on top of `|| true`. `|| true` handles a command that FAILS;
# a command that HANGS never returns, so it never reaches the `||` at all - and Docker has no
# per-step timeout. pip or cargo blocking on a half-dead index is exactly that shape.
#
# `|| true` still matters on its own: a build failure at the base commit is a legitimate state
# for some PRs and must not fail the image build. The verdict is recorded so a hollow image is
# DETECTABLE afterwards. Inspect with: docker run <image> cat /home/.warm_status
warm() {{
  if timeout "$2" bash -c "$3" > /tmp/warm.log 2>&1; then
    echo "warm-up $1: OK" >> /home/.warm_status
  else
    echo "warm-up $1: INCOMPLETE (exit $?)" >> /home/.warm_status
    tail -40 /tmp/warm.log || true
  fi
}}

warm deps 1800 "pip install --upgrade pip && pip install -r requirements-dev.txt"

# Compiling polars is the expensive step by a wide margin, which is exactly why it is done here
# rather than left to the first graded stage: the target/ directory it fills makes each stage's
# rebuild incremental. 3600s, not 1800 - a cold full build of the workspace does not finish in
# half an hour on a modest box.
warm build 3600 "{maturin}"

cat /home/.warm_status

# Hard gates. Without them a failed warm-up produces an image that builds clean and then
# reports 0/0/0 from all three stages - Report.check() rejects it at rule 1 with nothing in the
# log pointing at the build. Fail here instead, where the cause is on screen.
pytest --version
maturin --version
python -c "import polars; print('polars', polars.__version__)"

# Back to pristine so the graded stages apply their own patches cleanly. Plain `git reset
# --hard`, NOT `git clean -fdx`: venv/ and target/ are ignored files, and -x would delete the
# entire warm-up this script just paid for. Nothing tracked is left modified - the build writes
# only into those two ignored directories - which is what the assertion below confirms.
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
""".format(pr=self.pr, shell_env=SHELL_ENV, maturin=MATURIN_BUILD),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{pr.repo}/py-polars
{maturin}
{pytest}
""".format(pr=self.pr, shell_env=SHELL_ENV, maturin=MATURIN_BUILD, pytest=PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
# Rebuild even though this patch touches only Python: the three stages must run the same
# sequence, or a difference in build state reads as a difference in test outcome. The rebuild
# is incremental against the warm target/ and is close to a no-op here.
cd /home/{pr.repo}/py-polars
{maturin}
{pytest}
""".format(pr=self.pr, shell_env=SHELL_ENV, maturin=MATURIN_BUILD, pytest=PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{pr.repo}
# test.patch first, then fix.patch - separate invocations so a failure names which one failed.
# They touch disjoint files here (test.patch only py-polars/tests/, fix.patch the Rust crates
# plus py-polars/polars/), but the order is the graded contract regardless.
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
# This is the rebuild that matters: fix.patch changes polars/polars-time/, which py-polars
# depends on by path, so without recompiling the extension the Python tests would still be
# exercising the unfixed Rust.
cd /home/{pr.repo}/py-polars
{maturin}
{pytest}
""".format(pr=self.pr, shell_env=SHELL_ENV, maturin=MATURIN_BUILD, pytest=PYTEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Generated from files() rather than hard-coded, so a file added there can never be
        # written into the build context yet left uncopied - which would surface at build time
        # as `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/{f.name}\n" for f in self.files())

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# The test command runs from py-polars/, so pytest reports node ids relative to that directory
# (`tests/unit/test_datelike.py::test_x`). test_patch_files holds repo-relative paths
# (`py-polars/tests/unit/test_datelike.py`), and report.py's _test_name_matches_files compares
# the two by path. Prefixing here is what lets an n2p test be attributed to the patch that
# introduced it.
_ID_PREFIX = "py-polars/"

# pytest's `-rA` short-summary block - the authoritative source, and the only place a PASSING
# test is named with its full node id.
#
#   PASSED tests/unit/test_datelike.py::test_truncate_by_calendar_weeks
#   FAILED tests/unit/test_datelike.py::test_truncate_by_calendar_weeks - AssertionError
#
# The trailing ` - <reason>` is stripped: the reason text differs between stages (different
# assertion values), and a name carrying it would make one test read as two and manufacture
# transitions that never happened (Check 4B).
_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<name>[^\s]+?)(?:\s+-\s+.*)?$"
)

# pytest's `-v` progress line, the fallback when a stage dies before the summary block.
#
#   tests/unit/test_datelike.py::test_truncate_by_calendar_weeks PASSED [ 42%]
#
# The percentage is outside the capture on purpose - it shifts with the size of the suite,
# which is exactly what changes between the run and fix stages.
_PROGRESS_RE = re.compile(
    r"^(?P<name>[^\s]+::[^\s]+)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b"
)

_FAIL_STATUSES = {"FAILED", "ERROR"}
_SKIP_STATUSES = {"SKIPPED", "XFAIL", "XPASS"}


def parse_pytest_log(test_log: str) -> TestResult:
    """Parse a graded stage's output into per-test results.

    Both the `-rA` summary and the `-v` progress lines are read and unioned. They name a test
    identically, so a test present in both contributes one entry; reading both means a stage
    killed before it could print the summary still reports what it got through.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Strip ANSI FIRST. pytest colourises whenever it believes it is attached to a terminal,
    # and a single escape sequence in front of PASSED defeats every pattern below - the stage
    # then reports 0/0/0 and Report.check() rejects it at rule 1 with no clue why.
    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _SUMMARY_RE.match(line) or _PROGRESS_RE.match(line)
        if not match:
            continue

        name = match.group("name")
        # A summary line for a collection error names a FILE, not a node id. Keeping it is
        # correct - a file that fails to import is a real regression - but a bare word from
        # some other log line is not, so require something that looks like a path or node id.
        if "::" not in name and "/" not in name:
            continue
        name = _ID_PREFIX + name

        status = match.group("status")
        # Worst result wins, applied as we go rather than as a post-pass, so a test reported
        # twice can never end up in two buckets at once. TestResult.__post_init__ raises
        # ValueError on any overlap, which would abort the whole instance rather than mis-grade
        # it.
        if status in _FAIL_STATUSES:
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif name not in failed_tests:
            if status in _SKIP_STATUSES:
                if name not in passed_tests:
                    skipped_tests.add(name)
            else:
                skipped_tests.discard(name)
                passed_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("pola-rs", "polars_0_to_12255")
class POLARS_0_TO_12255(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PolarsEarlyImageDefault(self.pr, self._config)

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
        return parse_pytest_log(test_log)
