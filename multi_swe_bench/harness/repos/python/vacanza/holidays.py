import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# vacanza/holidays - PRs 570, 630, 651, 698, 716 (Dec 2021 - Sep 2022).
#
# RUNTIME. The repo's own ci-cd.yml matrix is [3.6, 3.7, 3.8, 3.9, pypy3] at the
# oldest base commit and ["3.7", "3.8", "3.9", "3.10", "pypy-3.7"] at the
# newest. 3.9 is the ONE version present in both, so a single runtime covers
# every PR here and no era mapping is needed. setup.cfg declares
# python_requires >=3.6. The sibling era config holidays_728_to_570.py (same PR
# range) also uses python:3.9 - this agrees, but pins the distro too.
PYTHON_IMAGE = "python:3.9-slim-bullseye"

# DEPENDENCY PINS. setup.cfg's install_requires is effectively unpinned
# (`convertdate>=2.3.0`, bare `hijri_converter`, `korean_lunar_calendar`,
# `python-dateutil`), so a plain `pip install -e .` resolves to TODAY's releases
# against a 2021/2022 tree. These are the era-appropriate versions. All are pure
# Python, so the same set installs on linux/amd64 and linux/arm64 with no
# compiler and no per-arch wheel gaps.
#
# Installed INLINE in the base Dockerfile rather than from a COPY'd
# requirements file, so `images/base/` holds only Dockerfile + build_image.log.
PINNED_REQUIREMENTS = [
    "python-dateutil==2.8.2",
    "convertdate==2.4.0",
    "hijri-converter==2.2.4",
    "korean-lunar-calendar==0.2.1",
    "pytest==7.1.3",
]

# --junitxml      : machine-readable output (HANDOFF rule b). The console -v
#                   text is NOT parsed: pytest's short summary prints
#                   "FAILED test/x.py::T::test_y - AttributeError: ..." and a
#                   regex over that captures the error message INTO the test id,
#                   so the same test gets a different id at the test stage than
#                   at the fix stage and the transition is silently lost.
#                   The sibling era config parses that console text; this does
#                   not, deliberately.
# --override-ini addopts= : tox.ini carries `[pytest] addopts = --cov=./ ...`.
#                   pytest reads [pytest] out of tox.ini, so a bare run needs
#                   pytest-cov present or it aborts before collecting anything
#                   (this really happened - exit 4 - see BUILD_ERRORS/).
# junit_family=xunit1 : pytest's default (xunit2) omits the `file` attribute on
#                   <testcase>, leaving only a dotted classname. Ids rebuilt
#                   from that read "test/countries/test_malta/TestMT.py::test_x"
#                   - stable and unique, but NOT valid pytest node ids, so
#                   nobody downstream could re-run a credited test.
# --continue-on-collection-errors : one bad import must not zero a whole stage.
TEST_CMD = (
    "python -m pytest test/ -v --tb=short "
    "--override-ini=addopts= "
    "--override-ini=junit_family=xunit1 "
    "-p no:cacheprovider "
    "--continue-on-collection-errors "
    "--junitxml=/home/results.xml"
)

BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="


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
        # A STRING, so the harness treats this as a true base image and the five
        # PR images all dedupe onto the single tag returned by image_tag().
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        # SHARED across all five PRs. image_full_name() is what the harness
        # dedupes on, so this builds exactly ONE base image.
        #
        # Safe because the base stops at `git clone` and never checks out a
        # commit. The clone is identical for every PR; the per-PR checkout and
        # the ref-deleting hardening live in the PR layer. Were the checkout
        # here, this single deduped image would be pinned to whichever PR built
        # first and the other four would silently test the WRONG commit.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        # Empty on purpose: images/base/ must contain only Dockerfile and
        # build_image.log, so the dependency pins are installed inline instead
        # of from a COPY'd requirements file.
        return []

    def dockerfile(self) -> str:
        # Emitted WITH the syntax directive as the first line. DockerfileEnhancer
        # .enhance() is idempotent - it early-returns on content that already
        # carries the directive - so this file reaches Docker exactly as written.
        #
        # That matters for more than tidiness. `_inject_final_sanitize` injects
        # WORKDIR + the whole hardening block immediately before CMD in any
        # Dockerfile containing the string "git clone". Since the base MUST end
        # at the clone, letting the enhancer rewrite it would force the git
        # stripping into the base - precisely what the layering rule forbids.
        #
        # The infra (ARGs, ENV trust block, OCI labels, CA symlink farm) is not
        # hand-copied: it is generated by the harness's own
        # _infrastructure_block, so it cannot drift from what the enhancer would
        # have produced.
        infra = DockerfileEnhancer._infrastructure_block(self, PYTHON_IMAGE)
        pins = " \\\n        ".join(PINNED_REQUIREMENTS)

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {PYTHON_IMAGE}

{infra}
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --retries 10 --timeout 120 \\
        {pins} && \\
    python -c "import dateutil, convertdate, hijri_converter, korean_lunar_calendar, pytest" && \\
    echo "BASE_DEPS_OK"

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


def _stage_script(repo: str, apply_line: str) -> str:
    """One graded stage: optional patch apply, then the shared test command.

    All three stages are built from THIS function, so the test command exists in
    exactly one place in the config and cannot drift between them.

    The junit -> TESTCASE conversion is inlined as a heredoc rather than shipped
    as a separate helper file, keeping images/pr-<N>/ to the standard file set.
    The heredoc is quoted ('PYEOF') so the shell expands nothing inside it.
    """
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        "\n"
        f"cd /home/{repo}\n"
        f"{apply_line}"
        "\n"
        "# never inherit the previous stage's results\n"
        "rm -f /home/results.xml\n"
        "\n"
        "# -e is lifted ONLY around the test call: at the test stage the suite is\n"
        "# SUPPOSED to fail, and dying here would report zero tests - which\n"
        '# satisfies report.py\'s "fix something" check vacuously and manufactures\n'
        "# a false-positive valid instance.\n"
        "set +e\n"
        f"{TEST_CMD}\n"
        "RC=$?\n"
        "set -e\n"
        'echo "TEST_EXIT_CODE=$RC"\n'
        "\n"
        f'echo "{BEGIN_MARKER}"\n'
        "python - <<'PYEOF'\n"
        "import os\n"
        "import xml.etree.ElementTree as ET\n"
        "\n"
        'PATH = "/home/results.xml"\n'
        "\n"
        "if os.path.exists(PATH):\n"
        "    try:\n"
        "        root = ET.parse(PATH).getroot()\n"
        "    except ET.ParseError:\n"
        "        root = None\n"
        "\n"
        "    if root is not None:\n"
        '        for tc in root.iter("testcase"):\n'
        '            classname = tc.get("classname") or ""\n'
        '            path = tc.get("file")\n'
        "\n"
        "            if path:\n"
        "                # Emit a REAL pytest node id: <file>::<Class>::<name>.\n"
        "                # The class is the last dotted component of classname,\n"
        "                # but only when it IS a class - for a module-level test\n"
        "                # function pytest sets classname to the module's dotted\n"
        "                # path, whose last component is the module basename.\n"
        "                # Compare against the file to tell them apart rather\n"
        "                # than guessing from capitalisation.\n"
        "                module_base = os.path.basename(path)\n"
        '                if module_base.endswith(".py"):\n'
        "                    module_base = module_base[:-3]\n"
        '                tail = classname.rsplit(".", 1)[-1] if classname else ""\n'
        "                if not tail or tail == module_base:\n"
        "                    ident = path\n"
        "                else:\n"
        '                    ident = path + "::" + tail\n'
        "            else:\n"
        "                # xunit2 fallback: no `file` attribute, so the dotted\n"
        "                # classname is all there is. Still stable and unique\n"
        "                # across stages, just not re-runnable.\n"
        '                ident = classname.replace(".", "/") + ".py"\n'
        "\n"
        '            name = tc.get("name") or ""\n'
        "            # pytest escapes newlines inside parametrized ids, but\n"
        "            # guarantee it: a raw newline would split the TESTCASE line\n"
        "            # and corrupt the id.\n"
        '            name = name.replace("\\r", " ").replace("\\n", " ")\n'
        "\n"
        '            status = "PASSED"\n'
        "            for child in tc:\n"
        '                if child.tag in ("failure", "error"):\n'
        '                    status = "FAILED"\n'
        "                    break\n"
        '                if child.tag == "skipped":\n'
        '                    status = "SKIPPED"\n'
        "                    break\n"
        "\n"
        '            print("TESTCASE " + ident + "::" + name + " " + status)\n'
        "PYEOF\n"
        f'echo "{END_MARKER}"\n'
    )


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
        repo = self.pr.repo

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
                '  echo "check_git_changes: Not inside a git repository"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                "if [[ -n $(git status --porcelain) ]]; then\n"
                '  echo "check_git_changes: Uncommitted changes"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                'echo "check_git_changes: No uncommitted changes"\n'
                "exit 0\n",
            ),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "# NO git checkout and NO history stripping here - both belong to\n"
                "# the PR Dockerfile, which has already run them by the time this\n"
                "# script executes. This file does install + verification only.\n"
                f"cd /home/{repo}\n"
                "bash /home/check_git_changes.sh\n"
                "\n"
                "# --no-deps is load-bearing: the era-pinned runtime deps already\n"
                "# live in the shared base, and setup.cfg's install_requires is\n"
                "# unpinned, so letting pip resolve here would pull today's\n"
                "# releases over those pins and modernise a 2021/2022 tree.\n"
                "pip install --no-cache-dir --no-deps -e . || true\n"
                "\n"
                "# HARD GATE - the install above is tolerant (|| true) so a\n"
                "# packaging hiccup cannot fail the build on its own, which means\n"
                "# something must prove the tree is importable and runnable.\n"
                "# Without this a broken environment reaches the graded stages and\n"
                "# surfaces as an unexplained empty report instead of an honest\n"
                "# build failure.\n"
                "python --version\n"
                "git --no-pager log -1 --format='HEAD %H'\n"
                "python -c \"import holidays; print('holidays', holidays.__version__)\"\n"
                "python -c \"import dateutil, convertdate, hijri_converter, korean_lunar_calendar\"\n"
                "\n"
                "# The gate COLLECTS the real suite with the real flags rather\n"
                "# than calling `pytest --version`. Two reasons: it proves the\n"
                "# whole test tree imports, and --override-ini=addopts= must be\n"
                "# present for the same reason it is in TEST_CMD - tox.ini carries\n"
                "# `[pytest] addopts = --cov=...` which pytest applies to ANY bare\n"
                "# invocation, so without the override even `pytest --version`\n"
                "# dies with `UsageError: unrecognized arguments: --cov=./`\n"
                "# (exit 4) because pytest-cov is deliberately not installed.\n"
                "python -m pytest test/ --collect-only -q \\\n"
                "    --override-ini=addopts= -p no:cacheprovider \\\n"
                "    > /tmp/collect.txt 2>&1 || true\n"
                "tail -2 /tmp/collect.txt\n"
                "COLLECTED=$(grep -cE '^test/.*::' /tmp/collect.txt || true)\n"
                'if [ "${COLLECTED:-0}" -lt 100 ]; then\n'
                '    echo "FATAL: collected only ${COLLECTED:-0} tests" >&2\n'
                "    exit 1\n"
                "fi\n"
                'echo "COLLECTED=$COLLECTED"\n'
                'echo "DEPS_OK"\n',
            ),
            File(".", "run.sh", _stage_script(repo, "")),
            File(
                ".",
                "test-run.sh",
                _stage_script(
                    repo,
                    "if ! git apply --whitespace=nowarn /home/test.patch; then\n"
                    '    echo "Error: git apply (test.patch) failed" >&2\n'
                    "    exit 1\n"
                    "fi\n",
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _stage_script(
                    repo,
                    "if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then\n"
                    '    echo "Error: git apply (test.patch + fix.patch) failed" >&2\n'
                    "    exit 1\n"
                    "fi\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        """PR layer: inherits the clone from the shared base, then owns the
        per-PR checkout and the FULL git stripping / hardening block.

        There is deliberately no `git clone` here - the base already performed
        it, and repeating it would re-download the repository five times.

        The hardening block is reused verbatim from Image._HARDENING_BLOCK
        rather than reimplemented, so it cannot drift from the harness's own
        definition. Because dependency() returns an Image, enhance() returns
        this text untouched, so what is written here is what Docker receives.

        NO `CMD` here by rule - it belongs to the base Dockerfile only. Docker
        inherits CMD from the parent image, so this layer still starts
        /bin/bash; repeating it would just shadow the base's with an identical
        value. The harness never relies on it either: every stage is invoked
        explicitly as `bash /home/<stage>.sh`.
        """
        pr = self.pr
        base = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base.image_name()}:{base.image_tag()}

ARG BASE_COMMIT="{pr.base.sha}"

{copy_commands}
WORKDIR /home/{pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

RUN bash /home/prepare.sh
"""


@Instance.register("vacanza", "holidays")
class Holidays(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI before any matching.
        test_log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        # Greedy id capture with the status as the FINAL token: pytest
        # parametrized ids can embed spaces, so a non-greedy or \S+ capture
        # would silently truncate them.
        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in test_log.splitlines():
            stripped = line.strip()

            if stripped.startswith(BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(END_MARKER):
                in_detail = False
                continue
            # Everything outside the markers is raw pytest console output and is
            # ignored, so the "FAILED <id> - <error>" summary lines can never
            # pollute a test id.
            if not in_detail:
                continue

            match = case_re.match(stripped)
            if not match:
                continue

            name, status = match.group(1), match.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # Failure wins; each test lands in exactly one bucket.
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
