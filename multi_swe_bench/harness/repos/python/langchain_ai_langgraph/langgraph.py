import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# langchain-ai/langgraph — a Python library for stateful agent graphs.
#
# Discovery (verified in Docker, python:3.12-bookworm, native arm64):
#  - 60-PR range #8..#7730. Early PRs are a single package (root pyproject +
#    root tests/); from ~#23 the repo is a uv monorepo: libs/langgraph,
#    libs/checkpoint, libs/checkpoint-{sqlite,postgres,duckdb}, libs/cli,
#    libs/prebuilt, ... — each its own uv package with a flat tests/ dir.
#  - One config, no era split: each PR's test files identify the package
#    directory(ies) to test; the runner `cd`s into each, `uv sync`s it, and
#    runs `uv run pytest tests/`. Both the monorepo and the old single-package
#    layout are handled by the same path-derived package list.
#  - pytest throughout -> one parse_log. Per-package runs are tagged with a
#    `### LGPKG: <dir> ###` marker so test ids stay unique across packages.
#  - Note: libs/checkpoint-postgres tests need a live Postgres; without one
#    they fail in every stage (and simply do not resolve) — non-Postgres
#    packages (langgraph, checkpoint, prebuilt, cli, sqlite) are unaffected.


def _test_pkgs(patch: str) -> list[str]:
    """Package directories owning the test files in a patch.

    `libs/langgraph/tests/unit/x.py` -> `libs/langgraph`
    `tests/test_pregel.py`           -> `.`   (old single-package era)
    """
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if not path.endswith(".py"):
            continue
        if "/tests/" in path:
            pkgs.add(path.split("/tests/")[0])
        elif path.startswith("tests/"):
            pkgs.add(".")
    return sorted(pkgs)


class LanggraphImageBase(Image):
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

        # Pin to linux/arm64 (the eval host's native arch): `uv` is a Rust
        # binary that segfaults under qemu-x86_64, so the amd64 leg of a
        # multi-arch build cannot run it. Pinning keeps every build step
        # native; the multi-arch manifest still lists both arches.
        return f"""FROM --platform=linux/arm64 {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_ROOT_USER_ACTION=ignore
ENV UV_LINK_MODE=copy
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential libpq-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

{code}

{self.clear_env}

"""


class LanggraphImageDefault(Image):
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
        return LanggraphImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

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

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm: sync each package the PR's tests live in (deps cached into the image).
bash /home/run_tests.sh --warm-only || true
""".replace("__REPO__", repo).replace("__SHA__", sha)

        # Per-package: install deps into an in-package .venv, then pytest.
        # The monorepo spans two eras — early packages are poetry, later ones
        # are uv — so the install is tool-detected. Both poetry (in-project
        # mode) and uv produce `.venv/`, so the test runner is uniform.
        # Each run is fenced by a LGPKG marker for unique cross-package ids.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
WARM=0
[ "${1:-}" = "--warm-only" ] && WARM=1

install_pkg() {
  cd "$1" || return 0
  if [ -f poetry.lock ] || grep -qF '[tool.poetry]' pyproject.toml 2>/dev/null; then
    pip install -q poetry >/dev/null 2>&1 || true
    poetry config virtualenvs.in-project true >/dev/null 2>&1 || true
    poetry install --all-extras >/dev/null 2>&1 \\
      || poetry install >/dev/null 2>&1 || true
    # Backup for the oldest "permchain" era, whose ancient poetry.lock can
    # break `poetry install`: build via the poetry-core backend with pip,
    # which still resolves [tool.poetry.dependencies] from PyPI.
    if [ -x .venv/bin/python ]; then
      .venv/bin/python -m pip install -q -e . >/dev/null 2>&1 || true
    fi
  else
    uv sync --all-extras --group dev >/dev/null 2>&1 \\
      || uv sync --all-extras >/dev/null 2>&1 \\
      || uv sync >/dev/null 2>&1 \\
      || uv pip install -e . >/dev/null 2>&1 || true
  fi
  if [ -x .venv/bin/python ]; then
    .venv/bin/python -m pip install -q pytest pytest-asyncio pytest-mock syrupy \
      >/dev/null 2>&1 || true
  fi
}

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  ( install_pkg "$pkg" )
  [ "$WARM" = "1" ] && continue
  echo "### LGPKG: $pkg ###"
  ( cd "$pkg" || exit 0
    PYBIN=.venv/bin/python
    [ -x "$PYBIN" ] || PYBIN=python
    # -o addopts="": discard the pyproject's inherited pytest addopts, which
    # across eras reference plugin-specific flags (e.g. --snapshot-warn-unused)
    # that abort the whole run when that plugin/flag is absent.
    "$PYBIN" -m pytest tests/ -v -rA --continue-on-collection-errors \\
      -o addopts="" -p no:cacheprovider 2>&1 || true )
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.webp --exclude=*.pdf "
            "--exclude=*.woff --exclude=*.woff2 --exclude=*.ttf --exclude=*.zip"
        )

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
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

        # arm64 pin — see LanggraphImageBase.dockerfile (uv vs qemu-x86_64).
        return f"""FROM --platform=linux/arm64 {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("langchain-ai", "langgraph")
class Langgraph(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LanggraphImageDefault(self.pr, self._config)

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

        # pytest -v / -rA, one test per line. The `### LGPKG: <pkg> ###` fence
        # prefixes ids so the same `tests/test_x.py::t` in two packages stays
        # distinct.
        line_re = re.compile(
            r"^(?:(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"|(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+))\b"
        )
        pkg_re = re.compile(r"^### LGPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.strip()
            pm = pkg_re.match(line)
            if pm:
                pkg = pm.group(1)
                continue
            m = line_re.match(line)
            if not m:
                continue
            name = m.group(1) or m.group(4)
            status = m.group(2) or m.group(3)
            if not name:
                continue
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status in ("PASSED", "XPASS"):
                passed_tests.add(tid)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(tid)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(tid)

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
