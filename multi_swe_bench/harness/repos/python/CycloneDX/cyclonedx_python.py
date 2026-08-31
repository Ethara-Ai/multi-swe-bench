import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

POETRY_VERSION = "1.1.11"

# Identical in all three graded stages - only patch application differs between them.
#
# `python -m pytest` rather than the bare `pytest` binary: tests/ imports both `tests` and
# `cyclonedx_py` from the repo root, and `python -m` puts the cwd on sys.path where the
# console script does not.
#
# The project's own runner is `python -m unittest` (see tox.ini). pytest is used here
# purely because unittest has no machine-readable output; pytest discovers and executes
# unittest.TestCase subclasses unchanged, so the tests that run and their outcomes are the
# same - only the reporting differs, and --junitxml gives one <testcase> per method.
#
# --continue-on-collection-errors is cheap insurance rather than a necessity here: the new
# tests fail at call time (an unexpected keyword argument), not import time, so the modules
# still collect. It keeps one bad import from taking the whole stage to zero results.
TEST_CMD = (
    "python -m pytest -rA --tb=short -p no:cacheprovider "
    "--continue-on-collection-errors "
    "--junitxml=/home/junit.xml tests/"
)


class CycloneDxPythonImageBase(Image):
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
        # pyproject declares python = "^3.6" and CI matrixes 3.6-3.10, but its
        # PYTHON_VERISON_DEFAULT (sic) is 3.10, which is what the lint and default test
        # jobs run on. -bullseye pins the distro so the tag cannot drift underneath us.
        # The full image (not -slim) already ships git and a C toolchain, so no apt is
        # needed for the handful of pure-Python dependencies this project has.
        return "python:3.10-bullseye"

    def image_tag(self) -> str:
        # Per-PR base image. A shared "base" tag would be pinned to whichever
        # BASE_COMMIT built it first, and the history scrub then deletes every
        # other commit - so a second PR could not check its own base out.
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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # DEBIAN_FRONTEND and LANG are deliberately NOT set here: DockerfileEnhancer
        # already injects both (with TZ and the proxy/CA wiring) in the block it places
        # right after FROM. Re-declaring them only produces duplicate ENV lines.
        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /home/

{code}

{copy_commands}

{self.clear_env}

"""


class CycloneDxPythonImageDefault(Image):
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
        return CycloneDxPythonImageBase(self.pr, self._config)

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
                "reset_pytest.sh",
                # /home/junit.xml is the single source of truth for results, so it must not
                # survive from one stage into the next. __pycache__ goes too so each stage
                # imports freshly compiled modules - the parser signatures change between
                # the test and fix stages.
                """#!/bin/bash
rm -f /home/junit.xml
rm -rf .pytest_cache
find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true
""",
            ),
            File(
                ".",
                "print_test_detail.sh",
                # pytest's console output is version-dependent and its short summary is easy
                # to mis-parse at file granularity - collapsing every result in a file into
                # one entry is exactly what hides a single added test method. The JUnit XML
                # carries one <testcase> per test with file/classname/name, which is enough
                # to rebuild the real pytest node id.
                """#!/bin/bash
echo "===== BEGIN PYTEST DETAIL ====="
python - <<'PYEOF'
import os
import xml.etree.ElementTree as ET

path = "/home/junit.xml"
if not os.path.exists(path):
    print("NO_JUNIT_XML")
else:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        print("JUNIT_XML_UNPARSEABLE")
        root = None
    if root is not None:
        for case in root.iter("testcase"):
            name = case.get("name") or ""
            classname = case.get("classname") or ""
            fname = case.get("file") or ""
            # A collection error is reported as a testcase whose name IS the file path.
            # Emitting "<file>::<file>" would just be noise, so collapse it to the file.
            if name == fname:
                name = ""
            # pytest writes classname as the dotted module path plus any class chain.
            # Stripping the module part leaves the class chain, so
            # <file>::<Class>::<name> reconstructs the node id an agent can rerun.
            module = fname[:-3].replace("/", ".") if fname.endswith(".py") else ""
            cls = ""
            if module and classname.startswith(module):
                cls = classname[len(module):].lstrip(".")
            elif not module:
                cls = classname
            parts = [fname or classname.replace(".", "/")]
            if cls:
                parts.append(cls)
            if name:
                parts.append(name)
            ident = "::".join(p for p in parts if p)
            if case.find("failure") is not None or case.find("error") is not None:
                status = "FAILED"
            elif case.find("skipped") is not None:
                status = "SKIPPED"
            else:
                status = "PASSED"
            print("TESTCASE " + ident + " " + status)
PYEOF
echo "===== END PYTEST DETAIL ====="
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

# Install the way CI does - poetry against the checked-in poetry.lock, pinned to the same
# version the workflow uses. virtualenvs.create=false puts everything in the system
# interpreter so the run scripts need no activation step.
pip install --no-cache-dir "poetry=={poetry_version}"
poetry config virtualenvs.create false

# pytest is installed BEFORE poetry so the lock gets the last word on shared transitive
# dependencies, and it is PINNED to the project's era. An unpinned install pulls pytest 9,
# which upgrades `packaging` past 22.0 - and pip_requirements_parser (a locked RUNTIME
# dependency) imports packaging.version.LegacyVersion, which 22.0 removed. The result is an
# ImportError in cyclonedx_py.parser.requirements that has nothing to do with the PR.
pip install --no-cache-dir "pytest==7.1.2"

poetry install --no-root || true

# Fallback: if that old poetry cannot resolve the lock on this interpreter, install the
# runtime dependencies straight from pyproject's ranges. Only one of these two paths needs
# to work, and the hard gate below is what decides whether we proceed.
python -c "import cyclonedx_py.parser.requirements" 2>/dev/null || pip install --no-cache-dir \\
    "cyclonedx-python-lib>=2.0.0,<3.0.0" "packageurl-python>=0.9" \\
    "pip-requirements-parser>=31.2.0,<32.0.0" "setuptools>=47.0.0" "toml>=0.10.0"

# Whichever install path ran above, force `packaging` back below the 22.0 boundary where
# LegacyVersion was removed. The lock pins 21.3; this makes that hold even if the poetry
# path failed and the pip fallback resolved something newer.
pip install --no-cache-dir "packaging<22"

# Hard gate, deliberately NOT tolerant: if the base tree cannot be imported then no stage
# can produce results, and that must fail HERE rather than three stages later behind a
# silently empty report.
python -c "import cyclonedx_py.parser.requirements, cyclonedx_py.parser.conda, cyclonedx_py.parser.poetry, pytest; print('DEPS_OK')"

""".format(pr=self.pr, poetry_version=POETRY_VERSION),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/reset_pytest.sh
set +e
{test_cmd}
PYTEST_RC=$?
set -e
echo "PYTEST_EXIT_CODE=$PYTEST_RC"
bash /home/print_test_detail.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/reset_pytest.sh
set +e
{test_cmd}
PYTEST_RC=$?
set -e
echo "PYTEST_EXIT_CODE=$PYTEST_RC"
bash /home/print_test_detail.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/reset_pytest.sh
set +e
{test_cmd}
PYTEST_RC=$?
set -e
echo "PYTEST_EXIT_CODE=$PYTEST_RC"
bash /home/print_test_detail.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
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


@Instance.register("CycloneDX", "cyclonedx-python")
class CycloneDxPython(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CycloneDxPythonImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # Only the lines emitted by print_test_detail.sh are authoritative. pytest's own
        # short-summary lines are ignored on purpose: a regex that stops at the first ":"
        # collapses "tests/x.py::Class::test" to "tests/x.py", which merges every test in
        # a file into one result and makes a single added test method invisible.
        case_re = re.compile(r"^TESTCASE (\S+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in test_log.splitlines():
            if line.startswith("===== BEGIN PYTEST DETAIL ====="):
                in_detail = True
                continue
            if line.startswith("===== END PYTEST DETAIL ====="):
                in_detail = False
                continue
            if not in_detail:
                continue

            m = case_re.match(line)
            if not m:
                continue

            name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
