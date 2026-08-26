import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PyRepoStatsImageBase(Image):
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
        # `.github/workflows/ci_testing.yml` matrixes 3.10 and 3.12, and pins
        # 3.10 for the sampler job; 3.10 is the version both jobs share.
        # Pinned, not floating, so the image stays reproducible.
        return "python:3.10-slim"

    def image_tag(self) -> str:
        # Per-PR, not a shared `base`: this image chains to a STRING, so
        # DockerfileEnhancer rewrites the clone below into
        # `clone + git checkout ${BASE_COMMIT} + hardening` (R10). A tag shared
        # across PRs would be pinned to whichever PR built it first and would
        # have every other PR's commit pruned out of the object store.
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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class PyRepoStatsImageDefault(Image):
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
        return PyRepoStatsImageBase(self.pr, self.config)

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

""".format(),
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

# Installed at build time, where the network is available: the three stages
# below run offline, so nothing may be resolved from PyPI at stage time.
pip install --no-cache-dir --upgrade pip setuptools wheel || true
pip install --no-cache-dir -e . -r tests/requirements.txt || true

# `jsonargparse` is added to `requirements.txt` by the fix patch, which rewrites
# the CLI onto it. `pip install` never re-runs between stages, so installing it
# here is what lets the gold tests reach their assertions after the fix instead
# of dying in collection on ModuleNotFoundError -- which would grade the fix
# stage at 0 tests and read as a parse_log bug.
pip install --no-cache-dir jsonargparse || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

# `.github/workflows/ci_testing.yml` exports SHOW_FIGURE=0 so matplotlib never
# opens a window on a headless runner. `src/repo_stats/__main__.py` reads it as
# `bool(int(os.getenv("SHOW_FIGURE", default=1)))`, i.e. it defaults to ON. The
# gold tests currently neutralise it by patching the resolved constant, so the
# variable is bypassed on the test path today; it is exported here so a test
# that reaches the un-mocked path cannot block on a GUI backend.
export SHOW_FIGURE=0

cd /home/{pr.repo}

# `-o addopts=` drops `--doctest-modules` and `--color=yes` from the `addopts`
# in `pyproject.toml`. Doctests are dropped because the fix patch rewrites
# `src/repo_stats/cli.py` and `__main__.py`; any doctest collected from those
# files would be a fix-authored test that only passes after the fix, and
# report.py's cheating guard would reject the whole instance for crediting it.
# Colour is dropped so no ANSI escape ever lands inside a nodeid.
#
# `src/ tests/` mirrors the CI command. `--durations` is omitted deliberately:
# timings would differ per stage, and only the `PASSED`/`FAILED` lines that
# `-rA` prints are parsed.
python -m pytest src/ tests/ -v -rA --no-header --tb=short --color=no -p no:cacheprovider -o addopts=

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
git apply --whitespace=nowarn /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
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


@Instance.register("Borda", "pyRepoStats")
class PyRepoStats(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PyRepoStatsImageDefault(self.pr, self._config)

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

        # `-rA` prints a `short test summary info` section whose lines are
        # `<STATUS> <nodeid>`. Those lines are parsed rather than the per-test
        # progress lines because this suite is parametrized on CLI strings that
        # contain spaces and quotes -- `test_offline_github[--min_contribution 2
        # --users_summary+ "all"]` -- so a nodeid cannot be delimited by
        # whitespace. Anchoring on the leading status keeps the whole remainder
        # of the line as the name, and the `::` requirement keeps summary
        # counters such as `5 passed, 1 skipped` from ever becoming a name.
        #
        # SKIPPED is excluded on purpose: pytest renders it as
        # `SKIPPED [1] tests/test_cli.py:41: <reason>`, a file:line location and
        # not a nodeid, and its line number shifts between stages as the test
        # patch adds lines. Recording it would give the same test a different
        # name in each stage and break the FAIL->PASS comparison.
        re_pass = [re.compile(r"^(?:PASSED|XPASS)\s+(\S.*::.*)$")]
        re_fail = [re.compile(r"^(?:FAILED|ERROR)\s+(\S.*::.*)$")]
        re_skip = [re.compile(r"^(?:XFAIL)\s+(\S.*::.*)$")]

        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", test_log)

        for line in clean_log.splitlines():
            line = line.strip()
            for r in re_pass:
                m = r.match(line)
                if m:
                    # `FAILED <nodeid> - <exception>` has no counterpart here,
                    # but a PASSED line may still carry a trailing reason for
                    # xpass; keep only the nodeid.
                    passed_tests.add(m.group(1).strip())
            for r in re_fail:
                m = r.match(line)
                if m:
                    failed_tests.add(m.group(1).split(" - ")[0].strip())
            for r in re_skip:
                m = r.match(line)
                if m:
                    skipped_tests.add(m.group(1).split(" - ")[0].strip())

        # R2 -- the three sets MUST be disjoint or TestResult raises. Failure
        # wins, so a nodeid reported both ok and FAILED counts as failed.
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
