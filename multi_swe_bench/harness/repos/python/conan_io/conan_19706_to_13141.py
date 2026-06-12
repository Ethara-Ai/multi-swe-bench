import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v output anchored on the trailing `<STATUS> [ NN%]` so
    parametrized node ids with internal spaces are captured whole."""
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


class ConanV2ImageBase(Image):
    """conan era 2 — v2.x (PRs 13141-19706, releases 2.0->2.28, 2023+).
    Package dir `conan/` + `conans/` shim; tests live under top-level
    `test/{integration,functional,unittests}/` plus legacy `conans/test/...`.
    The grep matches any path under `test/` or `conans/test/`. Built with
    Python 3.10."""

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
        return "python:3.10-slim"

    def image_tag(self) -> str:
        return "base-v2"

    def workdir(self) -> str:
        return "base-v2"

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

# Ensure pytest is always present even if `pip install -e .[test]` falls back
# to `-e .` (which lacks test extras in some PR commits).
RUN pip install --no-cache-dir pytest PyJWT

{code}

{self.clear_env}
"""


class ConanV2ImageDefault(Image):
    """Per-PR image: checkout base commit, install conan in editable mode
    (with test extras), run the targeted pytest unit tests."""

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
        return ConanV2ImageBase(self.pr, self._config)

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
pip install --no-cache-dir -e .[test] || pip install --no-cache-dir -e . || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# pytest file paths the PR's test patch touches. v2 layout uses
# test/integration/, test/functional/, test/unittests/ plus legacy
# conans/test/... — grep matches any path under test/ or conans/test/.
# Exclude __init__/helpers/conftest — they only get touched as fixtures.
TEST_FILES=$({{ grep -E '^diff --git a/(conans/test|test)/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py|/conftest\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/(setup\\.py|pyproject\\.toml|requirements)' /home/test.patch 2>/dev/null; then
    pip install --no-cache-dir -e .[test] || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/(conans/test|test)/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py|/conftest\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(setup\\.py|pyproject\\.toml|requirements)' /home/test.patch /home/fix.patch 2>/dev/null; then
    pip install --no-cache-dir -e .[test] || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/(conans/test|test)/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py|/conftest\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \
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


@Instance.register("conan-io", "conan_19706_to_13141")
class CONAN_19706_TO_13141(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ConanV2ImageDefault(self.pr, self._config)

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
