from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        return "python:3.10-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-py310-changedetectionio"

    def workdir(self) -> str:
        return "base-py310-changedetectionio"

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

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git bash gawk && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


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

    def image_prefix(self) -> str:
        return "envagent"

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
                "filter_patch.sh",
                """#!/bin/bash
# Usage: filter_patch.sh <patch-file>  -> prints a patch with binary-only
# diff sections removed so `git apply` does not abort on them.
awk '
/^diff --git / { if (blk != "" && !bin) printf "%s", blk; blk = $0 ORS; bin = 0; next }
{ if (blk != "") { blk = blk $0 ORS } else { printf "%s\\n", $0 } ; if ($0 ~ /^Binary files .* differ$/) bin = 1 }
END { if (blk != "" && !bin) printf "%s", blk }
' "$1"
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
git checkout {pr.base.sha}

pip install --root-user-action=ignore --no-cache-dir -r requirements.txt || true
pip install --root-user-action=ignore --no-cache-dir -e . || true
pip install --root-user-action=ignore --no-cache-dir pytest pytest-flask || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# Each test file is run in its OWN pytest process. changedetection.io's
# tests are not import-isolated (the Flask app/blueprints register
# globally), so running the whole suite in one process collides with
# "View function mapping is overwriting an existing endpoint" errors.
cd /home/{pr.repo}/changedetectionio
for f in tests/test_*.py; do
    [ -e "$f" ] || continue
    echo "===== PYTEST FILE: $f ====="
    python -m pytest "$f" -p no:cacheprovider -rA --tb=no -o log_cli=false --continue-on-collection-errors || true
done
if [ -d tests/unit ]; then
    echo "===== PYTEST FILE: tests/unit ====="
    python -m pytest tests/unit -p no:cacheprovider -rA --tb=no -o log_cli=false --continue-on-collection-errors || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
bash /home/filter_patch.sh /home/test.patch > /home/test.applied.patch
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.applied.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Re-install dependencies from the patched working tree so deps newly
# introduced by the patch are present before the tests run.
cd /home/{pr.repo}
pip install --root-user-action=ignore --no-cache-dir -r requirements.txt || true
if [ -f requirements-dev.txt ]; then
    pip install --root-user-action=ignore --no-cache-dir -r requirements-dev.txt || true
fi
pip install --root-user-action=ignore --no-cache-dir -e . || true
pip install --root-user-action=ignore --no-cache-dir -U typing_extensions || true
cd /home/{pr.repo}/changedetectionio
for f in tests/test_*.py; do
    [ -e "$f" ] || continue
    echo "===== PYTEST FILE: $f ====="
    python -m pytest "$f" -p no:cacheprovider -rA --tb=no -o log_cli=false --continue-on-collection-errors || true
done
if [ -d tests/unit ]; then
    echo "===== PYTEST FILE: tests/unit ====="
    python -m pytest tests/unit -p no:cacheprovider -rA --tb=no -o log_cli=false --continue-on-collection-errors || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
bash /home/filter_patch.sh /home/test.patch > /home/test.applied.patch
bash /home/filter_patch.sh /home/fix.patch > /home/fix.applied.patch
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.applied.patch /home/fix.applied.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Re-install dependencies from the patched working tree so deps newly
# introduced by the fix patch (e.g. janus, flask_babel) are present.
cd /home/{pr.repo}
pip install --root-user-action=ignore --no-cache-dir -r requirements.txt || true
if [ -f requirements-dev.txt ]; then
    pip install --root-user-action=ignore --no-cache-dir -r requirements-dev.txt || true
fi
pip install --root-user-action=ignore --no-cache-dir -e . || true
pip install --root-user-action=ignore --no-cache-dir -U typing_extensions || true
cd /home/{pr.repo}/changedetectionio
for f in tests/test_*.py; do
    [ -e "$f" ] || continue
    echo "===== PYTEST FILE: $f ====="
    python -m pytest "$f" -p no:cacheprovider -rA --tb=no -o log_cli=false --continue-on-collection-errors || true
done
if [ -d tests/unit ]; then
    echo "===== PYTEST FILE: tests/unit ====="
    python -m pytest tests/unit -p no:cacheprovider -rA --tb=no -o log_cli=false --continue-on-collection-errors || true
fi
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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest ``-rA`` short-summary output for changedetection.io.

    Summary lines look like:
      PASSED tests/test_jinja2.py::test_format
      FAILED tests/test_pdf.py::test_fetch_pdf - IndexError: list index out of range
      SKIPPED [1] tests/test_x.py:42: reason
      ERROR tests/apprise/test_apprise_asset.py - ImportError ...
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    skipped_pattern = re.compile(r"^SKIPPED \[\d+\]\s+(\S+?):\d+:")

    for raw in log.splitlines():
        line = raw.strip()
        if line.startswith("PASSED ") or line.startswith("XPASS "):
            parts = line.split(None, 2)
            if len(parts) >= 2:
                passed_tests.add(parts[1])
        elif line.startswith("FAILED "):
            parts = line.split(None, 2)
            if len(parts) >= 2:
                failed_tests.add(parts[1])
        elif line.startswith("ERROR "):
            parts = line.split(None, 2)
            # Only treat node-level errors as failures; file-level
            # collection errors are environment noise.
            if len(parts) >= 2 and "::" in parts[1]:
                failed_tests.add(parts[1])
        elif line.startswith("SKIPPED "):
            match = skipped_pattern.match(line)
            if match:
                skipped_tests.add(match.group(1))
        elif line.startswith("XFAIL "):
            parts = line.split(None, 2)
            if len(parts) >= 2:
                skipped_tests.add(parts[1])

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("dgtlmoon", "changedetection.io_1400_to_2429")
class CHANGEDETECTION_IO_1400_TO_2429(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        return parse_pytest_log(log)
