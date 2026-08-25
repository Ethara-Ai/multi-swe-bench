import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FuzzManagerImageBase(Image):
    """Toolchain + checked-out source + installed dependencies.

    Everything expensive lives here so the per-PR patch layer stays cheap to
    rebuild: the pip install pulls ~70 packages and compiles pycryptodome from
    source (no wheel published for 3.7.3), which is minutes of work we do not
    want to repeat every time a run script changes.
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
        # setup.cfg pins `pytest-django; python_version >= '3.8' and <= '3.10'`,
        # and .taskcluster.yml runs the Django suite only on py38/py39/py310
        # (py37 and py311 are labelled "no django"). 3.10 is the newest version
        # that still gets pytest-django, so the server tests actually collect.
        return "python:3.10-bookworm"

    def image_tag(self) -> str:
        # Per-PR: DockerfileEnhancer injects a hardening block that detaches at
        # one ${BASE_COMMIT} and prunes every other ref, so a shared tag would
        # let whichever PR built first pin the commit for all the others.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}`, before the hardening block,
        # with WORKDIR already at /home/FuzzManager -- so setuptools_scm can
        # still read git metadata to derive the version.
        #
        # -c requirements.txt is how tox installs (tox.ini install_command), and
        # it is load-bearing: unconstrained, pip resolves celery/django to
        # versions the pinned set does not agree with.
        #
        # [server,test] only. The taskmanager extra pulls fuzzing-decision from
        # a git URL and MozillaPulse; omitting it costs 7 deterministic failures
        # in server/taskmanager/tests/test_update_pools.py, which are identical
        # across run/test/fix and therefore cancel out of the p2p/f2p diff.
        return 'RUN pip install --no-cache-dir -c requirements.txt -e ".[server,test]"'


class FuzzManagerImageDefault(Image):
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
        return FuzzManagerImageBase(self.pr, self._config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the import cache and prove the interpreter can collect the suite. The
# dependency install already happened in the base image, so there is nothing to
# download here. `|| true` because a collection hiccup must not fail the build --
# the graded runs are the thing that decides pass/fail, not this.
pytest --collect-only -q -p no:cacheprovider > /dev/null 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest --no-header -rA --tb=no -p no:cacheprovider

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest --no-header -rA --tb=no -p no:cacheprovider

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest --no-header -rA --tb=no -p no:cacheprovider

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("MozillaSecurity", "FuzzManager")
class FUZZMANAGER(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FuzzManagerImageDefault(self.pr, self._config)

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
        # Matches the `-rA` short summary block, verified against real output
        # captured from this repo at 40e895f1e:
        #   PASSED server/crashmanager/tests/test_crashes_rest.py::test_rest_crash_update
        #   FAILED server/taskmanager/tests/test_update_pools.py::test_update_task_1 - Mo...
        #   SKIPPED [1] server/crashmanager/tests/test_rest_live.py:23: unconditional skip
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for match in re.finditer(r"^PASSED (\S+)", log, re.MULTILINE):
            passed_tests.add(match.group(1))
        # ERROR covers collection failures, which -rA reports in the same block.
        for match in re.finditer(r"^(?:FAILED|ERROR) (\S+)", log, re.MULTILINE):
            failed_tests.add(match.group(1))
        # SKIPPED carries a file:line, not a node id -- pytest does not report
        # the test name for skips in the summary block.
        for match in re.finditer(r"^SKIPPED \[\d+\] (\S+?):\d+:", log, re.MULTILINE):
            skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )