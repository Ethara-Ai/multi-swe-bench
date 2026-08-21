import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Python 3.9, NOT the 3.7 that requirements_travis.txt's `torch==1.3.1` pin
# implies. 3.7 is what makes this repo un-buildable on arm64: no jaxlib wheel
# exists for arm64 + cp37 on any index, and conftest.py imports jax at module
# scope, so an arm64 image would collect zero tests. 3.9 is the lowest
# interpreter where every backend publishes wheels for BOTH architectures.
# The full python:3.9 image rather than -slim because scipy/h5py fall back to
# building from source when no wheel matches, and that needs a compiler
# toolchain the slim variant does not carry.
PYTHON_IMAGE = "python:3.9"

# ---------------------------------------------------------------------------
# MULTI-ARCH (linux/amd64 + linux/arm64).
#
# Getting arm64 to work meant moving OFF the repo's own pinned dependency set,
# so the trade-off is recorded here rather than hidden:
#
#   requirements_travis.txt pins torch==1.3.1 (January 2020)
#     -> torch 1.3.1 ships no wheel above cp37, forcing Python 3.7
#     -> arm64 + cp37 has NO jaxlib on any index (PyPI or Google's jax index)
#     -> conftest.py does `import jax` at module scope
#     -> an arm64 image built that way collects ZERO tests.
#
# The chain only breaks at the interpreter. On Python 3.9 every backend
# publishes wheels for both architectures, so the pins below are raised to the
# oldest set that satisfies that AND still runs this era's code:
#
#   tensorflow  <2.16   conftest.py calls tf.compat.v1.enable_v2_behavior(),
#                       which TensorFlow REMOVED in 2.16 - a newer pin makes
#                       every test fail at collection.
#   numpy       <2      TensorFlow 2.15 is not built against the numpy 2 ABI.
#
# What this costs: the suite is graded against newer libraries than the PR was
# reviewed with. That is a real deviation, and it is the price of arm64 - there
# is no version set that is both era-faithful and arm64-installable. The
# single-arch, era-faithful configuration is preserved beside this file as
# tensornetwork_py37_singlearch.py.bak; restore it if fidelity matters more
# than architecture coverage.
# ---------------------------------------------------------------------------

# conftest.py imports jax, tensorflow AND tensornetwork at module scope, and its
# `backend` fixture parametrises every test over
# ["numpy", "tensorflow", "jax", "pytorch"]. So all four backends must import or
# pytest fails during COLLECTION and the stage reports nothing - none of them is
# optional, even though only numpy is the default backend.
#
# Every version is pinned rather than left open. Two reasons:
#   * Reproducibility - unpinned, pip walks down from the newest release testing
#     each against Python 3.7, which is slow (it downloads wheels just to read
#     metadata) and lands on whatever is newest today.
#   * every pin here is also an ARCHITECTURE constraint - see the banner above.
#     jax and jaxlib must move together (they are released as a matched pair),
#     and both must be a version that publishes arm64 wheels.
# SINGLE quotes around numpy<2, not double. This string is interpolated into a
# `RUN /bin/bash -o pipefail -c "( ... )"` line, so a double quote here would
# terminate that outer string and produce a malformed command; the `<` also has
# to stay quoted or the shell reads it as an input redirect.
BACKEND_DEPS = (
    "'numpy<2' tensorflow==2.15.1 torch==2.2.2 jax==0.4.30 jaxlib==0.4.30"
)

# Every byte this image emits at BUILD time is forced down to printable ASCII
# (plus tab/LF/CR). The harness streams `docker buildx` output through
# subprocess with `text=True` and no explicit encoding, so a Windows host
# decodes it as cp1252 - and any UTF-8 byte outside that map aborts the build
# with "'charmap' codec can't decode byte 0x81" before a single layer exists.
# pip's progress bar alone triggers it (U+2501 -> E2 94 81). Runtime logs are
# decoded as UTF-8 by the harness, so only build-time commands are wrapped.
ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"


def _test_files_from_patch(patch: str) -> list[str]:
    """Test files the gold patch touches, used as the pytest target.

    Scoped to these rather than the whole tests/ tree: every test is
    parametrised over four backends, so the full suite multiplies out into a
    very long run for signal that has nothing to do with this PR.
    """
    files = {
        p for p in re.findall(r"^diff --git a/(.+?) b/", patch or "", re.M)
        if p.endswith(".py") and ("test" in p.rsplit("/", 1)[-1])
    }
    return sorted(files)


class ImageBase(Image):
    """Per-PR base: interpreter + ML backends + the repo frozen at BASE_COMMIT.

    Tagged `base-pr-<N>`, so the tag names the pull request whose code is inside
    it. A single shared `base` tag cannot make that promise - the first PR to
    build it freezes it, and every later PR silently inherits the wrong commit
    while the tag still reads `base`.

    The clone below is deliberately the bare `RUN git clone <url> /home/<repo>`
    form. That exact shape is what DockerfileEnhancer._standardize_repo_fetch
    matches, and its rewrite is what supplies the REPO_URL/BASE_COMMIT clone,
    the checkout, the history-sanitising scrub (remove origin, delete every ref,
    expire reflogs, gc/repack, then assert none of it leaked) and the final CMD.
    Decorate that line - wrap it in bash, pipe it through a filter, change the
    path - and the enhancer no longer recognises it, so the clone stays raw and
    the entire hardening block is silently never injected.

    The backend wheels are installed here, before the clone, so the expensive
    layer is cached independently of the source tree.
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
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        # Kept in step with image_tag so the generated Dockerfile lands in
        # images/base-pr-<N>/, which is where a base file is expected to live.
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

        # No `ENV DEBIAN_FRONTEND` here - DockerfileEnhancer already sets it
        # (with LANG and TZ) on every base image, and repeating it only
        # duplicates the line in the generated Dockerfile.
        #
        # WORKDIR precedes the apt RUN so the working directory is established
        # before the first network operation, matching the reference layout.
        return f"""FROM {image_name}

{self.global_env}

ENV PIP_PROGRESS_BAR=off \\
    PIP_NO_COLOR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONIOENCODING=utf-8 \\
    PYTHONUNBUFFERED=1 \\
    NO_COLOR=1 \\
    PY_COLORS=0

WORKDIR /home/

RUN /bin/bash -o pipefail -c "( apt-get update && apt-get install -y --no-install-recommends git ca-certificates graphviz && rm -rf /var/lib/apt/lists/* ) 2>&1 | {ASCII_FILTER}"

# All four backends come from PyPI now; the Google jax index the 3.7 build
# needed only ever carried cp37 wheels and is no longer required.
RUN /bin/bash -o pipefail -c "( python -m pip install --no-cache-dir --upgrade pip setuptools wheel && python -m pip install --no-cache-dir pytest {BACKEND_DEPS} ) 2>&1 | {ASCII_FILTER}"

{code}

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        targets = _test_files_from_patch(self.pr.test_patch)
        target = " ".join(targets) if targets else "tensornetwork/tests"

        # `python -m pytest` (not the bare `pytest` script) so the repo root
        # lands on sys.path and `import tensornetwork` resolves to the checkout
        # being tested rather than anything pip may have installed.
        # -rA prints one PASSED/FAILED/ERROR line per test, which parse_log reads.
        #
        # --continue-on-collection-errors keeps the middle stage honest. pytest's
        # default is to abort the whole session when any file fails to import,
        # which would drop every unrelated test out of the test stage; they would
        # reappear at the fix stage and read as "the fix repaired them" when it
        # repaired nothing.
        #
        # This exact string is shared by all three stage scripts, so the ONLY
        # difference between them is which patches are applied - which is what
        # makes the fail->pass signal attributable to the fix.
        test_cmd = (
            f"python -m pytest -rA --tb=short -v -p no:cacheprovider"
            f" --continue-on-collection-errors {target}"
        )

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

# Integrity guard. prepare.sh calls this immediately after `git reset --hard`
# and again after `git checkout <BASE_COMMIT>`, so a tree that did not actually
# come back clean aborts the BUILD instead of being baked into the image and
# silently contaminating all three graded stages.
#
# `git status --porcelain` is empty only when nothing is modified, staged or
# untracked - deliberately stricter than `git diff --quiet`, because the failure
# this catches is usually a leftover UNTRACKED file (`git clean -qfd` does not
# remove ignored files without -x, and a pytest run leaves __pycache__ behind).
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
# Assert the reset really produced a clean tree. Without this a leftover file
# would be baked into the image and silently inherited by all three graded
# stages - `git clean -qfd` does not remove ignored files (no -x), so a stale
# artefact can survive a reset without anyone being told.
bash /home/check_git_changes.sh

# The base image is frozen at ONE commit and has had its origin remote stripped
# by the hardening block, so a commit that is not already present cannot be
# resolved locally and `git fetch origin` has no remote to use. Ask GitHub for
# that exact commit by sha over the full URL - GitHub still serves commits no
# branch points at.
#
# A fetch drags fresh git objects into an image whose history the base
# deliberately stripped, so whatever it brings in has to be stripped again.
# FETCHED records whether that happened; the block after the checkout re-runs
# the scrub only in that case. With per-PR bases the fetch should never fire -
# the base was built from this very sha - but the guard keeps the image clean if
# it ever does.
FETCHED=0
if ! git cat-file -e {sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{org}/{repo}.git {sha}
    FETCHED=1
fi
git checkout {sha}
# Assert again: this is the exact state the graded stages start from.
bash /home/check_git_changes.sh

if [ "$FETCHED" = "1" ]; then
    git checkout --detach {sha}
    git remote remove origin 2>/dev/null || true
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d
    git reflog expire --expire=now --all
    git reflog expire --expire-unreachable=now --all
    git gc --prune=now --aggressive
    git repack -a -d -l --quiet
    rm -f .git/objects/info/alternates
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
    test -z "$(git remote)"
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
fi

# Runtime deps only; the heavy ML backends already live in the base layer.
python -m pip install --no-cache-dir -r requirements.txt || true

# Warm the import/collection path once so the scored stages do not pay
# TensorFlow's first-import cost; the outcome here is irrelevant to grading,
# hence `|| true` - a warm run must never decide the build.
{test_cmd} > /dev/null 2>&1 || true
# Undo everything the warm run left behind (.pytest_cache, __pycache__, any
# file the pip install dropped in the tree) so the image ships clean.
git reset --hard
git clean -qfdx
""".format(
                    repo=self.pr.repo,
                    org=self.pr.org,
                    sha=self.pr.base.sha,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfdx
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfdx
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfdx
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

# `-o pipefail` so a failing prepare.sh still fails the build: without it the
# pipeline would report the exit status of `tr`, which always succeeds, and a
# broken image would be published as if it were good.
RUN /bin/bash -o pipefail -c "bash /home/prepare.sh 2>&1 | {ASCII_FILTER}"

{self.clear_env}

"""


@Instance.register("google", "TensorNetwork")
class TensorNetwork(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        log = ansi_escape.sub("", test_log)

        # Two shapes carry a per-test result and both are matched, so a stage is
        # never silently read as empty:
        #   -v progress line : tests/x_test.py::test_name[jax] PASSED  [ 42%]
        #   -rA summary line : PASSED tests/x_test.py::test_name[jax]
        # The [backend] suffix is part of the id and is kept - the same test can
        # pass on numpy and fail on pytorch, and collapsing them would hide that.
        verbose_re = re.compile(
            r"^(\S+\.py::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        summary_re = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+\.py::\S+)"
        )
        # A test patch referencing a method the fix has not added yet fails at
        # COLLECTION, where pytest names the file with no `::nodeid`. Captured so
        # the stage records the failure instead of nothing.
        collect_err_re = re.compile(r"^ERROR\s+(\S+\.py)\s*$")

        def record(name: str, status: str) -> None:
            if status in ("PASSED", "XPASS"):
                # A failure already recorded for this id wins: a duplicate line
                # must not launder a failing test into a passing one.
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            else:  # SKIPPED / XFAIL
                if name in passed_tests or name in failed_tests:
                    return
                skipped_tests.add(name)

        for raw_line in log.splitlines():
            line = raw_line.strip()

            m = verbose_re.match(line)
            if m:
                record(m.group(1), m.group(2))
                continue

            m = summary_re.match(line)
            if m:
                record(m.group(2), m.group(1))
                continue

            m = collect_err_re.match(line)
            if m:
                record(m.group(1), "ERROR")

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
