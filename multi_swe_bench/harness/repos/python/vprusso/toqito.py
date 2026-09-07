"""Registry module for vprusso/toqito - one shared base image, all PRs.

`Instance.create()` (instance.py:41-50) builds its registry key like this:

    if pr.number_interval:  key = f"{pr.org}/{pr.number_interval}"
    elif pr.tag == "":      key = f"{pr.org}/{pr.repo}"
    else:                   key = f"{pr.org}/{pr.repo}_{pr.tag.replace('.', '_')}"

`output/vprusso__toqito_raw_dataset.jsonl` carries neither `number_interval`
nor `tag`, so every record in it resolves to the bare key `vprusso/toqito`,
which this module owns.

Why one base image
------------------
The predecessor of this module split the repo into three era modules, each
emitting its own `base-pr-{number}` image: five PRs meant five base images
plus five PR images. The split existed for one reason - the base image
carried the checkout *and* `Image._HARDENING_BLOCK`, which deletes every ref
except the base commit. A base pinned that way can only ever serve the one PR
that built it.

Here the base stops at a plain full-history fetch. Everything commit-specific
- reset, checkout, hardening - belongs to the per-PR image, which is what lets
a single base serve all five. The base is then only the interpreters, the
BLAS/LAPACK toolchain, the environment and the sources.

Two details of that base line are load-bearing, both about
`DockerfileEnhancer`:

  * It is spelled `git -C /home clone ... {repo}`, not `git clone ...
    /home/{repo}`. `_standardize_repo_fetch` (image.py:339-381) rewrites the
    latter into checkout + hardening, and `_inject_final_sanitize`
    (image.py:383-419) appends a hardening block to any Dockerfile whose text
    merely contains `git clone`, `git fetch` or `git remote add`. The `-C`
    form matches neither, so the base survives the enhancer unchanged - which
    is also why no comment in that Dockerfile may spell those tokens out.
  * `${REPO_URL}` resolves to the ARG the enhancer injects, which
    build_dataset.py:623-630 fills in because this image's dependency is a
    string.

What the eras needed, and where it went
---------------------------------------
The three eras still exist as facts about the tree; they are just no longer
separate images. Read off the five base SHAs in the dataset:

  * #62 (4b7399e0, 2021, toqito 1.0.0) - `python = "^3.7"`, every dependency
    declared `"*"`, tests in a top-level `tests/` package, no lockfile.
  * #614 / #645 (1122a6e6, d80f59aa, 2024, toqito 1.0.8) - `python =
    ">=3.10,<4"`, numpy pinned 1.26.4 with cvxpy 1.5.1 and qiskit 1.1.0,
    still no lockfile, tests under `toqito/**/tests/`.
  * #1026 / #1077 (3a67e7b6, 1bd4a128, 2025, toqito 1.1.1) - same
    requires-python, numpy 2.2.3, and a `poetry.lock` (lock-version 2.1).

Those three toolchains cannot share one interpreter: numpy 1.26 and numpy 2
are not interchangeable for this code, and the 2021 tree predates both. The
base therefore ships all three interpreters and `detect_toolchain.py` picks
one at image-build time by reading the checked-out tree - the requires-python
floor separates #62 from the rest, and the numpy major plus the presence of
`poetry.lock` separates the 2024 era from the 2025 one. Nothing is keyed on
the PR number, so a sixth PR lands on the right toolchain without a new
module.

Structure follows the two-image convention. ImageBase returns a *string*
dependency, so DockerfileEnhancer prepends the syntax directive, build ARGs,
proxy wiring, CA symlinks and OCI labels to it. ImageDefault returns an
Image, so its Dockerfile is passed through verbatim - which is what lets it
own the checkout and the hardening block explicitly, with the base sha
written out literally because no build arg reaches an image built on top of
another image.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The interpreter the base image is built on. Its own installation lives at
# /usr/local, so it is the toolchain entry whose home is /usr/local below.
BASE_IMAGE = "python:3.12-bullseye"

# Additional interpreters copied into the base, as (prefix, source image).
#
# All three come from bullseye images on purpose. A 3.7 built against
# bullseye's OpenSSL 1.1 cannot find libssl on a bookworm userland, and a
# python whose `ssl` module fails to import cannot reach PyPI at all.
#
# The copy relocates /usr/local to /opt/python/<version>. CPython derives
# sys.prefix from the landmark `lib/python<version>/os.py` next to its
# executable, so a relocated tree is self-consistent; the shared libpython is
# found via LD_LIBRARY_PATH, which python_env.sh exports.
EXTRA_PYTHONS = (
    ("3.7", "python:3.7-bullseye"),
    ("3.11", "python:3.11-bullseye"),
)

# Interpreter -> (prefix, poetry spec, pytest spec). These are the pins the
# era modules verified; only the way one is selected has changed.
#
# Poetry: 1.1 is the last line that runs on 3.7. 1.8 is the last that still
# ships the poetry-core 1.x `poetry.masonry.api` backend the 2024 pyproject
# declares. 2.1 reads the lock-version 2.1 lockfile the 2025 tree carries.
TOOLCHAINS = {
    "3.7": ("/opt/python/3.7", "poetry==1.1.15", "pytest==7.4.4"),
    "3.11": ("/opt/python/3.11", "poetry==1.8.5", "pytest==8.2.2"),
    "3.12": ("/usr/local", "poetry==2.1.1", "pytest==8.3.4"),
}

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
    "python -m pytest -rA --tb=short -p no:cacheprovider "
    '--continue-on-collection-errors --junitxml=/home/junit.xml "$TEST_TARGET"'
)

# Written into the PR image as /home/detect_toolchain.py and run from the
# repository root during prepare.sh. __TOOLCHAINS__ is substituted rather than
# formatted so neither this template nor the regexes in it need brace escaping.
_DETECT_TOOLCHAIN_TEMPLATE = r'''"""Choose the interpreter and toolchain for the checked-out tree.

Run by prepare.sh from the repository root, under the base image's own
interpreter, and writes /home/python_env.sh for every later script to source.

The choice is read off the tree, never off the PR number, so a PR that is not
in the dataset today still lands on the toolchain its own pyproject describes.
"""

import os
import re
import sys

TOOLCHAINS = __TOOLCHAINS__


def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


pyproject = read("pyproject.toml")
setup_py = read("setup.py")

# The requires-python floor, in Poetry's spelling first ([tool.poetry]
# `python = "^3.7"`), then PEP 621's, then setup.py's. Only the floor matters:
# the upper bound in ">=3.10,<4" is open enough to be no help.
match = re.search(r'^\s*python\s*=\s*["\']([^"\']+)["\']', pyproject, re.M)
if not match:
    match = re.search(
        r'^\s*requires-python\s*=\s*["\']([^"\']+)["\']', pyproject, re.M
    )
if not match:
    match = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']', setup_py)

constraint = match.group(1) if match else ""
version = re.search(r"(\d+)\.(\d+)", constraint)
floor = (int(version.group(1)), int(version.group(2))) if version else (3, 7)

# The numpy major the tree pins. `numpy = "*"` carries no digit and leaves this
# at 0, which is the right answer - an unpinned tree is the 2021 one.
numpy_pin = re.search(r'^\s*numpy\s*=\s*["\']?[^0-9"\']*(\d+)', pyproject, re.M)
numpy_major = int(numpy_pin.group(1)) if numpy_pin else 0

has_lock = os.path.exists("poetry.lock")

if floor < (3, 8):
    # 2021: every dependency declared "*", so the resolver installs whatever
    # the interpreter supports. Only 3.7 keeps that era's wheels reachable.
    choice = "3.7"
elif numpy_major >= 2 or has_lock:
    # 2025: numpy 2 and a lock-version 2.1 lockfile.
    choice = "3.12"
else:
    # 2024: numpy 1.26 with cvxpy 1.5 and qiskit 1.1, and no lockfile.
    choice = "3.11"

home, poetry_spec, pytest_spec = TOOLCHAINS[choice]

print(
    "detect_toolchain: requires-python=%s numpy_major=%d poetry_lock=%s"
    " -> python %s (%s)" % (constraint or "<none>", numpy_major, has_lock, choice, home)
)

lines = [
    "# Written by detect_toolchain.py at image-build time.",
    "export MSB_PYTHON_VERSION=%s" % choice,
    "export MSB_PY_HOME=%s" % home,
    "export MSB_POETRY_SPEC=%s" % poetry_spec,
    "export MSB_PYTEST_SPEC=%s" % pytest_spec,
    "export PATH=$MSB_PY_HOME/bin:$PATH",
    "export LD_LIBRARY_PATH=$MSB_PY_HOME/lib:/usr/local/lib",
    "",
]

with open("/home/python_env.sh", "w") as handle:
    handle.write("\n".join(lines))

sys.exit(0)
'''


def _detect_toolchain_py() -> str:
    return _DETECT_TOOLCHAIN_TEMPLATE.replace("__TOOLCHAINS__", repr(TOOLCHAINS))


# Shared by run.sh / test-run.sh / fix-run.sh. The only difference between the
# three scripts is the `git apply` line, so everything else is written once.
_STAGE_BODY = r"""rm -f /home/junit.xml
rm -rf .pytest_cache
find . -name '__pycache__' -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true

TEST_TARGET="$(cat /home/test_target)"

set +e
{test_cmd}
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
"""


def _stage_script(repo: str, apply_line: str) -> str:
    """One of the three graded run scripts.

    `source /home/python_env.sh` is what puts the interpreter chosen during
    prepare.sh on PATH, so `python` below means the same interpreter the
    dependencies were installed into.
    """
    header = (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "export CI=true\n"
        "\n"
        "source /home/python_env.sh\n"
        "\n"
        f"cd /home/{repo}\n"
    )
    if apply_line:
        header += f"{apply_line}\n"
    # str.format also turns the `{{}}` of the `find -exec rm -rf {} +` line
    # back into a literal `{}`.
    return header + _STAGE_BODY.format(test_cmd=TEST_CMD)


class ToqitoImageBase(Image):
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
        # One tag for every PR of this repo. Images are de-duplicated by
        # full name (image.py:83-95, build_dataset.py:665-681), so the five
        # PRs in the dataset agree on a single base build.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        versions = " ".join(version for version, _ in EXTRA_PYTHONS)
        copy_pythons = "\n".join(
            f"COPY --from={source} /usr/local /opt/python/{version}"
            for version, source in EXTRA_PYTHONS
        )

        # No `# syntax` directive here: DockerfileEnhancer adds it along with
        # the ARG/proxy/LABEL/CA block right after FROM. DEBIAN_FRONTEND, LANG
        # and TZ are deliberately absent - the enhancer already sets all three.
        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_ROOT_USER_ACTION=ignore
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV POETRY_VIRTUALENVS_CREATE=false
ENV POETRY_NO_INTERACTION=1

# The project's own CI installs these before building the scientific stack
# (.travis.yml in the 2021 era, build-test-actions.yml from 2024 on). Wheels
# usually vendor OpenBLAS, so this is belt-and-braces - but it belongs here
# rather than in prepare.sh, where every PR image would pay for it again.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    libblas-dev \\
    liblapack-dev \\
    gfortran \\
    && rm -rf /var/lib/apt/lists/*

{copy_pythons}

# `python` is only a versioned name in a relocated tree, and the `pip` console
# script still carries its original /usr/local shebang - hence the symlink and
# `python -m pip` everywhere downstream. The import check fails the build here
# rather than during a prepare.sh that cannot reach PyPI.
RUN set -eux; \\
    for version in {versions}; do \\
        ln -sf "/opt/python/$version/bin/python$version" "/opt/python/$version/bin/python"; \\
        LD_LIBRARY_PATH="/opt/python/$version/lib" \\
            "/opt/python/$version/bin/python" -c "import ssl, sysconfig"; \\
    done

WORKDIR /home/

RUN git -C /home clone "${{REPO_URL}}" {repo}

CMD ["/bin/bash"]
"""


class ToqitoImageDefault(Image):
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
        return ToqitoImageBase(self.pr, self._config)

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
            File(".", "detect_toolchain.py", _detect_toolchain_py()),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

# ---------------------------------------------------------------------------
# Toolchain selection.
#
# The base image carries three interpreters and no opinion about which one
# this PR needs. detect_toolchain.py reads the checked-out tree and freezes
# the answer into /home/python_env.sh, which every script below and all three
# graded run scripts source - so the environment tests run in is the one the
# dependencies were installed into.
#
# It runs under the base image's own interpreter (an absolute path, because
# nothing is on PATH yet), and the sourced file is what puts the chosen one
# ahead of it.
# ---------------------------------------------------------------------------
/usr/local/bin/python /home/detect_toolchain.py
cat /home/python_env.sh
source /home/python_env.sh

python -V

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_ROOT_USER_ACTION=ignore
export POETRY_VIRTUALENVS_CREATE=false
export POETRY_NO_INTERACTION=1

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
#
# `python -m pip` rather than the `pip` console script: two of the three
# interpreters are relocated copies whose console-script shebangs still point
# at the /usr/local path they were built for.
# ---------------------------------------------------------------------------
python -m pip install --no-cache-dir --upgrade pip setuptools wheel || true

if [ -f pyproject.toml ] && grep -qE '^\[tool\.poetry\]' pyproject.toml; then
    python -m pip install --no-cache-dir "$MSB_POETRY_SPEC" || true
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
    python -m pip install --no-cache-dir -r requirements.txt || true
elif [ -f setup.py ] || [ -f setup.cfg ]; then
    python -m pip install --no-cache-dir -e . || true
fi

python -c "import pytest" 2>/dev/null \
    || python -m pip install --no-cache-dir "$MSB_PYTEST_SPEC" || true

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

# A failed collection ends with one ERROR summary line per test module - 113 of
# them on this repo - so `tail` shows the roll-call and never the reason. Print
# the distinct exception lines and the first traceback instead, which is what
# actually names the missing or unimportable dependency.
collect_why() {{
    echo "--- distinct collection errors ---"
    grep -E '^E ' /home/collect.log | sort -u | head -n 30
    echo "--- first traceback ---"
    sed -n '/^=* ERRORS =*$/,$p' /home/collect.log | head -n 60
}}

# ---------------------------------------------------------------------------
# Fallback. If the resolver above could not produce a collectable tree - the
# expected case on the 2021 era, whose "*" constraints have no solution a
# modern resolver will accept - install the declared dependency names directly
# and let pip choose whatever the interpreter supports.
# ---------------------------------------------------------------------------
if ! collect_ok; then
    echo "prepare: collection failed after the primary install, falling back"
    collect_why

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

    python -c "import pytest" 2>/dev/null \
        || python -m pip install --no-cache-dir "$MSB_PYTEST_SPEC" || true
fi

# ---------------------------------------------------------------------------
# Hard gate, deliberately without `|| true`. If pytest cannot collect here then
# no stage can produce results, and that has to fail during the build rather
# than three stages later behind a silently empty report.
# ---------------------------------------------------------------------------
if ! collect_ok; then
    echo "prepare: FATAL - pytest cannot collect tests from '$TEST_TARGET'"
    collect_why
    exit 1
fi

python -m pytest --version
tail -n 3 /home/collect.log
echo "DEPS_OK"
""".format(
                    pr=self.pr,
                    pkg=self.pr.repo.replace("-", "_"),
                ),
            ),
            File(".", "run.sh", _stage_script(self.pr.repo, "")),
            File(
                ".",
                "test-run.sh",
                _stage_script(
                    self.pr.repo,
                    "git apply --whitespace=nowarn /home/test.patch",
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _stage_script(
                    self.pr.repo,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # dependency() is an Image, so DockerfileEnhancer.enhance (image.py:311)
        # returns this verbatim, and build_dataset.py:623-630 passes build args
        # only for string dependencies. There is therefore no BASE_COMMIT ARG
        # to read here: the sha is written into every line that needs it, which
        # is also what makes the built image self-describing.
        repo = self.pr.repo
        sha = self.pr.base.sha
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout {sha}

{hardening}
{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("vprusso", "toqito")
class TOQITO(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ToqitoImageDefault(self.pr, self._config)

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
