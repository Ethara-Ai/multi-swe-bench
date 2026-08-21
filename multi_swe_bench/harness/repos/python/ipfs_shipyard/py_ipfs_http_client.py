from __future__ import annotations

import re
from typing import Optional

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

    def dependency(self) -> str | Image:
        # The project at this commit still targets the 2.7 - 3.8 range (it uses
        # `six` throughout), so 3.8 is the newest interpreter it supports.
        return "python:3.8-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-py38-py-ipfs-http-client"

    def workdir(self) -> str:
        return "base-py38-py-ipfs-http-client"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # The `ENV PIP_*` block below keeps pip's output pure ASCII, and is load
        # bearing: pip renders its progress bar with U+2501 (UTF-8 `e2 94 81`),
        # and the harness streams buildx output through
        # `safe_popen(..., text=True)` without an explicit encoding
        # (utils/docker_util.py), so on a cp1252 host the `0x81` byte aborts the
        # build with "'charmap' codec can't decode byte 0x81". ImageDefault
        # inherits it, which is what keeps `prepare.sh`'s pip calls safe.
        # Do not drop it without first fixing docker_util.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV PIP_PROGRESS_BAR=off \\
    PIP_NO_COLOR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

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
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

python -m pip install --upgrade pip "setuptools<60" wheel || true

# Test dependencies, mirroring the `[testenv]` deps pinned in the repo's tox.ini.
# `pathlib` is deliberately skipped: it is only needed by test/run-tests.py (which
# drives a real go-ipfs daemon) and the PyPI backport shadows the stdlib on py3.
python -m pip install \\
    "pytest ~= 4.6" \\
    "pytest-cov ~= 2.6" \\
    "pytest-localserver ~= 0.5" \\
    "pytest-mock ~= 1.10" \\
    "pytest-ordering ~= 0.6" || true

# Runtime dependencies, from [tool.flit.metadata] requires in pyproject.toml.
# The package itself is intentionally NOT installed: `python -m pytest` puts the
# working tree first on sys.path, so the unit tests always exercise the patched
# checkout rather than a stale copy in site-packages.
python -m pip install \\
    "multiaddr >= 0.0.7" \\
    "requests >= 2.11" \\
    six || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
python -m pytest test/unit -v --tb=short --override-ini="addopts=" -p no:cacheprovider

""".format(pr=self.pr),
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
python -m pytest test/unit -v --tb=short --override-ini="addopts=" -p no:cacheprovider

""".format(pr=self.pr),
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
python -m pytest test/unit -v --tb=short --override-ini="addopts=" -p no:cacheprovider

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
    log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Verbose progress lines: "test/unit/test_http.py::test_basic_auth PASSED  [ 42%]"
    pattern_status_after = re.compile(
        r"^(test/unit/[^\s:]+\.py)::(\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    )
    # Short summary lines: "FAILED test/unit/test_http.py::test_basic_auth - AssertionError: ..."
    # Capture stops at whitespace so the trailing reason is not folded into the name.
    pattern_status_before = re.compile(
        r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(test/unit/[^\s:]+\.py)::(\S+)"
    )

    def record(status: str, name: str) -> None:
        if status in ("PASSED", "XFAIL", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        elif status == "SKIPPED":
            skipped_tests.add(name)

    for line in log.splitlines():
        line = line.strip()

        m = pattern_status_after.match(line)
        if m:
            record(m.group(3), f"{m.group(1)}::{m.group(2)}")
            continue

        m = pattern_status_before.match(line)
        if m:
            record(m.group(1), f"{m.group(2)}::{m.group(3)}")

    # Enforce disjoint-set invariant: failed wins over passed, skipped wins over
    # passed, failed wins over skipped.
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


@Instance.register("ipfs-shipyard", "py-ipfs-http-client")
class PY_IPFS_HTTP_CLIENT(Instance):
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
