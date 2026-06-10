import re
from typing import Optional

from unidiff import PatchSet

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# TheAlgorithms/Python ships its tests as doctests embedded in the algorithm
# modules plus a handful of ``test_*.py`` files.  Running the whole repo with
# ``pytest --doctest-modules .`` would collect thousands of doctests, many of
# which need heavy optional dependencies.  Instead we scope every run to the
# ``.py`` files actually touched by the patch bundle and let pytest collect
# both doctests (``--doctest-modules``) and real test functions from them.
_EXCLUDED_BASENAMES = frozenset({
    "conftest.py",
    # Old TheAlgorithms/Python modules (pre `if __name__` convention) that
    # execute blocking network/socket code at import time.  `--doctest-modules`
    # imports every module to collect doctests, so collecting these would hang
    # the whole run forever.
    "server.py",
    "ftp_send_receive.py",
    "ftp_client_server.py",
})

# git apply must not abort the whole patch because of binary asset diffs
# (images, committed .pyc, .sqlite, VS .suo, notebooks ...) that some
# time-window bundles carry without full-index lines.  None of these affect
# the doctest/pytest run, so they are excluded from `git apply`.
_APPLY_EXCLUDES = " ".join(
    f"--exclude='*.{ext}'"
    for ext in (
        "png", "PNG", "jpg", "JPG", "jpeg", "JPEG", "gif", "GIF", "ico",
        "bmp", "tif", "tiff", "pyc", "pyo", "suo", "sqlite", "sqlite3",
        "db", "zip", "gz", "tar", "so", "pdf", "ipynb", "npy", "npz",
        "pkl", "xlsx", "docx", "class", "jar",
    )
)


def _changed_py_files(*patches: str) -> list[str]:
    """Union of ``.py`` files referenced by the given patches.

    Deleted files (``/dev/null`` target) are skipped.  Both source modules
    (for doctests) and ``test_*.py`` files are kept.
    """
    seen: set[str] = set()
    for patch in patches:
        if not patch:
            continue
        try:
            patch_set = PatchSet(patch)
        except Exception:
            continue
        for patched_file in patch_set:
            for path in (patched_file.target_file, patched_file.source_file):
                if not path or path == "/dev/null":
                    continue
                if path.startswith(("a/", "b/")):
                    path = path[2:]
                if path.endswith(".py") and path.rsplit("/", 1)[-1] not in _EXCLUDED_BASENAMES:
                    seen.add(path)
                    break
    return sorted(seen)


# Shared pytest invocation.  ``-o addopts=''`` neutralises the repo's own
# pyproject/pytest.ini addopts so the command behaves the same across eras;
# ``--doctest-modules`` is then added back explicitly.  Collection errors
# (a scoped module importing a dependency we did not install) must not abort
# the rest of the run, hence ``--continue-on-collection-errors``.
_PYTEST_SNIPPET = """mapfile -t CANDIDATES < /home/test_files.txt
SELECTED=()
for f in "${CANDIDATES[@]}"; do
    [ -n "$f" ] && [ -f "$f" ] && SELECTED+=("$f")
done
if [ ${#SELECTED[@]} -eq 0 ]; then
    echo "No scoped test files present at this revision."
    exit 0
fi
# timeout: wall-clock cap so an unforeseen blocking import cannot hang the
#          instance forever.  --timeout: per-test cap for runaway test bodies.
timeout --preserve-status --kill-after=30 1500 \\
    python -m pytest --doctest-modules --continue-on-collection-errors \\
    --timeout=120 --timeout-method=signal \\
    -p no:cacheprovider -o addopts='' -rN --tb=short -v "${SELECTED[@]}" || true
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

    def dependency(self) -> str:
        # python:3.13 satisfies the modern era's ``requires-python >= 3.13``
        # and still runs the pure-algorithm doctests of the 2017-2024 eras.
        return "python:3.13-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = _changed_py_files(self.pr.test_patch, self.pr.fix_patch)

        # prepare.sh detects the toolchain era at build time:
        #   - uv era  (2025+): uv.lock / dependency-groups in pyproject.toml
        #   - pip era (2020-2024): requirements.txt
        #   - ancient era (2017): nothing -> bare pytest is enough
        # Optional-dependency installs are best-effort (`|| true`): old pinned
        # requirements do not resolve on Python 3.13, but the generous extras
        # below still let the common doctests collect.
        prepare = """#!/bin/bash
set -uo pipefail
cd /home/{repo}
git config --global --add safe.directory /home/{repo} || true
git reset --hard
git checkout {sha}

python -m pip install --no-cache-dir -U pip setuptools wheel || true
python -m pip install --no-cache-dir pytest pytest-cov pytest-timeout || true

if [ -f requirements.txt ]; then
    python -m pip install --no-cache-dir -r requirements.txt || true
fi
if [ -f pyproject.toml ] && grep -q 'dependency-groups\\|\\[tool.uv\\]' pyproject.toml; then
    python -m pip install --no-cache-dir uv || true
    uv pip install --system --group test 2>/dev/null || true
fi

# Generous extras so doctests importing common scientific libraries collect.
python -m pip install --no-cache-dir \\
    numpy scipy sympy pandas matplotlib scikit-learn \\
    rich pillow requests beautifulsoup4 || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha)

        run = """#!/bin/bash
cd /home/{repo}
git reset --hard
git clean -fd
{snippet}""".format(repo=self.pr.repo, snippet=_PYTEST_SNIPPET)

        # Resilient apply: try a clean apply first; on failure fall back to
        # --reject so the .py changes still land even when an unrelated binary
        # asset or a context-mismatched data file in the bundle would otherwise
        # abort the whole patch.  Never `exit 1` -- a partial apply still yields
        # a usable test signal.
        test_run = """#!/bin/bash
cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn {excl} /home/test.patch \\
  || git apply --whitespace=nowarn {excl} --reject /home/test.patch \\
  || echo "Warning: test.patch did not fully apply"
{snippet}""".format(repo=self.pr.repo, snippet=_PYTEST_SNIPPET, excl=_APPLY_EXCLUDES)

        fix_run = """#!/bin/bash
cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn {excl} /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn {excl} --reject /home/test.patch /home/fix.patch \\
  || echo "Warning: patches did not fully apply"
{snippet}""".format(repo=self.pr.repo, snippet=_PYTEST_SNIPPET, excl=_APPLY_EXCLUDES)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "test_files.txt", "\n".join(test_files) + "\n"),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(
            f"COPY {f.name} /home/\n" for f in self.files()
        )

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {self.dependency()}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("TheAlgorithms", "Python")
class THEALGORITHMS_PYTHON(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.compile(r"\x1b\[[0-9;]*m").sub("", log)

        # pytest -v line: "<nodeid> <STATUS>  [ NN%]"
        # The nodeid may contain spaces (e.g. "project_euler/.../sol1.py"),
        # so the trailing "[ NN%]" is used as a reliable right anchor.
        line_re = re.compile(
            r"^(.+?)\s+(PASSED|FAILED|ERROR|XPASS|XFAIL|SKIPPED)\s+\[\s*\d+%\]"
        )
        # Modules that fail to import surface only as collection errors.
        collect_re = re.compile(r"ERROR collecting (.+?\.py)\b")

        for line in log.splitlines():
            m = line_re.match(line.rstrip())
            if m:
                node, status = m.group(1).strip(), m.group(2)
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(node)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(node)
                elif status in ("SKIPPED", "XFAIL"):
                    skipped_tests.add(node)
                continue
            cm = collect_re.search(line)
            if cm:
                failed_tests.add(cm.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
