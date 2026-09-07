"""Registry module for vprusso/toqito, PR #645 down to #614.

Era: PRs #614-#645 (toqito 1.0.8, Jun-Jul 2024).

What pins this era, read off the tree at the two base SHAs in the dataset
(1122a6e6 for #614, d80f59aa for #645):

  * No poetry.lock. Dependencies are resolved from pyproject's exact pins,
    so the resolver has to run - hence Poetry 1.8, the last line that still
    ships the poetry-core 1.x `poetry.masonry.api` backend this pyproject
    declares.
  * pyproject pins numpy 1.26.4 / scipy 1.13-1.14 / cvxpy 1.5.x and
    qiskit 1.1.x. numpy 1.x is why this cannot share an image with the
    #1026-#1077 era.
  * requires-python is ">=3.10,<4" and CI matrixes 3.10/3.11/3.12; 3.11 is
    the version every one of those pins publishes a cp311 wheel for.
  * Tests already live under toqito/**/tests/, not the top-level tests/
    directory the #62 era used.

Structure follows the two-image convention: ImageBase returns a *string*
dependency, which is what makes DockerfileEnhancer rewrite its trailing
`git clone` into the standard clone/checkout/history-hardening block and
prepend the syntax directive, build ARGs, proxy wiring, CA symlinks and OCI
labels. ImageDefault returns an Image, so its Dockerfile is passed through
verbatim: FROM the base, the COPY block, then `RUN bash /home/prepare.sh`.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The PR numbers this module owns, for reference when auditing coverage.
# toqito.py keeps its own copy of the interval boundaries rather than
# importing this, so the era modules stay free of cross-imports.
PR_NUMBERS = (614, 645)

# Pinned so a rebuild resolves the same toolchain. See the era notes above for
# why these particular versions and not the neighbouring eras'.
BASE_IMAGE = "python:3.11-bookworm"
POETRY_SPEC = "poetry==1.8.5"
PYTEST_SPEC = "pytest==8.2.2"

# Byte-identical in all three graded stages; only patch application differs.
#
# `python -m pytest` rather than the bare `pytest` console script: `-m` puts
# the repo root on sys.path ahead of site-packages, so the tests import the
# patched working tree.
#
# --junitxml is what parse_log actually reads. pytest's console summary
# collapses a node id at the first ":", which merges every test in a file into
# one result and makes a single added test method invisible; the XML carries
# one <testcase> per test with file/classname/name.
TEST_CMD = (
    'python -m pytest -rA --tb=short -p no:cacheprovider '
    '--continue-on-collection-errors --junitxml=/home/junit.xml "$TEST_TARGET"'
)


class Toqito645To614ImageBase(Image):
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
        return BASE_IMAGE

    def image_tag(self) -> str:
        # Per-PR base image. A shared "base" tag would be pinned to whichever
        # BASE_COMMIT built it first, and the enhancer's history hardening
        # then deletes every other commit - so a second PR in the same era
        # could not check its own base out.
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

        # No `# syntax` directive, no ARG/proxy/LABEL/CA block and nothing
        # after the clone: DockerfileEnhancer injects the first and rewrites
        # the clone into clone + WORKDIR + reset + checkout + hardening + CMD,
        # so any instruction emitted below it would land under that CMD.
        #
        # DEBIAN_FRONTEND, LANG and TZ are deliberately absent - the enhancer
        # already sets all three right after FROM.
        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_ROOT_USER_ACTION=ignore
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV POETRY_VIRTUALENVS_CREATE=false
ENV POETRY_NO_INTERACTION=1

WORKDIR /home/

{code}
"""


class Toqito645To614ImageDefault(Image):
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
        return Toqito645To614ImageBase(self.pr, self._config)

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
                r"""#!/bin/bash
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
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_ROOT_USER_ACTION=ignore
export POETRY_VIRTUALENVS_CREATE=false
export POETRY_NO_INTERACTION=1

# BLAS/LAPACK: the project's own CI installs these before building the
# scientific stack (.travis.yml in the 2021 era, build-test-actions.yml from
# 2024 on). Wheels usually vendor OpenBLAS, so this is belt-and-braces and is
# allowed to fail on an image whose apt lists are unavailable.
apt-get update > /dev/null 2>&1 && apt-get install -y --no-install-recommends \
    libblas-dev liblapack-dev gfortran > /dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# Test target discovery.
#
# Read off the checked-out tree rather than hard-coded: #62 keeps its tests in
# a top-level tests/ package, every later era keeps them in {pkg}/**/tests/.
# The answer is frozen into /home/test_target at image-build time so all three
# run scripts issue the identical pytest invocation.
# ---------------------------------------------------------------------------
TEST_TARGET=""
if [ -d tests ] && [ -n "$(find tests -name 'test_*.py' -print -quit)" ]; then
    TEST_TARGET="tests"
elif [ -d {pkg} ] && [ -n "$(find {pkg} -name 'test_*.py' -print -quit)" ]; then
    TEST_TARGET="{pkg}"
else
    TEST_TARGET="."
fi
echo "$TEST_TARGET" > /home/test_target
echo "prepare: test target resolved to '$TEST_TARGET'"

# ---------------------------------------------------------------------------
# Dependency install.
#
# The build system is detected from the checked-out tree, not assumed: a
# [tool.poetry] table means Poetry, then requirements.txt, then setup.py.
# --no-root is deliberate. Installing the project would put a second copy of
# {pkg} in site-packages, and fix.patch edits the working tree - the run
# scripts use `python -m pytest` from the repo root so the tree wins on
# sys.path either way, but not installing it removes the ambiguity entirely.
# ---------------------------------------------------------------------------
pip install --no-cache-dir --upgrade pip setuptools wheel || true

if [ -f pyproject.toml ] && grep -qE '^\[tool\.poetry\]' pyproject.toml; then
    pip install --no-cache-dir "{poetry_spec}" || true
    poetry config virtualenvs.create false || true
    # Every attempt is wrapped in `timeout`. Poetry's resolver has no bound of
    # its own, and the 2021 era declares all its dependencies as "*" - a search
    # space old Poetry will backtrack through for hours without converging.
    # Cutting it off hands the work to the deterministic pip fallback below,
    # which is the path that era needs anyway.
    #
    # --with/--without are Poetry >= 1.2 spellings; on 1.1 they raise
    # NoSuchOptionException and the chain falls through to the bare form.
    timeout 900 poetry install --no-root --with dev --without docs,lint \
        || timeout 900 poetry install --no-root --without docs,lint \
        || timeout 900 poetry install --no-root \
        || true
elif [ -f requirements.txt ]; then
    pip install --no-cache-dir -r requirements.txt || true
elif [ -f setup.py ] || [ -f setup.cfg ]; then
    pip install --no-cache-dir -e . || true
fi

python -c "import pytest" 2>/dev/null || pip install --no-cache-dir "{pytest_spec}" || true

# ---------------------------------------------------------------------------
# Whether the environment is usable is decided by pytest itself: collection
# imports every test module, which in turn imports the project and its
# dependencies.
#
# An `import {pkg}` check would NOT do. {pkg}/__init__.py is empty (0 bytes)
# in the 2024+ eras and a bare version string in the 2021 one, so it succeeds
# against a tree with nothing installed at all - a gate that can never fail is
# worse than no gate.
# ---------------------------------------------------------------------------
collect_ok() {{
    python -m pytest --collect-only -q -p no:cacheprovider "$TEST_TARGET" \
        > /home/collect.log 2>&1
}}

# ---------------------------------------------------------------------------
# Fallback. If the resolver above could not produce a collectable tree - the
# expected case on the 2021 era, whose "*" constraints have no solution a
# modern resolver will accept - install the declared dependency names directly
# and let pip choose whatever the interpreter supports.
# ---------------------------------------------------------------------------
if ! collect_ok; then
    echo "prepare: collection failed after the primary install, falling back"
    tail -n 20 /home/collect.log

    python - <<'PYSCRIPT' || true
import re
import subprocess
import sys

# pyproject and setup.py are merged rather than tried in turn: the 2021 tree
# declares cvx/cvxpy/numpy/scipy in pyproject but picos and scikit-image only
# in setup.py, and its tests import all of them.
names = []


def add(name):
    if name and name != "python" and name not in names:
        names.append(name)


try:
    with open("pyproject.toml", encoding="utf-8") as handle:
        text = handle.read()
except OSError:
    text = ""

for header in (
    "[tool.poetry.dependencies]",
    "[tool.poetry.dev-dependencies]",
    "[tool.poetry.group.dev.dependencies]",
):
    start = text.find(header)
    if start == -1:
        continue
    body = text[start + len(header):]
    end = body.find("\n[")
    if end != -1:
        body = body[:end]
    for line in body.splitlines():
        match = re.match(r'^\s*([A-Za-z0-9._-]+)\s*=', line)
        if match:
            add(match.group(1))

try:
    with open("setup.py", encoding="utf-8") as handle:
        setup_text = handle.read()
except OSError:
    setup_text = ""

for pattern in (
    r'requirements\s*=\s*\[([^\]]*)\]',
    r'install_requires\s*=\s*\[([^\]]*)\]',
):
    block = re.search(pattern, setup_text)
    if block:
        for name in re.findall(r'["\']([A-Za-z0-9._-]+)', block.group(1)):
            add(name)

print("prepare: fallback install of " + " ".join(names))
for name in names:
    subprocess.call([sys.executable, "-m", "pip", "install", "--no-cache-dir", name])
PYSCRIPT

    python -c "import pytest" 2>/dev/null || pip install --no-cache-dir "{pytest_spec}" || true
fi

# ---------------------------------------------------------------------------
# Hard gate, deliberately without `|| true`. If pytest cannot collect here then
# no stage can produce results, and that has to fail during the build rather
# than three stages later behind a silently empty report.
# ---------------------------------------------------------------------------
if ! collect_ok; then
    echo "prepare: FATAL - pytest cannot collect tests from '$TEST_TARGET'"
    tail -n 60 /home/collect.log
    exit 1
fi

python -m pytest --version
tail -n 3 /home/collect.log
echo "DEPS_OK"
""".format(
                    pr=self.pr,
                    pkg=self.pr.repo.replace("-", "_"),
                    poetry_spec=POETRY_SPEC,
                    pytest_spec=PYTEST_SPEC,
                ),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
rm -f /home/junit.xml
rm -rf .pytest_cache
find . -name '__pycache__' -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true

TEST_TARGET="$(cat /home/test_target)"

set +e
python -m pytest -rA --tb=short -p no:cacheprovider --continue-on-collection-errors --junitxml=/home/junit.xml "$TEST_TARGET"
PYTEST_RC=$?
set -e
echo "PYTEST_EXIT_CODE=$PYTEST_RC"

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
            # A collection error is reported as a testcase whose name IS the
            # file path; "<file>::<file>" would just be noise.
            if name == fname:
                name = ""
            # pytest writes classname as the dotted module path plus any class
            # chain. Stripping the module part leaves the class chain, so
            # <file>::<Class>::<name> reconstructs a runnable node id.
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
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
rm -f /home/junit.xml
rm -rf .pytest_cache
find . -name '__pycache__' -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true

TEST_TARGET="$(cat /home/test_target)"

set +e
python -m pytest -rA --tb=short -p no:cacheprovider --continue-on-collection-errors --junitxml=/home/junit.xml "$TEST_TARGET"
PYTEST_RC=$?
set -e
echo "PYTEST_EXIT_CODE=$PYTEST_RC"

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
            # A collection error is reported as a testcase whose name IS the
            # file path; "<file>::<file>" would just be noise.
            if name == fname:
                name = ""
            # pytest writes classname as the dotted module path plus any class
            # chain. Stripping the module part leaves the class chain, so
            # <file>::<Class>::<name> reconstructs a runnable node id.
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
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
rm -f /home/junit.xml
rm -rf .pytest_cache
find . -name '__pycache__' -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true

TEST_TARGET="$(cat /home/test_target)"

set +e
python -m pytest -rA --tb=short -p no:cacheprovider --continue-on-collection-errors --junitxml=/home/junit.xml "$TEST_TARGET"
PYTEST_RC=$?
set -e
echo "PYTEST_EXIT_CODE=$PYTEST_RC"

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
            # A collection error is reported as a testcase whose name IS the
            # file path; "<file>::<file>" would just be noise.
            if name == fname:
                name = ""
            # pytest writes classname as the dotted module path plus any class
            # chain. Stripping the module part leaves the class chain, so
            # <file>::<Class>::<name> reconstructs a runnable node id.
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("vprusso", "toqito_645_to_614")
class TOQITO_645_TO_614(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Toqito645To614ImageDefault(self.pr, self._config)

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

        # Colour codes first: every pattern below is anchored, and an escape
        # sequence at the start of a line defeats the anchor.
        test_log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        # Only the TESTCASE lines the run scripts emit from the JUnit XML are
        # authoritative. pytest's own short-summary lines are ignored on
        # purpose - see the TEST_CMD note about node ids collapsing at ":".
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

            match = case_re.match(line)
            if not match:
                continue

            name, status = match.group(1), match.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # TestResult.__post_init__ rejects overlapping sets with a ValueError,
        # which would kill the instance's report.
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
