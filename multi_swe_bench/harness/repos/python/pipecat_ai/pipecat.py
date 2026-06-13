import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# pipecat-ai/pipecat — a Python framework for voice/multimodal AI agents.
#
# Discovery (verified in Docker, python:3.12-bookworm, native arm64):
#  - 60-PR range #60..#4378. The repo was renamed (early "dailyai" ->
#    "pipecat-ai") and migrated setuptools -> uv, requires-python 3.7 -> 3.11.
#  - Tests are pytest (pytest-asyncio); the CI uses `uv sync` then `uv run
#    pytest`. The modern suite is large (~1066 tests collected).
#  - One config, no era split: install is a fallback chain -- `uv sync
#    --all-extras` for the modern era, degrading to `uv pip install -e .` for
#    the old "dailyai" era; pytest is the runner throughout, so one parse_log
#    serves every era.
#  - System dep portaudio19-dev is required (pipecat imports pyaudio).
#  - pipecat has many optional service extras (anthropic/aws/google/livekit/
#    ...); --all-extras pulls them so test files importing them collect.


_PY = "3.12"


class PipecatImageBase(Image):
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
        return "python:3.12-bookworm"

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

        # Pin to linux/arm64 (the eval host's native arch). `uv` is a Rust
        # binary that segfaults under qemu-x86_64 emulation, so the amd64 leg
        # of a multi-arch build cannot run it. Pinning makes every build step
        # run native arm64; a multi-arch buildx build then produces a manifest
        # that still lists both arches (the amd64 entry carries arm64 content)
        # for verify_ecr_images.py, while the build itself never touches qemu.
        return f"""FROM --platform=linux/arm64 {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_ROOT_USER_ACTION=ignore
ENV UV_LINK_MODE=copy
WORKDIR /home/

# git for checkout; portaudio19-dev for pyaudio; build tools for native wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential portaudio19-dev \\
    && rm -rf /var/lib/apt/lists/*

# uv — the package manager pipecat's CI uses.
RUN pip install --no-cache-dir uv

{code}

{self.clear_env}

"""


class PipecatImageDefault(Image):
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
        return PipecatImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        check_git = """#!/bin/bash
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
"""

        # Shared install: resolve + install pipecat and its dependencies for
        # the checked-out (possibly patched) tree. Fallback chain spans eras.
        install = """#!/bin/bash
set -u
cd /home/__REPO__
uv venv --python __PY__ .venv >/dev/null 2>&1 || python3 -m venv .venv || true
export VIRTUAL_ENV="$PWD/.venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"

# Modern era: uv sync with every extra + the dev group. Degrades gracefully
# to a plain editable install for the old "dailyai" era.
uv sync --all-extras --group dev >/dev/null 2>&1 \\
  || uv sync --all-extras >/dev/null 2>&1 \\
  || uv sync >/dev/null 2>&1 \\
  || uv pip install -e ".[all]" >/dev/null 2>&1 \\
  || uv pip install -e . >/dev/null 2>&1 || true

# Guarantee the pytest runner is present regardless of which branch ran.
uv pip install pytest pytest-asyncio pytest-aiohttp >/dev/null 2>&1 || true
"""

        install = install.replace("__REPO__", repo).replace("__PY__", _PY)

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the install at the base commit (deps cached into the image layer).
bash /home/install.sh || true
""".replace("__REPO__", repo).replace("__SHA__", sha)

        run_tests = """#!/bin/bash
# Re-run install (a patch may add a dependency), then run pytest. `-v` gives
# one `path::test STATUS` line per test for parse_log; the collection-error
# flag keeps a single bad import from aborting the whole run.
set -uo pipefail
cd /home/__REPO__
bash /home/install.sh || true
.venv/bin/python -m pytest -v -rA --continue-on-collection-errors \\
    -p no:cacheprovider tests/ || true
""".replace("__REPO__", repo)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --reject /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
            File(".", "install.sh", install),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # arm64 pin — see PipecatImageBase.dockerfile (uv vs qemu-x86_64).
        return f"""FROM --platform=linux/arm64 {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("pipecat-ai", "pipecat")
class Pipecat(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PipecatImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest -v / -rA output, one test per line:
        #   tests/test_foo.py::test_bar PASSED                    [ 12%]
        #   tests/test_foo.py::TestX::test_y FAILED               [ 30%]
        #   tests/test_foo.py::test_z SKIPPED (reason)            [ 45%]
        # The -rA summary repeats these as "PASSED tests/..::.." — both forms
        # are matched; set semantics dedupe.
        line_re = re.compile(
            r"^(?:(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"|(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+))\b"
        )

        for line in clean.splitlines():
            line = line.strip()
            m = line_re.match(line)
            if not m:
                continue
            name = m.group(1) or m.group(4)
            status = m.group(2) or m.group(3)
            if not name:
                continue
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # Disjoint sets: failed > skipped > passed.
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
