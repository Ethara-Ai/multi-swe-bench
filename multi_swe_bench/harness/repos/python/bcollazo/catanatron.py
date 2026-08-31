from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Same command in all three run scripts (run.sh / test-run.sh / fix-run.sh) so
# the f2p comparison is meaningful.
#
#   --override-ini="addopts="  drops pytest.ini's benchmark-column / warning
#                              flags so the node-id lines stay parseable.
#   --benchmark-disable        pytest-benchmark still runs each benchmarked
#                              test exactly once (pass/fail is still reported)
#                              but skips the timing rounds and the summary
#                              table, keeping the run deterministic and fast.
TEST_CMD = (
    "python -m pytest tests/ -v -rA --tb=short -p no:cacheprovider "
    '--override-ini="addopts=" --benchmark-disable'
)

# The four packages of the monorepo, each in a same-named top-level directory.
PACKAGE_DIRS = [
    "catanatron_core",
    "catanatron_gym",
    "catanatron_server",
    "catanatron_experimental",
]

# `all-requirements.txt` already lists all four as `-e`, but they are installed
# explicitly as well so the environment is correct even if one of its pinned
# transitive dependencies fails to resolve.
EDITABLES = " ".join(f"-e {p}" for p in PACKAGE_DIRS)

# pip installs the four packages as PEP 660 editables, which register a finder
# appended to `sys.meta_path` — i.e. *after* the stock PathFinder. pytest runs
# from the repo root, where `catanatron_server/`, `catanatron_gym/` and
# `catanatron_experimental/` are plain directories with no `__init__.py`, so
# PathFinder resolves those imports to empty namespace packages before the
# editable finder is ever consulted, and `tests/test_accumulators.py` and
# `tests/integration_tests/test_server.py` fail to collect. Putting the real
# package parents on PYTHONPATH gives PathFinder a regular package to find,
# which always beats a namespace portion. The paths point at the working tree,
# so fix.patch edits to the sources still take effect.
PYTHONPATH_EXPORT = 'export PYTHONPATH="{}"'.format(
    ":".join("/home/{pr.repo}/" + p for p in PACKAGE_DIRS)
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
        # .github/workflows/build.yml pins Python 3.11 for build-python and
        # build-gym; the full bookworm image ships the toolchain needed when a
        # pinned wheel (psycopg2-binary, numpy) has no prebuilt arm64 build.
        return "python:3.11-bookworm"

    def image_tag(self) -> str:
        # The tag must carry the PR number: this image is *not* reusable across
        # PRs. The generated Dockerfile checks out ${BASE_COMMIT} and the
        # hardening block then deletes every git object unreachable from it, so
        # a second PR sharing a `base` tag would find its own base commit
        # missing from the object store.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        # Rendered by the default `Image.dockerfile()` *between* the
        # `git checkout ${BASE_COMMIT}` and the history-scrub block, so the
        # environment is warmed on the pinned tree and `CMD ["/bin/bash"]`
        # remains the final instruction. The default package list already
        # supplies git, ca-certificates and build-essential, which is the whole
        # apt surface this repo needs on top of the python base image.
        return f"""RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r all-requirements.txt || true
RUN pip install --no-cache-dir {EDITABLES}
RUN pip install --no-cache-dir "pytest==7.2.2" "pytest-benchmark==4.0.0\""""


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

    def dependency(self) -> Image | None:
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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

pip install --no-cache-dir --upgrade pip setuptools wheel || true
pip install --no-cache-dir -r all-requirements.txt || true
pip install --no-cache-dir {editables} || true
pip install --no-cache-dir "pytest==7.2.2" "pytest-benchmark==4.0.0" || true

""".format(pr=self.pr, editables=EDITABLES),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
{pythonpath}
cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD, pythonpath=PYTHONPATH_EXPORT.format(pr=self.pr)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
{pythonpath}
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD, pythonpath=PYTHONPATH_EXPORT.format(pr=self.pr)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
{pythonpath}
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD, pythonpath=PYTHONPATH_EXPORT.format(pr=self.pr)),
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

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


def parse_log_pytest(log: str) -> TestResult:
    """Parse `pytest -v -rA` output for bcollazo/catanatron.

    Two line shapes carry a status, and both are keyed on the full node id
    (``tests/<path>.py::<test>``) so a test is named identically in the run,
    test-patch and fix-patch stages:

        tests/test_json.py::test_action_from_json_build_road PASSED   [ 50%]
        PASSED tests/test_json.py::test_action_from_json_build_road

    Collection errors reported by ``-rA`` (``ERROR tests/test_gym.py``) carry
    no ``::`` and are recorded as failures under the file path.

    Neither timings, counts nor the progress percentage are captured, so a test
    name cannot drift between stages.
    """
    # Strip ANSI colour codes first — pytest colourises status words whenever it
    # believes it is attached to a tty.
    log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # `tests/foo.py::bar PASSED [ 12%]` — verbose per-test line.
    status_after = re.compile(
        r"^(tests/[^\s:]+(?:::[^\s]+)?)\s+"
        r"(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b"
    )
    # `PASSED tests/foo.py::bar` — `-rA` short summary line.
    status_before = re.compile(
        r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)(?:\s+\[\s*\d+\s*\])?\s+"
        r"(tests/[^\s:]+(?:::[^\s]+)?)"
    )

    for raw_line in log.splitlines():
        line = raw_line.strip()

        match = status_after.match(line)
        if match:
            name, status = match.group(1), match.group(2)
        else:
            match = status_before.match(line)
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            # `-rA` renders skips as `SKIPPED [1] tests/foo.py:35: reason`,
            # i.e. file:line rather than a node id. The verbose line above has
            # already recorded that test under its real name, so drop anything
            # that is not a node id. Collection errors are the one legitimate
            # file-level record — they never produce a verbose line.
            if "::" not in name and status != "ERROR":
                continue

        name = name.rstrip(":")

        if status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        elif status == "SKIPPED":
            skipped_tests.add(name)
        else:  # PASSED, XFAIL, XPASS
            passed_tests.add(name)

    # TestResult.__post_init__ requires the three sets to be pairwise disjoint.
    # A test can legitimately show up with more than one status (a rerun, or a
    # passing test whose teardown errors) — failure wins, then skip.
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


@Instance.register("bcollazo", "catanatron")
class CATANATRON(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        return parse_log_pytest(log)
