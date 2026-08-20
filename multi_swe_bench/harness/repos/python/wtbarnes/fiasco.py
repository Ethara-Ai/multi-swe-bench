from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# CHIANTI atomic database seeded into the base image.
#
# fiasco's test session builds an HDF5 database from a raw ASCII CHIANTI tree
# (fiasco/conftest.py::hdf5_dbase_root -> fiasco.util.check_database). Left to
# itself that fixture downloads a ~270MB tarball on every run, so the tree is
# fetched once at build time and handed to pytest via --ascii-dbase-root.
#
# Version 8.0.7 is deliberate, not an oversight: at the base commit
# SUPPORTED_VERSIONS == ['8.0', '8.0.2', '8.0.6', '8.0.7'] and only
# file_hashes_v8.0.7.json ships in fiasco/util/data, so any other version makes
# check_database_version raise UnsupportedVersionError before a single test runs.
# The v9-only cases in test_gaunt.py are marked `requires_dbase_version('>= 9.0.1')`
# and skip cleanly here; the discriminating case for this PR
# (test_free_free_integrated_itoh_missing_data) is marked `< 9.0.1` and does run.
CHIANTI_VERSION = "8.0.7"
CHIANTI_URL = (
    f"http://download.chiantidatabase.org/CHIANTI_v{CHIANTI_VERSION}_database.tar.gz"
)
ASCII_DBASE_ROOT = "/home/chianti_dbase"

# Explicit `fiasco` path instead of the pyproject testpaths (which also lists
# `docs`) so a run does not need the docs extras (sphinx, aiapy, hissw) to collect.
TEST_CMD = (
    "pytest -vvv -rA --color=no --continue-on-collection-errors "
    f"--ascii-dbase-root={ASCII_DBASE_ROOT} fiasco"
)

# Shared preamble for the three run scripts. `pipefail` matters because the test
# command is the last stage of the script -- without it a failure to *start* pytest
# would be masked. CI=true is set for parity with the repo's own GitHub Actions runs.
SCRIPT_HEADER = """#!/bin/bash
set -eo pipefail
export CI=true
"""


class ImageBase(Image):
    """Per-PR base image: OS deps + source at ${BASE_COMMIT} + installed env
    + the CHIANTI ASCII database. Tagged `<repo>:base-pr-<N>`; the PR image layers
    only the patches and run scripts on top.

    The tag carries the PR number because the image bakes in a PR-specific
    ${BASE_COMMIT}. A shared `<repo>:base` tag would let two PRs with different
    base commits collide on one name, and the last build would silently win."""

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
        # pyproject: requires-python = ">=3.10"; 3.11 has wheels for every
        # dependency in this era (numpy, h5py, matplotlib, plasmapy).
        return "python:3.11-slim-bookworm"

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

        # Clone via "${{REPO_URL}}": the pipeline enhancer leaves that form untouched
        # and injects its git-hardening block just before the trailing CMD, i.e. after
        # the install below. That ordering matters -- setuptools_scm derives the version
        # from git tags at install time and the hardening pass deletes every ref.
        return f"""FROM {image_name}
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONUNBUFFERED=1
ENV MPLBACKEND=Agg
ENV PIP_PROGRESS_BAR=off
ENV PIP_NO_COLOR=1
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git cat-file -e ${{BASE_COMMIT}} 2>/dev/null || git fetch --no-tags "${{REPO_URL}}" ${{BASE_COMMIT}}
RUN git checkout ${{BASE_COMMIT}}

RUN printf '%s\\n' \\
    'numpy<2.1' \\
    'astropy<7' \\
    'matplotlib<3.10' \\
    'plasmapy<2024.10' \\
    'pytest<8.4' \\
    'pyparsing<3.2' \\
    'asdf<4' \\
    'asdf-astropy<0.7' \\
    > /home/constraints.txt
RUN PIP_CONSTRAINT=/home/constraints.txt pip install --no-cache-dir -e ".[test]"

RUN python -c "from fiasco.util import download_dbase; download_dbase('{CHIANTI_URL}', '{ASCII_DBASE_ROOT}')" \\
    && test -f {ASCII_DBASE_ROOT}/VERSION \\
    && cat {ASCII_DBASE_ROOT}/VERSION
CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """PR-specific image: FROM the repo base, add only patches + run scripts."""

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
# Re-assert a clean tree at the base commit at PR-build time. No test run here --
# tests execute at instance time via run/test/fix-run.sh. The base image has already
# been ref-stripped, so the checkout resolves against the detached HEAD object.
# Dependencies are installed in ImageBase, not here, so a failure below is a real
# problem and is allowed to abort the build rather than being swallowed by `|| true`.
# The check_git_changes.sh guards assert the tree is genuinely clean after the reset
# and again after the checkout -- git reset --hard leaves untracked files behind and
# neither command fails on a dirty tree, so without them a polluted baseline would
# reach the graded runs undetected.
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """{header}cd /home/{pr.repo}
{test_cmd}

""".format(header=SCRIPT_HEADER, pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """{header}cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}

""".format(header=SCRIPT_HEADER, pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """{header}cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}

""".format(header=SCRIPT_HEADER, pr=self.pr, test_cmd=TEST_CMD),
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


@Instance.register("wtbarnes", "fiasco")
class FIASCO(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped
        import re

        # Strip the full CSI class, not just SGR colour codes: astropy's ProgressBar
        # runs during the HDF5 database build and emits cursor/erase sequences.
        ansi_escape = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        log_no_ansi = ansi_escape.sub("", log)

        # Two shapes are emitted by `pytest -vvv -rA`:
        #   verbose progress -> "fiasco/tests/test_gaunt.py::test_repr PASSED [  1%]"
        #   short summary    -> "FAILED fiasco/tests/test_gaunt.py::test_repr - AssertionError"
        # SKIPPED lines in the short summary carry a "file:line: reason" instead of a
        # node id, so skips are picked up from the progress form only.
        #
        # The node pattern must tolerate SPACES inside the parametrize bracket: fiasco
        # parametrizes on ion names, so real node ids include
        # `test_create_ion_input_formats[fe 21]` and `test_parse_ion_name[26 21]`.
        # A `\S+`-based id would stop at the space and drop those cases from every
        # bucket silently. Path and function segments stay space-free; only the
        # bracket body is permissive, and it stops at the closing bracket.
        # No `\b` after the status: when a skip reason spans multiple lines, pytest
        # writes the continuation straight onto the progress line, e.g.
        #   ...::test_idl_compare_free_bound_ion SKIPPEDing IDL executable,
        # (the reason in fiasco/tests/idl/helpers.py is a multi-line string). A word
        # boundary would reject `SKIPPEDing` and drop that test from every bucket.
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

        # Deconflict: a test reported twice (e.g. progress PASSED, then a summary ERROR
        # raised in teardown) must land in exactly one bucket, worst status winning.
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
