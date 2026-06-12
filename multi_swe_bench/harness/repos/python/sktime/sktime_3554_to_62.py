import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v output anchored on the trailing `<STATUS> [ NN%]` so
    parametrized node ids with internal spaces/brackets are captured whole."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_line = re.compile(
        r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\]\s*$"
    )

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).strip()
        m = re_line.match(line)
        if not m:
            continue
        nodeid, status = m.group(1).strip(), m.group(2)
        if status in ("PASSED", "XPASS"):
            passed_tests.add(nodeid)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(nodeid)
        else:
            skipped_tests.add(nodeid)

    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class SktimePy39ImageBase(Image):
    """sktime era 1 (PRs 62-3554 with requires-python `<3.10`/`<3.11` or
    early/unspecified; releases 0.2->0.16, 2019-2023). Python 3.9 covers
    `<3.10` and `<3.11` constraints and runs early `classifier:3.6/3.7`
    code without forced toolchain. Routing is by python_requires at the
    PR's base SHA, not PR# (sktime maintains parallel release branches
    with backports — PR# is non-monotonic with python_requires)."""

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
        return "python:3.9-slim"

    def image_tag(self) -> str:
        return "base-py39"

    def workdir(self) -> str:
        return "base-py39"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential curl && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}
"""


class SktimePy39ImageDefault(Image):
    """Per-PR image: checkout base commit, install sktime + dev extras
    (pytest comes from [dev] in all sktime versions), run targeted pytest."""

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
        return SktimePy39ImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
# Pre-install build deps required by old sktime setup.py before metadata resolution.
# Very old PRs (<=~1600) use setup.py that imports numpy/cython at metadata time.
pip install --no-cache-dir numpy cython 2>&1 | tail -3 || true
# Install with extras priority: [dev] (pytest in all eras) → [tests] → bare.
# Wrap in timeout 600 per [[wrap-install-in-timeout]] — pip resolver hangs are real.
timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -5 \\
    || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -5 \\
    || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -5 || true
# For very old sktime (0.4-0.7, ~2020): setup.py uses .* version specifiers that
# modern pip rejects, so [dev] silently fails leaving pandas/sklearn uninstalled.
# Only install historical pins if pandas is missing — safe for newer era1 PRs.
python -c "import pandas" 2>/dev/null || \\
    pip install --no-cache-dir 'numpy<1.24' 'pandas<2' 'scikit-learn<1.1' \\
        'scipy<1.10' 'statsmodels' 2>&1 | tail -3 || true
# Ensure pytest is present. Use >/dev/null not | head -1: pipe exits 0 even on failure.
python -m pytest --version >/dev/null 2>&1 || pip install --no-cache-dir pytest pytest-xdist || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# Pytest files the PR's test patch touches under sktime/. The grep is anchored
# on `sktime/.+_test\\.py` and `sktime/.+/tests/.+\\.py` to match sktime's
# two test conventions; skip __init__.py.
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.npy' --exclude='*.npz' --exclude='*.parquet' --exclude='*.pkl' \\
    --exclude='*.joblib' --exclude='*.h5' --exclude='*.hdf5' --exclude='*.arff' \\
    --exclude='*.tsv' --exclude='*.tsf' --exclude='*.tar.gz' --exclude='*.xlsx' \\
    --exclude='*.mat' --exclude='*.xls' --exclude='*.nc')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
# Reinstall if patch touches deps — 80/81 sktime PRs do this.
if grep -qE '^diff --git a/(setup\\.py|pyproject\\.toml|setup\\.cfg|requirements)' /home/test.patch 2>/dev/null; then
    timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -3 || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.npy' --exclude='*.npz' --exclude='*.parquet' --exclude='*.pkl' \\
    --exclude='*.joblib' --exclude='*.h5' --exclude='*.hdf5' --exclude='*.arff' \\
    --exclude='*.tsv' --exclude='*.tsf' --exclude='*.tar.gz' --exclude='*.xlsx' \\
    --exclude='*.mat' --exclude='*.xls' --exclude='*.nc')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(setup\\.py|pyproject\\.toml|setup\\.cfg|requirements)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -3 || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
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


@Instance.register("sktime", "sktime_3554_to_62")
class SKTIME_3554_TO_62(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SktimePy39ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_pytest_log(log)
