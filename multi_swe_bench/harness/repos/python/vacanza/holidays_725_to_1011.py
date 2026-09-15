import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_PY_IMAGE = "python:3.10-slim-bookworm"

_BASE_TAG = "base-725_to_1011"

_PINS = " ".join(
    [
        "python-dateutil==2.8.2",
        "six==1.16.0",
        "convertdate==2.4.0",
        "PyMeeus==0.5.12",
        "hijri-converter==2.2.4",
        "korean-lunar-calendar==0.3.1",
        "pytest==7.2.2",
        "attrs==22.2.0",
        "iniconfig==2.0.0",
        "packaging==23.0",
        "pluggy==1.0.0",
        "exceptiongroup==1.1.0",
        "tomli==2.0.1",
        "wheel==0.40.0",
    ]
)

_GATE_IMPORTS = (
    "holidays, pytest, dateutil, convertdate, pymeeus, hijri_converter, korean_lunar_calendar"
)

_CHECK_GIT_CHANGES = """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE = """\
#!/bin/bash
set -euo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

python -m pip install --no-cache-dir --retries 10 __PINS__
python -m pip check

if [ -f scripts/l10n/generate_mo_files.py ]; then
  python scripts/l10n/generate_mo_files.py
fi

if [ -d tests ]; then
  TEST_DIR=tests
else
  TEST_DIR=test
fi

python -m pytest "$TEST_DIR" --collect-only -q -p no:cacheprovider --override-ini=addopts= > /home/collect.log 2>&1 || { tail -n 80 /home/collect.log; exit 1; }
tail -n 3 /home/collect.log
grep -q "::" /home/collect.log

python -c "import __GATE_IMPORTS__; print('DEPS_OK', holidays.__version__)"
"""

_STAGE = """\
#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1

cd /home/__REPO__
__APPLY__
if [ -f scripts/l10n/generate_mo_files.py ]; then
  python scripts/l10n/generate_mo_files.py
fi

if [ -d tests ]; then
  TEST_DIR=tests
else
  TEST_DIR=test
fi

python -m pytest "$TEST_DIR" -v -rA --tb=no -p no:cacheprovider --override-ini=addopts= --continue-on-collection-errors
"""

_PRUNE = """\
RUN set -eux; \\
    cd /home/__REPO__; \\
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    rm -f .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git remote)"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \\
        cd /home/__REPO__ && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("__APPLY__", apply)
        .replace("__PINS__", _PINS)
        .replace("__GATE_IMPORTS__", _GATE_IMPORTS)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__REPO__", pr.repo)
    )


class HolidaysEra725To1011ImageBase(Image):
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
        return _PY_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = DockerfileEnhancer._infrastructure_block(self, _PY_IMAGE)
        repo = self.pr.repo
        return (
            f"{DockerfileEnhancer.SYNTAX_DIRECTIVE}\n\n"
            f"FROM {_PY_IMAGE}\n\n"
            f"{infra}\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        git ca-certificates build-essential \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
            "RUN git config --global --add safe.directory '*'\n\n"
            "WORKDIR /home/\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
            f"    cd /home/{repo} && git rev-parse HEAD >/dev/null\n\n"
            'CMD ["/bin/bash"]\n'
        )


class HolidaysEra725To1011ImageDefault(Image):
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
        return HolidaysEra725To1011ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", _render(_STAGE, self.pr)),
            File(
                ".",
                "test-run.sh",
                _render(
                    _STAGE,
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch\n",
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _render(
                    _STAGE,
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            f"{copy_commands}\n\n"
            "RUN bash /home/prepare.sh\n\n"
            f"{_render(_PRUNE, self.pr)}"
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_STATUSES = "PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"

_PROGRESS_RE = re.compile(
    rf"^(?P<name>\S+::\S+)\s+(?P<status>{_STATUSES})(?:\s+\(.*?\))?(?:\s+\[\s*\d+%\])?\s*$"
)

_SUMMARY_RE = re.compile(rf"^(?P<status>{_STATUSES})\s+(?P<name>\S+)(?:\s+-\s+.*)?$")


@Instance.register("vacanza", "holidays_725_to_1011")
class HOLIDAYS_725_TO_1011(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return HolidaysEra725To1011ImageDefault(self.pr, self._config)

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

        for raw_line in _ANSI_RE.sub("", test_log).split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            match = _PROGRESS_RE.match(line) or _SUMMARY_RE.match(line)
            if not match:
                continue
            name = match.group("name")
            if "::" not in name and not name.endswith(".py"):
                continue
            status = match.group("status")
            if status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

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
