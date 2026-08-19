import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OM1ImageBase(Image):
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

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        if self.config.need_clone:
            fetch_block = f"""RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}
{self.clear_env}
CMD ["/bin/bash"]"""
        else:
            fetch_block = f"{self.clear_env}\nCOPY {repo} /home/{repo}"

        return f"""FROM {self.dependency()}

{self.global_env}

WORKDIR /home/

{fetch_block}
"""


class OM1ImageDefault(Image):
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
        return OM1ImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8
export PIP_ROOT_USER_ACTION=ignore
export UV_LINK_MODE=copy

# Toolchain lives here, not in the base image, so the base Dockerfile keeps the
# canonical structure. Build tools for native wheels; portaudio19-dev +
# python3-pyaudio for pyaudio (mirrors .github/workflows/unitest.yml); libgl1 +
# libglib2.0-0 for opencv-python; libhidapi-* for hid.
apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential cmake pkg-config \\
    portaudio19-dev python3-pyaudio \\
    libgl1 libglib2.0-0 libhidapi-hidraw0 libhidapi-libusb0
rm -rf /var/lib/apt/lists/*
git config --global --add safe.directory '*'
pip install --no-cache-dir --upgrade pip uv

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# The base image already cloned the repo, detached HEAD at the base commit and
# stripped every other ref/remote, so there is nothing left to fetch or check
# out here -- assert the tree is the expected commit and move on.
test "$(git rev-parse HEAD)" = "{pr.base.sha}"

# Build-time install (R16): resolve uv.lock into /home/{pr.repo}/.venv so every
# stage runs offline. --extra dds is deliberately omitted -- it needs a
# from-source CycloneDDS build and no collected test imports it.
uv sync || true
uv pip install pytest pytest-asyncio || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
.venv/bin/python -m pytest -v -rA --continue-on-collection-errors -p no:cacheprovider

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
.venv/bin/python -m pytest -v -rA --continue-on-collection-errors -p no:cacheprovider

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
.venv/bin/python -m pytest -v -rA --continue-on-collection-errors -p no:cacheprovider

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}
{prepare_commands}
"""


@Instance.register("OpenMind", "OM1")
class OM1(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OM1ImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        progress_pattern = re.compile(
            r"^(.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\(.*\))?\s+\[\s*\d+%\s*\]\s*$",
            re.MULTILINE,
        )

        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
            r"([\w./\\-]+\.py(?:::\S+)?)\s*(?:-.*)?$",
            re.MULTILINE,
        )

        for name, status in progress_pattern.findall(clean_log):
            name = name.strip()
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        for status, name in summary_pattern.findall(clean_log):
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
