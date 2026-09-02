from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

TEST_CMD = (
    'python -m pytest tests/ -v --tb=short --override-ini="addopts=" '
    "-p no:cacheprovider --continue-on-collection-errors --color=no "
    "--timeout=1800"
)

BASE_IMAGE = "python:3.9-slim"

TOOLCHAIN_SETUP = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel
"""


SALVAGE_SCRIPT = r"""import ast
import os
import re
import sys

stage_log, repo, baseline_path = sys.argv[1], sys.argv[2], sys.argv[3]

if not os.path.exists(stage_log):
    sys.exit(0)

log = open(stage_log, encoding="utf-8", errors="replace").read()

blocked = []
for match in re.finditer(r"^ERROR\s+(tests/\S+\.py)", log, re.M):
    if match.group(1) not in blocked:
        blocked.append(match.group(1))

if not blocked:
    sys.exit(0)

baseline = []
if os.path.exists(baseline_path):
    baseline = [
        line.strip()
        for line in open(baseline_path, encoding="utf-8", errors="replace")
        if line.strip()
    ]

already = set()
for match in re.finditer(
    r"^(tests/.+?::.+?)\s+(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\[\s*\d+%\])?\s*$",
    log,
    re.M,
):
    already.add(match.group(1))


def strip_params(test_id):
    return re.sub(r"\[.*\]$", "", test_id)


recovered = []
for module in blocked:
    prefix = module + "::"
    from_baseline = [t for t in baseline if t.startswith(prefix)]
    seen = set(strip_params(t) for t in from_baseline)
    recovered.extend(from_baseline)

    source_path = os.path.join(repo, module)
    if not os.path.exists(source_path):
        continue
    try:
        tree = ast.parse(
            open(source_path, encoding="utf-8", errors="replace").read()
        )
    except SyntaxError:
        continue

    stack = [(tree, "")]
    while stack:
        node, scope = stack.pop()
        for child in getattr(node, "body", []):
            if isinstance(child, ast.ClassDef):
                stack.append((child, scope + child.name + "::"))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test"):
                    test_id = module + "::" + scope + child.name
                    if test_id not in seen:
                        seen.add(test_id)
                        recovered.append(test_id)

for test_id in recovered:
    if test_id not in already:
        print(test_id + " FAILED")
"""


class LyzCodeYamlfixImageBase(Image):

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

        return (
            f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

"""
            + TOOLCHAIN_SETUP
            + f"""
{code}

{self.clear_env}

"""
        )


class LyzCodeYamlfixImageDefault(Image):

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
        return LyzCodeYamlfixImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

if ! git rev-parse --verify -q {pr.base.sha}^{{commit}} > /dev/null; then
  git remote add origin https://github.com/{pr.org}/{pr.repo}.git 2>/dev/null || true
  git fetch --depth=1 origin {pr.base.sha} 2>/dev/null || git fetch origin 2>/dev/null || true
  git remote remove origin 2>/dev/null || true
fi
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

pip install --no-cache-dir "pdm-pep517<2" || true

pip install --no-cache-dir \\
    "click==8.1.3" \\
    "ruyaml==0.91.0" \\
    "maison==1.4.0" \\
    "pydantic==1.10.7" || true

pip install --no-cache-dir --no-deps --no-build-isolation -e . || true

pip install --no-cache-dir "pytest==7.2.2" "pytest-timeout==2.1.0" "py==1.11.0" || true

python -c "import yamlfix, click, ruyaml, pytest, py._path.local"

git checkout -- .
bash /home/check_git_changes.sh

python -m pytest tests/ --collect-only -q --override-ini="addopts=" -p no:cacheprovider --color=no 2>/dev/null | grep -E '^tests/.+::' > /home/baseline_tests.txt || true

if [ "$(uname -m)" = "x86_64" ]; then
  {test_cmd} || true
else
  echo "prepare.sh: $(uname -m) is not the grading architecture -- skipping the"
  echo "prepare.sh: test warm-up."
fi

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
set +e
{test_cmd} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
python /home/salvage_blocked_tests.py /home/stage.log /home/{pr.repo} /home/baseline_tests.txt
exit $rc

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
set +e
{test_cmd} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
python /home/salvage_blocked_tests.py /home/stage.log /home/{pr.repo} /home/baseline_tests.txt
exit $rc

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "salvage_blocked_tests.py",
                SALVAGE_SCRIPT,
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


_RE_INLINE = re.compile(
    r"^(tests/.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\[\s*\d+%\])?\s*$"
)
_RE_SUMMARY = re.compile(r"^(?:FAILED|ERROR)\s+(tests/\S+?::\S+?)(?:\s|$)")
_RE_COLLECT_ERROR = re.compile(r"^ERROR\s+(tests/\S+\.py)\s*$")

_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

KNOWN_FLAKY_TESTS: frozenset[str] = frozenset()


def parse_pytest_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(status: str, test_id: str) -> None:
        if test_id in KNOWN_FLAKY_TESTS:
            return
        if status in ("PASSED", "XPASS"):
            if test_id in failed_tests:
                return
            skipped_tests.discard(test_id)
            passed_tests.add(test_id)
        elif status in ("FAILED", "ERROR"):
            passed_tests.discard(test_id)
            skipped_tests.discard(test_id)
            failed_tests.add(test_id)
        elif status in ("SKIPPED", "XFAIL"):
            if test_id not in passed_tests and test_id not in failed_tests:
                skipped_tests.add(test_id)

    for line in _RE_ANSI.sub("", log).splitlines():
        line = line.rstrip()

        match = _RE_INLINE.match(line)
        if match:
            record(match.group(2), match.group(1))
            continue

        match = _RE_SUMMARY.match(line)
        if match:
            record("FAILED", match.group(1))
            continue

        match = _RE_COLLECT_ERROR.match(line)
        if match:
            record("FAILED", match.group(1))
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("lyz-code", "yamlfix")
class LyzCodeYamlfix(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LyzCodeYamlfixImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_pytest_log(test_log)


_PR_NUMBERS = (182, 203, 215, 220, 244)

for _n in _PR_NUMBERS:
    Instance.register("lyz-code", str(_n))(LyzCodeYamlfix)
