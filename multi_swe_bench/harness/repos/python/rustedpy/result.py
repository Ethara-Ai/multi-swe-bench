import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Repository root inside every image. Kept in one place because the Dockerfile
# WORKDIR, prepare.sh, and all three run scripts must agree: `git apply` only
# works from the repo root, and a mismatch would surface as an unpatched stage
# rather than a hard error.
REPO_DIR = "/home/result"

# Pinned because requirements-dev.txt is entirely unpinned (`pytest`,
# `pytest-asyncio`, `mypy`, ...), so installing it verbatim resolves today's
# releases against Dec-2023 code -- pytest-asyncio reworked its event-loop
# fixture across 0.23.x and 1.x after this commit. Both pins are load-bearing,
# confirmed by removing each inside the built image:
#   * no pytest         -> "No module named pytest": zero output, every stage
#     parses to 0/0/0 and Report.check() rule 1 rejects the report.
#   * no pytest-asyncio -> the async tests are SKIPPED, not run
#     (PytestUnhandledCoroutineWarning; observed "1 passed, 1 skipped"). SKIP is
#     not PASS, so the six new tests are never classified, `fix_something` stays
#     False, and rule 3 rejects the report.
# 0.21.1 honours the bare `@pytest.mark.asyncio` used throughout
# tests/test_result_do.py under its default strict mode. flake8/mypy/twine/build
# are lint and packaging tools that no stage invokes.
TEST_DEPS = '"pytest==7.4.3" "pytest-asyncio==0.21.1"'

# ---------------------------------------------------------------------------
# Test command
# ---------------------------------------------------------------------------
# Identical string in run.sh / test-run.sh / fix-run.sh -- three different
# commands would mean three different test populations and a meaningless
# cross-stage comparison.
#
# `-o addopts=`  neutralises the repo's own pyproject addopts, which are
#     ["--tb=short", "--cov=result", "--cov=tests", "--cov-report=term",
#      "--cov-report=xml", "--ignore=tests/test_pattern_matching.py"].
#     Two reasons: the --cov flags make the run depend on pytest-cov being
#     present (a missing plugin turns every stage into a zero-test run, which
#     Report.check() rejects with an unhelpful message), and --cov-report=xml
#     writes coverage.xml into the working tree on every stage. The one addopt
#     worth keeping (--ignore=tests/test_pattern_matching.py) is restated below.
#
# `--continue-on-collection-errors` is load-bearing, not cosmetic. In the
#     test-patch stage tests/test_result_do.py cannot be imported at all --
#     the patched module does `from result import ..., do_async` and `do_async`
#     only exists after fix.patch. Without this flag pytest aborts the session
#     ("Interrupted: 1 error during collection") and the 34 tests in
#     tests/test_result.py are never run, so they read NONE in the test stage
#     and PASS in the fix stage: 34 phantom transitions instead of 34 honest
#     p2p tests.
#
# `--ignore=tests/test_pattern_matching.py` mirrors the repo default (the file
#     is Python 3.10+ `match` syntax; it is a SyntaxError, not a skip, on older
#     interpreters and the project excludes it by default on every version).
#
# `--ignore=tests/type_checking/test_result.yml` mirrors .github/workflows/ci.yml,
#     which runs that file as a separate step. It is a pytest-mypy-plugins suite
#     that asserts on exact mypy diagnostic text, so its result is a function of
#     the mypy release rather than of the patch under test -- pure noise for
#     f2p/p2p classification.
#
# `-p no:cacheprovider` keeps pytest from creating .pytest_cache/ in the tree.
#     The repo's .gitignore predates that directory name (it lists the pytest 2.x
#     `.cache/`), so without this the checkout is left dirty.
TEST_CMD = (
    "python -m pytest -v -rA --color=no -p no:cacheprovider -o addopts= "
    "--continue-on-collection-errors "
    "--ignore=tests/test_pattern_matching.py "
    "--ignore=tests/type_checking/test_result.yml "
    "tests/"
)

# Shared preamble for the three run scripts. `pipefail` matters because the test
# command is the final stage of each script: without it, a failure to *start*
# pytest would be masked. CI=true matches the repo's own GitHub Actions runs.
SCRIPT_HEADER = """#!/bin/bash
set -eo pipefail
export CI=true
"""


class ResultImageBase(Image):
    """Per-PR base image: OS deps + source at ${BASE_COMMIT} + the installed
    test environment. The PR image layers only the patches and run scripts.

    The tag carries the PR number because the image bakes in a PR-specific
    ${BASE_COMMIT} (build_dataset.py passes BASE_COMMIT as a build arg only for
    images whose dependency() is a string). A shared `base` tag would make two
    PRs with different base commits collide on one image_full_name(), and image
    dedup (Image.__hash__/__eq__) would silently build only the first.
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
        # setup.cfg at the base commit: python_requires = >=3.8, and the
        # classifiers/CI matrix top out at 3.12. 3.11 sits inside that window
        # and is the newest interpreter for which the pinned pytest-asyncio
        # 0.21.1 (see below) is a well-trodden combination.
        return "python:3.11-slim-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_env(self) -> dict[str, str]:
        # Returned through the enhancer's extra_env() hook rather than emitted as
        # an ENV line in dockerfile(), so the rendered Dockerfile carries exactly
        # ONE ENV instruction (these fold into the standard proxy/TLS block).
        #
        # PIP_PROGRESS_BAR / PIP_NO_COLOR: pip draws its progress bar with
        # Unicode box characters. The harness decodes buildx output with the
        # platform default codec, which is cp1252 on Windows, where those bytes
        # are undefined -- the build then dies on a UnicodeDecodeError unrelated
        # to the repo. ASCII-only pip output keeps it portable.
        #
        # PYTHONUNBUFFERED: keeps pytest output streaming into the harness log
        # capture instead of arriving in one block at process exit.
        return {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_PROGRESS_BAR": "off",
            "PIP_NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
        }

    def dockerfile(self) -> str:
        # No comments are emitted into the rendered Dockerfile -- the rationale
        # for each instruction lives here, in the generator, where it is reviewed
        # alongside the code instead of shipping inside the build artifact.
        #
        # This image deliberately carries NO Python packages: the interpreter,
        # the OS toolchain and the source tree only. Everything pip-installable
        # is provisioned by prepare.sh in the PR layer (see ImageDefault.files).
        # The trade that buys: a dependency change no longer rebuilds the base.
        # The trade that costs: prepare.sh installs run under `|| true`, so the
        # verification block at the end of prepare.sh is the ONLY thing standing
        # between a failed install and an image that silently reports zero tests.
        #
        # Cloning via "${REPO_URL}" (not a literal URL) is deliberate:
        # DockerfileEnhancer._standardize_repo_fetch rewrites a *hardcoded*
        # `git clone <url> /home/result` into clone + checkout + history-scrub +
        # CMD. The parameterised form leaves this block untouched and lets
        # _inject_final_sanitize append the scrub just before the trailing CMD.
        #
        # apt: git + ca-certificates are the universal minimum (clone over TLS
        # through the proxy). build-essential is carried because this is a
        # `-slim` base, which ships no C compiler: every dependency here resolves
        # to a `py3-none-any` wheel today, but a single sdist-only transitive dep
        # would otherwise fail the build with a "gcc not found" error, and arm64
        # could not be exercised locally to prove otherwise.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

CMD ["/bin/bash"]
"""


class ResultImageDefault(Image):
    """PR image: FROM the repo base, add only the patches and the run scripts."""

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
        return ResultImageBase(self.pr, self._config)

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
                """#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
# Runs once, during the PR image build. No tests execute here -- the graded runs
# happen at instance time via run.sh / test-run.sh / fix-run.sh.
#
# The base image has already been history-scrubbed by the pipeline enhancer, so
# the checkout below resolves against the detached HEAD object rather than a
# branch. The check_git_changes.sh guards bracket the reset/checkout because
# neither `git reset --hard` nor `git checkout` fails on a dirty tree and
# `reset --hard` leaves untracked files in place -- without them a polluted
# baseline would reach the graded stages unnoticed.
cd {REPO_DIR}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

# The base image ships no Python packages, so these two installs ARE the test
# environment. `|| true` follows the harness convention (a native-module compile
# failure on one arch must not fail an otherwise good image), which means a real
# failure here is silent -- the verification block below is what catches it.
#
# The editable install is required by the repo's src/ layout
# (`package_dir = =src` in setup.cfg): it writes a .pth containing
# /home/result/src, without which every test module dies with
# "ModuleNotFoundError: No module named 'result'" and pytest collects 0 tests.
pip install --no-cache-dir {TEST_DEPS} || true
pip install --no-cache-dir --no-build-isolation -e . || true

# THIS IS THE SAFETY NET FOR THE `|| true` ABOVE -- do not remove it. Both
# commands run under `set -e`, so either one failing aborts the image build.
# Without them a half-installed environment ships silently, and downstream a
# zero-test stage is indistinguishable from "these tests do not exist".
python -c "import result, pytest, pytest_asyncio"
python -m pytest --collect-only -q -p no:cacheprovider -o addopts= --ignore=tests/test_pattern_matching.py --ignore=tests/type_checking/test_result.yml tests/ > /dev/null
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SCRIPT_HEADER}cd {REPO_DIR}
{TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                # test.patch only. Neither patch in this instance carries a
                # `GIT binary patch` hunk, so no --exclude is needed.
                f"""{SCRIPT_HEADER}cd {REPO_DIR}
if ! git -C {REPO_DIR} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                # test.patch FIRST, then fix.patch -- the reverse order leaves
                # the graded tests absent and the stage meaningless.
                f"""{SCRIPT_HEADER}cd {REPO_DIR}
if ! git -C {REPO_DIR} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("rustedpy", "result")
class RUSTEDPY_RESULT(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ResultImageDefault(self.pr, self._config)

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

        # Strip the full CSI class rather than only SGR colour codes, so a
        # stray cursor/erase sequence cannot leave an unmatched prefix on an
        # otherwise well-formed result line.
        ansi_escape = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        log_no_ansi = ansi_escape.sub("", log)

        # `pytest -v -rA` emits each result twice, in two shapes:
        #   verbose progress -> "tests/test_result.py::test_eq PASSED   [  8%]"
        #   short summary    -> "PASSED tests/test_result.py::test_eq"
        #                       "FAILED tests/test_result.py::test_eq - AssertionError"
        # Both are parsed and reconciled below; every captured name is a full
        # pytest node id (path::function), which is inherently unique, carries no
        # timing or count metadata, and is therefore stable across the three
        # stages.
        #
        # Deliberately NOT captured: the collection-error line that this
        # instance's test stage produces, "ERROR tests/test_result_do.py". That
        # names a FILE that failed to import, not a test. Recording it would put
        # a non-node-id into the report, and it would read FAIL in the test stage
        # and NONE in the fix stage -- a fabricated entry describing nothing.
        # The node pattern requires "::", which excludes it. The same guard
        # excludes pytest-asyncio's teardown noise
        # ("ERROR    asyncio:base_events.py:1785 Task was destroyed ..."), which
        # has single colons only.
        #
        # The bracket body is permissive (`[^\\]]*`) so a parametrize id
        # containing spaces survives intact; path and function segments stay
        # space-free.
        node = r"[^\s\[]+::[^\s\[]+(?:\[[^\]]*\])?"
        statuses = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
        progress_re = re.compile(rf"^(?P<node>{node})\s+(?P<status>{statuses})")
        summary_re = re.compile(
            rf"^(?P<status>{statuses})\s+(?P<node>{node})(?:\s+-\s+.*)?$"
        )

        buckets = {
            "PASSED": passed_tests,
            "XPASS": passed_tests,
            "FAILED": failed_tests,
            "ERROR": failed_tests,
            "SKIPPED": skipped_tests,
            "XFAIL": skipped_tests,
        }

        for line in log_no_ansi.splitlines():
            line = line.strip()
            if not line:
                continue
            match = progress_re.match(line) or summary_re.match(line)
            if not match:
                continue
            buckets[match.group("status")].add(match.group("node"))

        # A test can legitimately land in two buckets (verbose PASSED, then a
        # summary ERROR raised in teardown). TestResult.__post_init__ raises on
        # any intersection, so collapse to one bucket with the worst status
        # winning.
        skipped_tests -= failed_tests
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
