import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# `setup.cfg` puts `--doctest-modules` and `--cov-fail-under=97` in `addopts`,
# so a plain `pytest` exits non-zero on a coverage shortfall even when every
# test passes. `-o addopts=--doctest-modules` drops the coverage gate but keeps
# the doctest flag, which is load-bearing: `tests/conftest.py` reads
# `violations.assigns` without importing it, and only the module walk that
# `--doctest-modules` performs binds that submodule attribute. Clearing
# `addopts` outright errors every test that touches the `all_violations`
# fixture. `-v` gives one nodeid per
# test for every status (including skips, which the `-rA` summary reports only
# as `file:line`); `--color=no` and `-p no:randomly` keep the log deterministic.
PYTEST_CMD = (
    "poetry run pytest -v -rA --no-header --tb=no --color=no "
    '-p no:cacheprovider -p no:randomly -o addopts=--doctest-modules'
)

SCRIPT_HEADER = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
"""


class ImageBase(Image):
    """Toolchain layer: clone, checkout, dependencies. Shared by every PR."""

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
        # Must stay on 3.10. Since 3.11 `typing.final` sets `__final__` itself,
        # so `tests/test_violations/test_codes.py::test_all_violations_are_final`
        # would already pass on the base commit and the PR would lose its
        # fail-to-pass signal. The project supports 3.9-3.12, so 3.10 is valid.
        #
        # The full image, not `-slim`: this Dockerfile installs no apt packages,
        # and `git` (needed by the clone below) plus a compiler toolchain for
        # any sdist-only dependency ship with the buildpack-deps base.
        return "python:3.10"

    def image_tag(self) -> str:
        # PR-scoped, not a shared "base". The hardening block the enhancer
        # appends detaches this image to one BASE_COMMIT and deletes every ref
        # and remote, so a single shared tag can only ever serve whichever PR
        # is built first: every later `git checkout <other sha>` in prepare.sh
        # dies with "fatal: reference is not a tree", with no remote left to
        # fetch from. One base per PR keeps each instance correctly pinned.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # `DockerfileEnhancer` prepends the syntax directive, ARGs, ENV, LABEL
        # and cert symlinks, and rewrites the `git clone` line below into the
        # full clone -> WORKDIR -> reset -> checkout -> hardening -> CMD tail.
        # Nothing may follow that line here, or it would land after the CMD.
        # Dependency installation therefore lives in prepare.sh, which the PR
        # image runs on top of this one.
        return f"""FROM {self.dependency()}

{self.global_env}

# Force every tool in this image to emit pure ASCII.
# `docker_util.build` streams buildx output through `text=True` with no
# `encoding=`, so on a Windows host Python decodes it with the locale codec
# (cp1252). pip's download bar draws U+2501 -> bytes e2 94 81, and 0x81 is
# undefined in cp1252, which kills an otherwise healthy build with
#   'charmap' codec can't decode byte 0x81 in position 422
# Verified: U+2501 was the ONLY non-ASCII codepoint in the entire build log.
ENV PIP_PROGRESS_BAR=off \\
    PIP_NO_COLOR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    POETRY_NO_INTERACTION=1 \\
    NO_COLOR=1

WORKDIR /home/

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}
"""


class ImageDefault(Image):
    """Per-PR layer: patches and run scripts on top of the shared base."""

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
            # Integrity guard for prepare.sh. Own `set -e` so a failed git
            # query aborts the check itself rather than reporting "clean".
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
""",
            ),
            # Sets the environment up. Run two ways, so it must work as both:
            #   1. `RUN bash /home/prepare.sh` in the Dockerfile below, where
            #      the delimiter lines are inert because `#` starts a comment;
            #   2. replayed chunk by chunk, split on the delimiter, by
            #      `session_util.run_prepare_cmds` before each stage.
            #
            # Deliberately no `set -e`: the chunks are replayed into the same
            # persistent bash session that later runs the test command, so
            # `set -e` would kill the session the moment the test stage exits
            # non-zero — its expected outcome — and lose the stage log.
            #
            # `poetry config virtualenvs.create false` rather than the env var:
            # this runs inside a single `RUN`, so an export would not survive
            # into later layers or the runtime container, but poetry's config
            # file does. It also keeps `poetry install` on the interpreter this
            # image pins instead of resolving one into a `.venv`.
            #
            # The installs are `|| true` because a native build failure on a
            # foreign arch is survivable; the final chunk is the gate that
            # turns a silently-swallowed failure into a failed build, and it
            # runs last so its exit code is the script's.
            File(
                ".",
                "prepare.sh",
                """cd /home/{pr.repo}
###ACTION_DELIMITER###
git reset --hard
###ACTION_DELIMITER###
bash /home/check_git_changes.sh || exit 1
###ACTION_DELIMITER###
git checkout {pr.base.sha}
###ACTION_DELIMITER###
bash /home/check_git_changes.sh || exit 1
###ACTION_DELIMITER###
pip install --no-cache-dir --progress-bar off poetry==1.8.3 || true
###ACTION_DELIMITER###
poetry config virtualenvs.create false || true
###ACTION_DELIMITER###
poetry install --no-interaction --no-ansi || true
###ACTION_DELIMITER###
echo '{cmd}' > /home/test_commands.sh
###ACTION_DELIMITER###
python -c "import sys, dotenv_linter, pytest; assert sys.version_info[:2] == (3, 10), sys.version\"""".format(
                    pr=self.pr, cmd=PYTEST_CMD
                ),
            ),
            File(
                ".",
                "run.sh",
                (SCRIPT_HEADER + "{cmd}\n").format(repo=self.pr.repo, cmd=PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                (
                    SCRIPT_HEADER
                    + """if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{cmd}
"""
                ).format(repo=self.pr.repo, cmd=PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                (
                    SCRIPT_HEADER
                    + """if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{cmd}
"""
                ).format(repo=self.pr.repo, cmd=PYTEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {self.dependency().image_full_name()}


{copy_commands}

RUN bash /home/prepare.sh
"""


@Instance.register("wemake-services", "dotenv-linter")
class DOTENV_LINTER(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Colour is disabled on the command line, but strip anyway: if the
        # runner ever allocates a pty, unstripped escapes would break every
        # pattern below and yield a silent 0/0/0 result.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # `-v` progress line, the authoritative source. Every status is
        # reported against a full nodeid, so all three stages share one
        # namespace:  `tests/test_cli/test_version.py::test_x PASSED [ 11%]`
        verbose_pattern = re.compile(
            r"^(?P<name>\S+::.*?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        # `-rA` summary line, which also carries collection errors that never
        # reach a progress line:  `FAILED tests/x.py::test_x - AssertionError`
        # The `SKIPPED [1] tests/x.py:12: reason` form is deliberately NOT
        # parsed: its `file:line` is not a nodeid and the line number shifts
        # once a patch is applied, so the same skip would be recorded under a
        # different name in each stage.
        summary_pattern = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS)\s+"
            r"(?P<name>.+?)(?:\s+-\s.*)?$"
        )

        for line in clean_log.splitlines():
            line = line.strip()

            match = verbose_pattern.match(line) or summary_pattern.match(line)
            if not match:
                continue

            name = match.group("name")
            status = match.group("status")
            if status == "SKIPPED":
                skipped_tests.add(name)
            elif status in {"FAILED", "ERROR"}:
                failed_tests.add(name)
            else:
                # PASSED, plus XFAIL/XPASS: `xfail_strict` is unset in
                # `setup.cfg`, so neither counts as a failure.
                passed_tests.add(name)

        # A test reported more than one way (e.g. passed, then errored in
        # teardown) counts as failed. Required for TestResult's disjointness.
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
