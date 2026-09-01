"""jbsparrow/CyberDropDownloader - Poetry-managed Python package, pytest + asyncio.

Modelled on typescript/coder/code_server.py: one shared install snippet used by
prepare.sh AND all three graded scripts, era-detection instead of hardcoded layout,
and a fallback behind every step so an optional path that is missing cannot kill a
stage.

Two facts about PR 982 drive the design:

1. fix_patch EDITS pyproject.toml. It adds [tool.coverage.*] and rewrites
   [tool.pytest.ini_options]:
       -asyncio_default_fixture_loop_scope = "module"
       +asyncio_default_fixture_loop_scope = "function"
       +addopts = ["-s"]
   pytest reads that file at collection time, so the install and the test run must
   both happen AFTER the patch is applied in each stage. Installing once in the base
   image would make every stage run under the base commit's pytest configuration and
   silently erase the difference the fix makes.

2. The tests are async (`async def test_...`) and the project sets
   `asyncio_mode = "auto"`, so pytest-asyncio must be present or every async test
   errors at collection rather than running.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# --- Shared install helper used by prepare.sh, run.sh, test-run.sh, fix-run.sh ---
#
# Poetry is the declared manager ([tool.poetry] in pyproject.toml, with pytest-cov
# and pytest-mock as dev dependencies), but the invocation differs across Poetry
# majors: 1.x installs dev dependencies by default and takes --no-dev, 2.x uses
# --with/--without groups. Rather than pin a Poetry version, try the modern form,
# fall back to the older one, then fall back to pip entirely.
#
# `virtualenvs.create false` keeps everything in the image's system interpreter so
# the graded stages do not have to know about a venv path.
_INSTALL_SNIPPET = r"""
# ---------- smart install ----------
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_ROOT_USER_ACTION=ignore
export POETRY_NO_INTERACTION=1

if command -v poetry >/dev/null 2>&1 && grep -q '^\[tool\.poetry\]' pyproject.toml 2>/dev/null; then
    poetry config virtualenvs.create false 2>/dev/null || true
    poetry install --no-interaction --no-ansi 2>&1 \
        || poetry install --no-interaction --no-ansi --with dev 2>&1 \
        || true
fi

# Belt and braces. If Poetry could not resolve (a lock file out of step with a
# patched pyproject.toml is the common case here, since fix_patch edits it), the
# package still has to be importable and the test tooling still has to exist.
python -c "import cyberdrop_dl" 2>/dev/null \
    || pip install -e . 2>&1 \
    || pip install . 2>&1 \
    || true

# asyncio_mode = "auto" means every `async def test_` silently ERRORs at collection
# without this plugin - which reads downstream as "those tests do not exist" rather
# than as a missing dependency.
python -c "import pytest_asyncio" 2>/dev/null \
    || pip install "pytest>=8.3" pytest-asyncio pytest-cov pytest-mock 2>&1 \
    || true
# ---------- end smart install ----------
"""

# testpaths = ["tests"] in pyproject.toml, so bare pytest already targets the right
# directory - do not hardcode a path here or a test that moves would vanish.
#
# -v                              one line per test, which parse_log reads
# -p no:cacheprovider             no .pytest_cache written into the work tree
# --continue-on-collection-errors one unimportable module must not hide the results
#                                 of every other module
# || true                         a non-zero exit is expected in the run/test stages;
#                                 the report is what matters, not the exit code
_TEST_CMD = (
    "python -m pytest -v -p no:cacheprovider --continue-on-collection-errors 2>&1 || true"
)


class ImageBase(Image):
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
        # Returning a str (not an Image) is what keeps DockerfileEnhancer engaged,
        # and the enhancer is what performs the clone rewrite, the BASE_COMMIT
        # checkout and the git history scrub.
        return "python:3.12-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # The defaults (ca-certificates, curl, build-essential, git, gnupg, make,
        # python3, sudo, wget) already cover everything needed to build the C
        # extensions in this dependency tree. Nothing repo-specific to add.
        return []

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}` and before the hardening block,
        # so the tree is at the right commit.
        #
        # Poetry is installed here rather than in the graded scripts so the network
        # is not needed mid-run. The dependency warm-up is deliberately tolerant:
        # a resolution failure at the base commit must not stop the image sealing,
        # because the run stage is what is supposed to measure that.
        return (
            "RUN pip install --no-cache-dir poetry\n\n"
            "RUN poetry config virtualenvs.create false || true\n\n"
            "RUN poetry install --no-interaction --no-ansi > /dev/null 2>&1 || true\n\n"
            "RUN pip install --no-cache-dir -e . > /dev/null 2>&1 || true\n\n"
            'RUN pip install --no-cache-dir "pytest>=8.3" pytest-asyncio pytest-cov '
            "pytest-mock > /dev/null 2>&1 || true"
        )

    # dockerfile() is deliberately NOT overridden - Image.dockerfile() already emits
    # FROM -> global_env -> apt -> clone -> checkout BASE_COMMIT -> extra_setup ->
    # hardening/scrub -> CMD, and DockerfileEnhancer wraps it with the syntax pin,
    # ARGs, proxy ENV, CA symlinks and labels.


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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self._config)

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
                """#!/bin/bash
set -e
cd /home/{repo}
git reset --hard

# MUST come before check_git_changes.sh, not after. The base image already ran
# `pip install -e .`, which writes *.egg-info/ into the work tree, and this repo
# does not gitignore it - so the tree is already untracked-dirty by the time this
# script runs and the check would fail the image before any stage executes
# (`git reset --hard` restores tracked files but never removes untracked ones).
#
# .git/info/exclude is git's own per-clone ignore list and is NOT a tracked file, so
# this changes nothing about the code under test and `git apply` is unaffected. The
# check then tests what it is meant to test: that no SOURCE file differs from
# BASE_COMMIT.
printf '*.egg-info/\\n.pytest_cache/\\n__pycache__/\\n.coverage\\n' >> .git/info/exclude

bash /home/check_git_changes.sh

# Re-assert the baseline in the PR layer itself. The base image's hardening block
# already asserts HEAD == BASE_COMMIT, and nothing between that and this script moves
# HEAD, so this is defence in depth rather than a fix for a live drift. It is kept
# because it makes the PR layer independently verifiable: if a future step is added
# above that touches the tree, this catches it instead of silently grading the wrong
# commit.
git checkout {sha}
bash /home/check_git_changes.sh

{install}
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    install=_INSTALL_SNIPPET.strip(),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo}

{install}

{test}
""".format(repo=self.pr.repo, install=_INSTALL_SNIPPET.strip(), test=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{install}

{test}
""".format(repo=self.pr.repo, install=_INSTALL_SNIPPET.strip(), test=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{install}

{test}
""".format(repo=self.pr.repo, install=_INSTALL_SNIPPET.strip(), test=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Thin by design: the clone, the BASE_COMMIT checkout, the scrub and the
        # toolchain all live in the base layer. DockerfileEnhancer does not apply
        # here because dependency() returns an Image rather than a str.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("jbsparrow", "CyberDropDownloader")
class CyberDropDownloader(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI colour; pytest emits it whenever it thinks it has a tty, and the
        # codes sit between the test id and its status.
        log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", log)

        # `-v` line form:  tests/test_manager.py::TestMerge::test_overwrite PASSED [ 12%]
        # The id may carry a parametrisation in brackets, which can itself contain
        # spaces, so the id is matched up to the LAST whitespace before the status
        # rather than with \S+.
        verbose_re = re.compile(
            r"^(?P<name>\S+::.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        # Summary form (pytest -ra / short summary):  FAILED tests/x.py::test_y - Err
        summary_re = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<name>\S+::\S+)"
        )

        def record(name: str, status: str) -> None:
            name = name.strip()
            if not name:
                return
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:  # SKIPPED, XFAIL
                skipped_tests.add(name)

        for line in log.splitlines():
            line = line.rstrip()
            m = verbose_re.match(line) or summary_re.match(line)
            if m:
                record(m.group("name"), m.group("status"))

        # A test can appear twice - once in the verbose run and again in the short
        # summary, or via a rerun. Enforce one bucket each, worst status winning, or
        # the stage comparison double-counts and invents transitions.
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
