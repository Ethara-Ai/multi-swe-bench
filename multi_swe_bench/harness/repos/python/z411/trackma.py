import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TrackmaImageBase(Image):
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
        # Trackma is a pure-Python application: setup.py declares no mandatory
        # install_requires (the extras are UI toolkits the tests never import),
        # and the suite added by PR #547 only exercises
        # trackma.extras.AnimeInfoExtractor, which is stdlib-only. A plain
        # CPython image plus pytest is therefore enough. 3.9 matches the
        # interpreter the project targeted around this PR, and the non-slim
        # image already ships git for the clone below.
        return "python:3.9"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \\
    && python -m pip install --no-cache-dir "pytest==7.4.4"

{code}

{self.clear_env}

"""


class TrackmaImageDefault(Image):
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
        return TrackmaImageBase(self.pr, self._config)

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
# The shared base image is pruned to a single commit's history, so a PR whose
# base commit differs from the one that seeded the base image would be missing
# it ("fatal: reference is not a tree"). Re-fetch that exact commit when it is
# absent so the checkout works regardless of which PR built the base image.
if ! git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null; then
    git fetch --no-tags --depth 1 https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
fi
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

python --version
python -m pytest --version

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

# Exported here rather than as a Dockerfile ENV so the image layout is
# untouched; pytest itself ignores CI, but keeping the three run scripts
# identical to the harness convention avoids surprises if the suite ever
# grows a plugin that inspects it.
export CI=true

cd /home/{pr.repo}
# Run through `python -m pytest` rather than the bare `pytest` entry point so
# the repository root lands on sys.path: trackma is never installed into
# site-packages, so the tests must import the checked-out package directly.
# The base commit of an early PR may predate the tests/ directory entirely,
# in which case there is simply nothing to run.
if [ -d tests ]; then
    python -m pytest -v -rA tests/
else
    echo "No tests directory at this commit; nothing to run."
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

# Exported here rather than as a Dockerfile ENV so the image layout is
# untouched; pytest itself ignores CI, but keeping the three run scripts
# identical to the harness convention avoids surprises if the suite ever
# grows a plugin that inspects it.
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
python -m pytest -v -rA tests/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

# Exported here rather than as a Dockerfile ENV so the image layout is
# untouched; pytest itself ignores CI, but keeping the three run scripts
# identical to the harness convention avoids surprises if the suite ever
# grows a plugin that inspects it.
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest -v -rA tests/

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


@Instance.register("z411", "trackma")
class Trackma(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TrackmaImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text: str) -> str:
            return re.compile(r"\x1B\[[0-?9;]*[mK]").sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # `pytest -v -rA` reports every outcome twice: once on the verbose
        # progress line ("tests/x.py::test_y PASSED  [ 5%]") and once in the
        # short summary ("PASSED tests/x.py::test_y"). Both carry the node id,
        # so either form yields the same identifier and the sets deduplicate.
        # A summary line such as "SKIPPED [1] tests/x.py:12: reason" carries no
        # node id, hence the explicit "::" requirement below -- matching it
        # would otherwise record a test literally named "[1]".
        verbose_re = re.compile(
            r"^(\S+::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        summary_re = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)"
        )

        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            match = verbose_re.match(line)
            if match:
                name, status = match.group(1), match.group(2)
            else:
                match = summary_re.match(line)
                if not match:
                    continue
                status, name = match.group(1), match.group(2)

            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # An id can surface under more than one status across the progress line
        # and the summary (a test that errors during teardown after passing, for
        # instance); resolve precedence deterministically so the buckets never
        # overlap.
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
