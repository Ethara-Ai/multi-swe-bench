import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class KeyringImageBase(Image):
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
        # `setup.cfg` declares `python_requires = >=3.7`; CI matrixes 3.7/3.10/3.11.
        return "python:3.10"

    def image_tag(self) -> str:
        # Per-PR, not a shared `base`: DockerfileEnhancer injects
        # `git checkout ${BASE_COMMIT}` plus the history scrub into this image, so a
        # tag shared across PRs would be pinned to whichever PR built it first and
        # would have every other PR's commit pruned out of the object store.
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

# The gold test lives in the `BackendBasicTests` mixin, so it only executes for
# a backend that is actually viable. Without a Secret Service on the bus every
# subclass skips and the test never runs at all, leaving nothing to grade.
# gnome-keyring supplies that service; dbus-x11 supplies `dbus-run-session`.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    gnome-keyring \\
    dbus \\
    dbus-x11 \\
    libsecret-1-0 \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class KeyringImageDefault(Image):
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
        return KeyringImageBase(self.pr, self.config)

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

# Only the runtime dependencies are installed, not the `testing` extra: it
# pulls pytest-black/-flake8/-mypy unpinned, and a current pytest-black
# registers `pytest_collect_file` with an argument today's hookspec no longer
# has, so pytest aborts with PluginValidationError before collecting anything.
# 7.4.4 is the last release contemporary with this base commit.
pip install --no-cache-dir -e . || true
pip install --no-cache-dir "pytest==7.4.4" || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}

# `-o addopts=` drops `--doctest-modules` from `pytest.ini`. The fix patch adds
# a doctest to `keyring/backend.py`, which would otherwise appear as a test
# that only passes after the fix -- and because that file is in the fix patch,
# report.py's cheating guard would reject the whole instance for crediting a
# fix-authored test. Dropping doctests from all three stages keeps the gold
# test from `test.patch` as the only transition.
#
# stderr is discarded because dbus-daemon writes service-activation warnings
# while pytest is printing: the two interleave mid-line and split a nodeid from
# its status, which would record the same test under a different name in each
# stage. pytest reports on stdout, which is kept.
dbus-run-session -- bash -c 'echo -n "" | gnome-keyring-daemon --unlock --components=secrets > /dev/null 2>&1; exec python -m pytest -v -rA --no-header --tb=no --color=no -p no:cacheprovider -p no:randomly -o addopts=' 2> /dev/null

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


@Instance.register("jaraco", "keyring")
class Keyring(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KeyringImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # `-v` progress line: `tests/test_core.py::test_init PASSED [ 1%]`.
        verbose_pattern = re.compile(
            r"^(?P<name>\S+::.*?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        # `-rA` summary line: `FAILED tests/x.py::test_x - AssertionError`.
        # The name is required to carry `::` so that a wrapped status line, or
        # any stray output beginning with a status word, cannot be mistaken for
        # a nodeid. The `SKIPPED [1] keyring/testing/backend.py:68: reason`
        # form is not parsed at all: its `file:line` is not a nodeid and the
        # line moves when a patch is applied, which would record the same skip
        # under a different name in each stage.
        summary_pattern = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS)\s+"
            r"(?P<name>\S+::\S*)(?:\s+-\s.*)?$"
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
                # PASSED, plus XFAIL/XPASS: `xfail_strict` is not set, so an
                # expected failure is not a failure.
                passed_tests.add(name)

        # A test reported more than one way (e.g. passed, then errored during
        # teardown) counts as failed. Required for TestResult's disjointness.
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
