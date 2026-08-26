import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Identical in all three graded stages - only patch application differs between them.
#
# `python -m pytest` (not bare `pytest`) is REQUIRED: at the base commit tests/ has no
# __init__.py, so pytest puts tests/ on sys.path instead of the repo root and `import dyc`
# fails. `python -m` adds the cwd, which fixes it.
#
# --continue-on-collection-errors is REQUIRED too. tests/test_classes.py imports
# dyc.classes, which only the fix patch creates, so at the test stage that file raises
# ImportError during collection. Without this flag pytest aborts the whole session there
# and tests/test_utils.py never runs either - which pushes 12 otherwise-clean p2p tests
# through the "hidden at test stage" branch into reclassified_from_target and leaves
# fixed_tests (19) disagreeing with f2p+s2p+n2p (7). That mismatch is what
# audit/domain.py's reward_bucket_consistency_check rejects.
TEST_CMD = (
    "python -m pytest -rA --tb=short -p no:cacheprovider "
    "--continue-on-collection-errors "
    "--junitxml=/home/junit.xml tests/"
)


class DycImageBase(Image):
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
        # requirements.txt pins pytest==4.4.0, which is broken on Python 3.8+: pytest
        # below 5.1 cannot do assertion rewriting after the 3.8 ast.Module change
        # (missing "type_ignores"). tests/test_classes.py also documents that it fails
        # on 2.7. That leaves 3.7, and 3.7.17 is the final release, so the only thing
        # left floating is the distro - hence the explicit -bookworm, which is what the
        # bare python:3.7 tag resolves to today. The full image (not -slim) already
        # ships git, so this config needs no apt-get at all.
        return "python:3.7-bookworm"

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
        #
        # pip and setuptools are deliberately NOT upgraded. The image ships pip 23.0.1
        # and setuptools 57.5.0, which are already the last releases that support 3.7 -
        # `pip install --upgrade pip` would fetch a wheel that refuses to run here.
        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /home/

{code}

{copy_commands}

{self.clear_env}

"""


class DycImageDefault(Image):
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
        return DycImageBase(self.pr, self._config)

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
                # /home/junit.xml is the single source of truth for results, so it must
                # not survive from one stage into the next. __pycache__ is dropped too so
                # each stage imports freshly compiled modules - important here because
                # dyc/classes.py only exists after the fix patch.
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
                # pytest's console output is version-dependent and its short summary is
                # easy to mis-parse at file granularity - collapsing every result in a
                # file into one entry is exactly what hides a single added test method.
                # The JUnit XML carries one <testcase> per test with file/classname/name,
                # which is enough to rebuild the real pytest node id.
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
            # A collection error (e.g. the ImportError that tests/test_classes.py
            # raises before the fix patch adds dyc/classes.py) is reported as a
            # testcase whose name IS the file path. Emitting "<file>::<file>" would
            # just be noise, so collapse it to the file.
            if name == fname:
                name = ""
            # pytest writes classname as the dotted module path plus any class chain.
            # Stripping the module part of the dotted path leaves the class chain, so
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

# Install the four packages the suite actually imports, plus watchdog. watchdog is the
# trap here: setup.py's install_requires carries it but requirements.txt does not, and
# dyc/dyc.py does `from watchdog.observers import Observer`. tests/test_classes.py does
# `from dyc.dyc import start`, so with no watchdog that file fails collection at EVERY
# stage - including fix - and the instance would credit nothing at all.
#
# "<3" rather than setup.py's watchdog==0.9.0 is a deliberate deviation: 0.9.0 is a 2018
# sdist that pulls pathtools, which does not build under this image's setuptools 57. The
# suite only ever touches watchdog.observers.Observer and watchdog.events, both stable
# across the 0.x-2.x range, so the resolved 2.x is behaviourally equivalent here.
pip install --no-cache-dir \\
    "click==7.0" "pyyaml>=4.2b1" "gitpython>=2.1.11" "pytest==4.4.0" "watchdog<3"

# requirements.txt additionally pins pre-commit==1.18.1, which the suite never imports
# and which drags in an old virtualenv/nodeenv chain. Try the full file for fidelity,
# but do not let pre-commit's resolution kill the image build.
pip install --no-cache-dir -r requirements.txt \\
    || echo "NOTE: full requirements.txt install incomplete (pre-commit is not needed by the suite)"

# Hard gate, deliberately NOT tolerant: if the base tree cannot be imported then no
# stage can produce results, and that must fail here rather than three stages later.
python -c "import click, yaml, git, watchdog, pytest, dyc.dyc; print('DEPS_OK')"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

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
set -e

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
set -e

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


@Instance.register("Zarad1993", "dyc")
class Dyc(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DycImageDefault(self.pr, self._config)

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

        failed_tests -= passed_tests
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
