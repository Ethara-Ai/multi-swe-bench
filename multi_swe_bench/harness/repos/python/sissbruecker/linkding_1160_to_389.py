import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_django_test_log(log: str) -> TestResult:
    """Parse Django test runner output (`manage.py test --verbosity=2`).

    Verbose result lines look like (Django <4.1):
        test_x (bookmarks.tests.test_mod.SomeTest) ... ok
    or (Django >=5.0, method repeated in the dotted path):
        test_x (bookmarks.tests.test_mod.SomeTest.test_x) ... ok

    Test names are normalised to the full `module.Class.method` id so they are
    stable across the run/test/fix stages regardless of the Django version."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_line = re.compile(
        r"^(test\w*)\s+\(([\w.]+)\)(?:\s+\([^)]*\))?\s+\.\.\.\s+"
        r"(ok|FAIL|ERROR|skipped|expected failure|unexpected success)"
    )
    re_summary = re.compile(r"^(?:FAIL|ERROR):\s+(test\w*)\s+\(([\w.]+)\)")

    def norm(method: str, path: str) -> str:
        return path if path.endswith("." + method) else f"{path}.{method}"

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).strip()
        m = re_line.match(line)
        if m:
            name = norm(m.group(1), m.group(2))
            status = m.group(3)
            if status == "ok":
                passed_tests.add(name)
            elif status in ("FAIL", "ERROR", "unexpected success"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)
            continue
        m = re_summary.match(line)
        if m:
            failed_tests.add(norm(m.group(1), m.group(2)))

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


class LinkdingEraBImageBase(Image):
    """linkding era B (PRs 389-1160, v1.20->1.43): deps via `requirements.txt`
    plus `requirements.dev.txt` (compiled with pip-tools); the dev set carries
    django-debug-toolbar which the dev settings import. Python 3.12."""

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
        return "python:3.12-slim"

    def image_tag(self) -> str:
        return "base-py312"

    def workdir(self) -> str:
        return "base-py312"

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
    git build-essential && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}
"""


class LinkdingEraBImageDefault(Image):
    """Per-PR image: checkout base commit, install requirements (+dev),
    run the targeted Django unit tests."""

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
        return LinkdingEraBImageBase(self.pr, self._config)

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
pip install --no-cache-dir -r requirements.txt || true
[ -f requirements.dev.txt ] && pip install --no-cache-dir -r requirements.dev.txt || true
mkdir -p data
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
mkdir -p data
TEST_FILES=$({{ grep -E '^diff --git a/bookmarks/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py' | sort -u; }} || true)
LABELS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then LABELS="$LABELS $(echo "${{f%.py}}" | tr '/' '.')"; fi
done
if [ -z "$LABELS" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python manage.py test $LABELS --verbosity=2 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
mkdir -p data
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.sqlite' --exclude='*.woff*' \
    --exclude='*.afdesign' --exclude='*.shortcut')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/requirements' /home/test.patch 2>/dev/null; then
    pip install --no-cache-dir -r requirements.txt || true
    [ -f requirements.dev.txt ] && pip install --no-cache-dir -r requirements.dev.txt || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/bookmarks/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py' | sort -u; }} || true)
LABELS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then LABELS="$LABELS $(echo "${{f%.py}}" | tr '/' '.')"; fi
done
if [ -z "$LABELS" ]; then echo "NO_TEST_FILES"; exit 0; fi
python manage.py test $LABELS --verbosity=2 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
mkdir -p data
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.sqlite' --exclude='*.woff*' \
    --exclude='*.afdesign' --exclude='*.shortcut')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/requirements' /home/test.patch /home/fix.patch 2>/dev/null; then
    pip install --no-cache-dir -r requirements.txt || true
    [ -f requirements.dev.txt ] && pip install --no-cache-dir -r requirements.dev.txt || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/bookmarks/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py' | sort -u; }} || true)
LABELS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then LABELS="$LABELS $(echo "${{f%.py}}" | tr '/' '.')"; fi
done
if [ -z "$LABELS" ]; then echo "NO_TEST_FILES"; exit 0; fi
python manage.py test $LABELS --verbosity=2 2>&1
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


@Instance.register("sissbruecker", "linkding_1160_to_389")
class LINKDING_1160_TO_389(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LinkdingEraBImageDefault(self.pr, self._config)

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
        return parse_django_test_log(log)
