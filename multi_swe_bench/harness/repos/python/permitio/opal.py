"""Repo config for permitio/opal (Python / pytest).

Runner
------
``.github/workflows/tests.yml`` installs the tree with ``pip install -r
requirements.txt`` and then runs a bare ``pytest`` from the repository root.
``requirements.txt`` is what makes that work: its first three lines are
editable installs of the three packages that make up the monorepo::

    -e ./packages/opal-common
    -e ./packages/opal-client
    -e ./packages/opal-server

so ``opal_common`` / ``opal_client`` / ``opal_server`` resolve as importable
top-level modules rather than as paths under ``packages/``. The gold test file
imports all three, so a plain ``pip install -e .`` at the root -- there is no
root-level package -- would leave it uncollectable.

``pytest.ini`` sets ``asyncio_mode = strict`` and 29 tests carry an explicit
``@pytest.mark.asyncio``, so the config file must stay in force: the run
scripts do not pass ``-c`` or ``-p no:cacheprovider`` in a way that would
bypass it.

Test identity
-------------
pytest node IDs already have the required ``<source file>::<test name>``
shape, so ``parse_log`` reports them verbatim::

    packages/opal-common/opal_common/tests/test_config.py::test_opal_common_config_descriptions

No marker scaffolding is needed here. This is the one runner family where the
identity falls out of the reporter for free -- ``-v`` prints the node ID and
the outcome on a single line, and the node ID is relative to the rootdir,
which is the repository root.

Toolchain pin
-------------
``python:3.10``:

* The CI matrix is ``["3.9", "3.10", "3.11", "3.12"]`` and the docker job pins
  ``3.10``, so it is the one version exercised by both halves of CI.
* The unsuffixed (non-``slim``) tag derives from ``buildpack-deps``, which
  already carries git. The ``git_utils`` tests shell out to git through
  GitPython, and the base image clones the repo, so git is required twice
  over -- taking it from the base image avoids an apt layer entirely.

Network-dependent tests
-----------------------
``repo_cloner_test.py`` clones ``https://github.com/permitio/fastapi_websocket_pubsub.git``
over https and ssh. Those cases cannot pass in a sandbox with no egress, but
they fail identically in all three stages, so they land in neither ``f2p`` nor
``p2p`` and cannot violate the no-PASS-to-FAIL rule. The rest of the
``git_utils`` suite builds its fixtures with a local ``Repo.init`` and is
unaffected.

Gold signal
-----------
The test patch creates ``packages/opal-common/opal_common/tests/test_config.py``
with three tests asserting every config entry carries a description; the fix
patch adds those descriptions across the three ``config.py`` files. The three
tests therefore do not exist in the run stage, fail in the test stage, and
pass in the fix stage -- a clean three-test ``f2p``.
"""

import re
import shlex
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)

# `.+?` rather than `\S+?` for the node ID: pytest parametrisation embeds the
# repr of the parameter, so `test_fetch[param with space]` is a legitimate node
# ID containing spaces. Anchoring on `^` plus the `.py::` infix keeps this from
# matching the trailing summary lines, which lead with the status instead.
_VERBOSE_RE = re.compile(
    r"^(?P<nodeid>\S+\.py::.+?)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)",
    re.MULTILINE,
)

_SUMMARY_RE = re.compile(
    r"^(?P<status>FAILED|ERROR)\s+(?P<nodeid>\S+\.py::.+?)(?:\s+-\s|\s*$)",
    re.MULTILINE,
)


def _gold_test_exclude_flags(test_patch: str) -> str:
    """``git apply --exclude`` flags for every file the gold test patch touches.

    Reward-hacking guard, defence in depth for
    ``test_result.fix_patch_tampers_with_tests``: that pre-run check reads
    ``get_modified_files``, which drops entries whose ``---`` side is
    ``/dev/null`` and is therefore blind to gold tests the test patch
    *creates*. This patch creates exactly such a file, so the regex half below
    is doing the real work here, not the ``get_modified_files`` half.
    """
    text = (test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    paths = {m.group(2) for m in _DIFF_GIT_RE.finditer(text)}
    paths |= set(get_modified_files(test_patch or ""))
    return " ".join(f"--exclude={shlex.quote(p)}" for p in sorted(paths))


_RUN_PYTEST = """pytest_status=0
timeout -k 60 1800 python -m pytest -v --color=no \\
    --continue-on-collection-errors 2>&1 || pytest_status=$?
printf '##### MSWEB-PYTEST-EXIT: %s\\n' "$pytest_status\""""


class OpalImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- clones the repo on top of Python 3.10.

    Tagged per PR rather than with a bare ``:base``: a single shared tag would
    be rewritten by every other instance of this repo, silently changing the
    foundation an already-verified instance was built against.

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance``
    rewrites the ``git clone`` line below into the standard
    clone + ``checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` sequence
    and supplies ``REPO_URL`` / ``BASE_COMMIT`` as build args.
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
        return "python:3.10"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class OpalImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT and installs the three editable packages."""

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
        return OpalImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        gold_excludes = _gold_test_exclude_flags(self.pr.test_patch)

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
# `-e` is load-bearing, not decoration. Without it the three
# `check_git_changes.sh` calls below are advisory: a dirty tree or a failed
# checkout prints its complaint, the script runs on, and the image builds
# green -- an assertion that cannot fail the build is not an assertion.
set -euxo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

python -m pip install --upgrade pip

# The three `-e ./packages/*` lines at the top of requirements.txt are the
# whole point of installing this way: they put opal_common / opal_client /
# opal_server on sys.path as top-level modules, which is what the gold test
# imports. pytest, pytest-asyncio and pytest-rerunfailures come from the same
# file, so the suite's own toolchain needs no separate install.
#
# Retried once, then tolerated: psutil and pydantic 1.x publish no linux
# aarch64 wheels, so an arm64 build compiles both from sdist and a transient
# network or build hiccup should not be fatal on the first try. `|| true` on
# the last attempt keeps this in line with the standard prepare.sh shape --
# but it is NOT where correctness is decided. The import gate below is.
pip install --no-cache-dir -r requirements.txt \\
    || pip install --no-cache-dir -r requirements.txt \\
    || true

# This is the real assertion, and it is deliberately not tolerant. A failed
# install leaves the three packages un-importable, which would yield zero
# collectable tests in all three stages -- Report.check() would reject on
# `all_count == 0` after three full stage runs. Failing here turns that into
# an immediate, legible build error.
python -c "import opal_common, opal_client, opal_server"
python -m pytest --version

# Editable installs drop *.egg-info/ and build/ into packages/, and
# __pycache__/ everywhere -- all three are covered by .gitignore, so the tree
# must still be pristine here. A dirty tree at this point means an install
# wrote into a tracked path, and every later `git apply` would then be laid on
# top of unexplained edits.
#
# This is deliberately the last command: no `exit 0` follows it, so the
# script's exit status *is* this check's status.
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

{run_pytest}
""".format(pr=self.pr, run_pytest=_RUN_PYTEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_pytest}
""".format(pr=self.pr, run_pytest=_RUN_PYTEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

# Canonical stage order: gold tests first, fix patch on top.
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# At evaluation time this patch is the *agent's*, so it is applied with every
# gold test file excluded -- a fix patch that edits the tests grading it cannot
# take effect. The gold fix patch touches only the three config.py files, so
# the exclusions are a no-op for dataset generation.
if ! git apply --whitespace=nowarn {gold_excludes} /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_pytest}
""".format(pr=self.pr, gold_excludes=gold_excludes, run_pytest=_RUN_PYTEST),
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


def parse_pytest_verbose(test_log: str) -> TestResult:
    """Classify every test in a ``pytest -v`` log by its node ID.

    Two passes over the same log, because one is not enough:

    * ``_VERBOSE_RE`` reads the per-test progress lines, which is where all
      normally-collected tests appear.
    * ``_SUMMARY_RE`` reads the trailing ``short test summary info`` block,
      which is the only place a *collection* error surfaces with a node ID --
      ``--continue-on-collection-errors`` keeps the run alive past one, but the
      affected tests never produce a progress line.

    The summary pass runs second and does not overwrite a verdict already
    recorded from a progress line: the summary repeats failures that the first
    pass has already classified, so letting it win would be a no-op at best and
    a downgrade of an ``XFAIL`` at worst.

    ANSI is stripped first. The run scripts already pass ``--color=no``, so in
    practice the log arrives clean -- but both patterns anchor on ``^`` and a
    single leaked escape sequence at the start of a line would silently drop
    every match rather than fail loudly, which is the worst possible failure
    mode for a parser. One substitution removes that class of bug entirely.
    """
    text = ANSI_ESCAPE.sub("", test_log or "")

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    def record(nodeid: str, status: str) -> None:
        if nodeid in passed or nodeid in failed or nodeid in skipped:
            return
        if status in ("PASSED", "XPASS"):
            passed.add(nodeid)
        elif status in ("FAILED", "ERROR"):
            failed.add(nodeid)
        elif status in ("SKIPPED", "XFAIL"):
            skipped.add(nodeid)

    for m in _VERBOSE_RE.finditer(text):
        record(m.group("nodeid"), m.group("status"))

    for m in _SUMMARY_RE.finditer(text):
        record(m.group("nodeid"), m.group("status"))

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("permitio", "opal")
class Opal(Instance):
    """Instance handler for permitio/opal.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create``
    resolves on.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpalImageDefault(self.pr, self._config)

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
        return parse_pytest_verbose(test_log)
