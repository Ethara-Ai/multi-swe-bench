import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTHON_IMAGE = "python:3.13-bookworm"
BASE_TAG = "base"
RESULTS_MARKER = "----- per-test results -----"

_TEST_FILES = re.compile(r"^diff --git a/(\S+/(?:test_\S+|\S+_test)\.py) b/", re.M)
_PASSED = re.compile(r"^(.+?)\s+PASSED$", re.M)
_FAILED = re.compile(r"^(.+?)\s+FAILED$", re.M)
_SKIPPED = re.compile(r"^(.+?)\s+SKIPPED$", re.M)
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _targets(pr: PullRequest) -> str:
    dirs = {f.rsplit("/", 1)[0] for f in _TEST_FILES.findall(pr.test_patch)}
    return " ".join(sorted(dirs))


def _run_tests_sh(pr: PullRequest) -> str:
    return (
        "#!/bin/bash\n"
        f"cd /home/{pr.repo}\n"
        "export PIP_FIND_LINKS=/tmp/msb-wheelhouse\n"
        "RESULTS=/tmp/msb-results.txt\n"
        ': > "$RESULTS"\n'
        f'TARGETS="{_targets(pr)}"\n'
        "SUITES=$(find $TARGETS -type f \\( -name 'test_*.py' -o -name '*_test.py' \\)"
        " 2>/dev/null | sort -u)\n"
        "for suite in $SUITES; do\n"
        '  out="/tmp/msb-$(echo "$suite" | tr / _).xml"\n'
        '  rm -f "$out"\n'
        '  log="/tmp/msb-$(echo "$suite" | tr / _).log"\n'
        '  python -m pytest "$suite" -p no:cacheprovider --junitxml="$out" -q'
        ' > "$log" 2>&1 < /dev/null\n'
        '  echo "----- pytest output: $suite -----"\n'
        '  tail -40 "$log"\n'
        '  SUITE="$suite" OUT="$out" python >> "$RESULTS" <<\'PY\'\n'
        "import os\n"
        "import xml.etree.ElementTree as ET\n"
        "suite = os.environ['SUITE']\n"
        "module = suite[:-3].replace('/', '.')\n"
        "lines = []\n"
        "root = None\n"
        "try:\n"
        "    root = ET.parse(os.environ['OUT']).getroot()\n"
        "except Exception:\n"
        "    pass\n"
        "for case in [] if root is None else root.iter('testcase'):\n"
        "    name = case.get('name') or ''\n"
        "    classname = case.get('classname') or ''\n"
        "    extra = classname[len(module):].lstrip('.')\n"
        "    if not classname.startswith(module):\n"
        "        extra = classname\n"
        "    prefix = extra + '::' if extra else ''\n"
        "    status = 'PASSED'\n"
        "    if case.find('skipped') is not None:\n"
        "        status = 'SKIPPED'\n"
        "    if case.find('failure') is not None or case.find('error') is not None:\n"
        "        status = 'FAILED'\n"
        "    lines.append(suite + '::' + prefix + name + ' ' + status)\n"
        "missing = [suite + '::SUITE_ERROR FAILED'][: 0 if lines else 1]\n"
        "print('\\n'.join(lines + missing))\n"
        "PY\n"
        "done\n"
        f'echo "{RESULTS_MARKER}"\n'
        'cat "$RESULTS"\n'
    )


class TimezonefinderImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        infra = DockerfileEnhancer._infrastructure_block(self, base_img).rstrip("\n")
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_img}

{infra}

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_INPUT=1
ENV PIP_ROOT_USER_ACTION=ignore
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV NO_COLOR=1
ENV CI=true

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class TimezonefinderImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return TimezonefinderImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "git rev-parse --is-inside-work-tree > /dev/null\n"
            "git status --porcelain\n"
            'test -z "$(git status --porcelain)"\n'
            'echo "check_git_changes: No uncommitted changes"\n'
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "python -m pip config set global.retries 10\n"
            "python -m pip install --upgrade pip setuptools wheel\n"
            "python -m pip install uv pytest pytz\n"
            "python -m pip install -e .\n"
            "python -m pip download --dest /tmp/msb-wheelhouse"
            " pip setuptools wheel numpy 'h3>4' cffi flatbuffers\n"
            "bash /home/check_git_changes.sh\n"
            "python -c \"import numpy, h3, cffi, flatbuffers, pytest, pytz;"
            " print('DEPS_OK')\"\n"
            "uv --version\n"
            "timezonefinder --help > /dev/null\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "bash /home/run_tests.sh\n"
        )

        test_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            "bash /home/run_tests.sh\n"
        )

        fix_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            "bash /home/run_tests.sh\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
            File(".", "run_tests.sh", _run_tests_sh(self.pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {image.image_full_name()}

{copies}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("jannikmi", "timezonefinder")
class Timezonefinder(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TimezonefinderImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        section = _ANSI.sub("", test_log).rsplit(RESULTS_MARKER, 1)[-1]
        failed_tests = set(_FAILED.findall(section))
        passed_tests = set(_PASSED.findall(section)) - failed_tests
        skipped_tests = set(_SKIPPED.findall(section)) - failed_tests - passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
