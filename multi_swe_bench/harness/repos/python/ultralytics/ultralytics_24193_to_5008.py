import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class UltralyticsImageDefault(Image):
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
        # Returning a str base image makes DockerfileEnhancer.enhance() treat this
        # as a buildable image: it standardizes the `git clone` line, passes
        # REPO_URL/BASE_COMMIT build args, and auto-injects the git-history
        # hardening block (single-commit history, no refs/remotes/reflog).
        return "python:3.11-bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_files(self) -> str:
        """Test files touched by test.patch (under tests/, *.py).

        Running only these instead of the whole tests/ suite isolates the PR's
        f2p signal and avoids flaky unrelated failures (downloads/timeouts) that
        otherwise dilute or mask the fail->pass transition. Mirrors the tldraw
        config's approach.
        """
        seen = set()
        out = []
        for m in re.findall(r"diff --git a/(\S+)", self.pr.test_patch or ""):
            if m.startswith("tests/") and m.endswith(".py") and m not in seen:
                seen.add(m)
                out.append(m)
        return " ".join(out)

    def files(self) -> list[File]:
        test_files = self._test_files()
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git diff --name-only
git diff --name-only --cached
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

git reset --hard
git checkout --force {base_sha}
git clean -fd

# Install CPU-only torch, PINNED to 2.2.2 (released before the torch 2.6
# weights_only=True default flip). Unpinned torch (2.6+/2.12) breaks ultralytics
# 8.0-8.2.x with mass `NameError/weights_only` model-load failures; 2.2.2 defaults
# weights_only=False and is supported across the dataset's 8.0->8.3 range.
pip install --no-cache-dir torch==2.2.2 torchvision==0.17.2 --index-url https://download.pytorch.org/whl/cpu

# Install ultralytics with dev deps (handles both setup.py and pyproject.toml eras)
pip install --no-cache-dir -e '.[dev]' || pip install --no-cache-dir -e '.'

# Install pytest if not already present (fallback for very old PRs)
pip install --no-cache-dir pytest pytest-cov 2>/dev/null || true

# Pin numpy<2 LAST so it wins: torch 2.2.2 is built against the numpy 1.x C-ABI,
# and torch/ultralytics deps otherwise pull numpy 2.x -> `RuntimeError: Numpy is
# not available` on every model load. numpy 1.26.4 satisfies ultralytics (>=1.23)
# across the 8.0->8.4 range.
pip install --no-cache-dir "numpy<2"

# NOTE: no torch.load monkeypatch needed. torch is pinned to 2.2.2, which still
# defaults weights_only=False, so model loads work across the 8.0->8.4 range.
# (The old sitecustomize patch was both redundant under this pin and broken by
# bash double-quote nesting -> `NameError: name 'weights_only' is not defined`.)

# Warm up: verify import works
python3 -c "import ultralytics; print(f'ultralytics {{ultralytics.__version__}} ready')"
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
TARGET="{test_files}"
RUN=""
for f in $TARGET; do [ -f "$f" ] && RUN="$RUN $f"; done
[ -z "$RUN" ] && RUN="tests/"
pytest $RUN --ignore=tests/test_cuda.py --tb=short -rA 2>&1
""".format(repo=self.pr.repo, test_files=test_files),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

# Clean any previous patch state
git checkout -- . 2>/dev/null || true

# Apply test patch
if ! git apply --whitespace=nowarn /home/test.patch; then
    git apply --whitespace=nowarn --reject /home/test.patch || true
fi

# NOTE: ultralytics is installed editable (-e) in prepare.sh, so source-level
# patches are picked up without reinstall. We deliberately do NOT re-run
# `pip install -e .[dev]` here -- it would re-resolve and bump numpy back to 2.x,
# re-breaking torch 2.2.2 (`RuntimeError: Numpy is not available`).

# Run only the test files touched by test.patch (isolates the f2p signal)
TARGET="{test_files}"
RUN=""
for f in $TARGET; do [ -f "$f" ] && RUN="$RUN $f"; done
[ -z "$RUN" ] && RUN="tests/"
pytest $RUN --ignore=tests/test_cuda.py --tb=short -rA 2>&1
""".format(repo=self.pr.repo, test_files=test_files),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

# Clean any previous patch state
git checkout -- . 2>/dev/null || true

# Apply test patch first, then fix patch
if ! git apply --whitespace=nowarn /home/test.patch; then
    git apply --whitespace=nowarn --reject /home/test.patch || true
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    git apply --whitespace=nowarn --reject /home/fix.patch || true
fi

# NOTE: ultralytics is installed editable (-e) in prepare.sh, so source-level
# patches are picked up without reinstall. We deliberately do NOT re-run
# `pip install -e .[dev]` here -- it would re-resolve and bump numpy back to 2.x,
# re-breaking torch 2.2.2 (`RuntimeError: Numpy is not available`).

# Run only the test files touched by test.patch (isolates the f2p signal)
TARGET="{test_files}"
RUN=""
for f in $TARGET; do [ -f "$f" ] && RUN="$RUN $f"; done
[ -z "$RUN" ] && RUN="tests/"
pytest $RUN --ignore=tests/test_cuda.py --tb=short -rA 2>&1
""".format(repo=self.pr.repo, test_files=test_files),
            ),
        ]

    def dockerfile(self) -> str:
        repo = self.pr.repo

        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())

        # NOTE: no `# syntax=...` directive here so DockerfileEnhancer.enhance()
        # runs: it injects the proxy/cert/label infrastructure, REPO_URL/BASE_COMMIT
        # ARGs, and the git-history hardening block right before the final CMD.
        # The clone uses "${REPO_URL}" and checkout uses ${BASE_COMMIT}; both are
        # supplied as build args because dependency() returns a str.
        return f"""FROM python:3.11-bookworm

{self.global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git libgl1 libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("ultralytics", "ultralytics_24193_to_5008")
class Ultralytics(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return UltralyticsImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        log = ansi_re.sub("", log)

        # --- Real-name parsers (preferred). All produce stable pytest node IDs. ---

        # tests/test_python.py::test_workflow PASSED   (verbose, name-first)
        verbose_re = re.compile(
            r"^(tests/\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)",
            re.MULTILINE,
        )
        for m in verbose_re.finditer(log):
            test_name = m.group(1)
            status = m.group(2)
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # `pytest -rA` short-summary block is STATUS-FIRST, one line per test:
        #   PASSED tests/test_python.py::test_workflow
        #   FAILED tests/test_python.py::test_x - AssertionError
        #   SKIPPED [1] tests/test_python.py::test_y
        status_first_re = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED)\s+(?:\[\d+\]\s+)?(tests/\S+::\S+)",
            re.MULTILINE,
        )
        for m in status_first_re.finditer(log):
            status, test_name = m.group(1), m.group(2)
            if status == "PASSED":
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
                passed_tests.discard(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # FAILED tests/test_python.py::test_workflow - AssertionError
        failed_summary_re = re.compile(
            r"^FAILED\s+(tests/\S+::\S+)", re.MULTILINE
        )
        for m in failed_summary_re.finditer(log):
            test_name = m.group(1)
            failed_tests.add(test_name)
            passed_tests.discard(test_name)

        # --- Fallback ONLY if no real node IDs were found at all. ---
        # The compact progress line (`tests/test_python.py .F.sxX..`) has no test
        # names, so we synthesize positional ones (::test_1, ::test_2, ...). These
        # are UNSTABLE across stages and create spurious f2p/n2p churn, so we use
        # them only when -rA/verbose output is entirely absent (e.g. a crash before
        # the summary). With `pytest -rA` in the run scripts this should never fire.
        if not (passed_tests or failed_tests or skipped_tests):
            compact_re = re.compile(r"^(tests/\S+\.py)\s+([.FEsxX]+)", re.MULTILINE)
            for m in compact_re.finditer(log):
                test_file = m.group(1)
                results = m.group(2)
                for i, symbol in enumerate(results):
                    test_name = f"{test_file}::test_{i + 1}"
                    if symbol == ".":
                        passed_tests.add(test_name)
                    elif symbol in ("F", "E"):
                        failed_tests.add(test_name)
                    elif symbol in ("s", "x", "X"):
                        skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
