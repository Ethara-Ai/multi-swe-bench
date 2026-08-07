"""TauricResearch/TradingAgents registry config (single era, 3 PRs).

Dataset: TauricResearch__TradingAgents_lht_final.jsonl -- PRs 13, 379, 453.
  * requires-python : ">=3.10" at every base sha -> python:3.12-slim.
  * package manager : pyproject.toml (v0.2.x, PRs 379/453) and a plain
    requirements.txt (v0.1.x, PR 13). prepare.sh handles both.
  * test framework  : stdlib ``unittest`` files under ``tests/`` (379, 453),
    driven by pytest; PR 13 predates ``tests/`` and its test patch adds a
    top-level ``test.py`` run as a script.

Layering follows multi_swe_bench.harness.image:

  * ``TradingAgentsImageBase`` -- toolchain + a full-history clone, tagged
    ``base`` and SHARED by all three PRs. It opts out of DockerfileEnhancer by
    emitting ``DockerfileEnhancer.SYNTAX_DIRECTIVE`` itself, then calls
    ``DockerfileEnhancer._infrastructure_block`` to lay down the exact same
    ARG/ENV/LABEL infra the enhancer would have injected. The opt-out is
    required: automatic enhancement rewrites the clone into
    ``git checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK``, which under a
    constant tag would prune the shared base to whichever PR built first and
    break the other two with "reference is not a tree".
  * ``TradingAgentsImageDefault`` -- per-PR layer. prepare.sh checks out this
    PR's base.sha, then ``Image._HARDENING_BLOCK`` runs with ``${BASE_COMMIT}``
    resolved to that literal sha, detaching there and stripping every other
    ref/reflog so the fix commit is unreachable.
"""

from __future__ import annotations

import json as _json
import re
from typing import Optional

from multi_swe_bench.harness import pull_request as _pull_request
from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "TauricResearch"
_REPO = "TradingAgents"

# ---------------------------------------------------------------------------
# number_interval: every bundle member listed explicitly, never a range
# ---------------------------------------------------------------------------
# A bundled instance covers a specific set of PRs, so the field enumerates them
# dash-joined -- "146-147-150-155-157" -- rather than "146-157", which would
# falsely claim 148, 149, 151-154 and 156 are in the bundle. For this dataset
# that distinction is load-bearing but invisible at a glance: "453-464" is two
# members, not twelve, and PR 13 covers 17 non-contiguous PRs.
#
# TauricResearch__TradingAgents_lht_final.jsonl ships `prs_in_bundle` but has no
# `number_interval` key at all. `prs_in_bundle` is not a PullRequest field and
# the dataclass_json loader silently drops unknown keys, so the bundle is gone
# before any registry sees the PR. Two import-time patches, scoped to this repo,
# recover it without editing harness source:
#
#   1. PullRequest.from_json (the loader used by both build_dataset.py:424 and
#      gen_report.py:267) -- re-read the raw line and stash the dash-joined value
#      on a NON-field attr, so it is never serialized. pr.number_interval is
#      deliberately left "" : Instance.create uses f"{org}/{number_interval}" as
#      its registry key whenever that field is non-empty (instance.py:42-43), so
#      populating it here would send routing after an unregistered
#      "TauricResearch/453-464" and raise instead of resolving this class.
#   2. Dataset.build -- the single output-serialization point. gen_report emits
#      every resolved-jsonl row via Dataset.build(self.raw_dataset[id], report)
#      (gen_report.py:599), which copies number_interval off a SEPARATELY loaded
#      PullRequest. Stamping it anywhere else would not survive to the output.
if not getattr(_pull_request.PullRequest, "_tauric_number_interval_patched", False):
    _tauric_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _tauric_from_json(cls, json_str):
        pr = _tauric_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            bundle = raw.get("prs_in_bundle")
            # Require a non-empty list of real ints. Without the type check a
            # malformed field is not an error but a silent corruption: sorted()
            # on a string sorts its characters, so "not-a-list" would stamp
            # "----a-i-l-n-o-s-t-t" into the resolved dataset. bool is excluded
            # because it is an int subclass.
            if (
                raw.get("org") == _ORG
                and raw.get("repo") == _REPO
                and isinstance(bundle, (list, tuple))
                and bundle
                and all(
                    isinstance(n, int) and not isinstance(n, bool) for n in bundle
                )
            ):
                # Stash only -- must NOT touch pr.number_interval (routing key).
                pr._tauric_number_interval = "-".join(str(n) for n in sorted(bundle))
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_tauric_from_json)
    _pull_request.PullRequest._tauric_number_interval_patched = True

    # Dataset subclasses PullRequest and therefore INHERITS the flag just set.
    # Check the class's own __dict__ rather than getattr, which would see the
    # inherited flag and wrongly skip this patch.
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_tauric_build_patched", False):
        _tauric_orig_build = _Dataset.build.__func__

        def _tauric_build(cls, pr, report):
            ds = _tauric_orig_build(cls, pr, report)
            interval = getattr(pr, "_tauric_number_interval", "")
            if interval:
                ds.number_interval = interval
            return ds

        _Dataset.build = classmethod(_tauric_build)
        _Dataset._tauric_build_patched = True

# Import-time credentials. tradingagents wires several provider SDKs at module
# import, so collection of tests/ aborts without them. Dummy values only -- no
# test in this dataset reaches the network on purpose (see PR 13 caveat below).
_DUMMY_ENV = """ENV OPENAI_API_KEY=sk-dummy0000000000000000000000000000000000000000
ENV ANTHROPIC_API_KEY=sk-ant-dummy0000000000000000000000000000000000
ENV GOOGLE_API_KEY=dummy
ENV GEMINI_API_KEY=dummy
ENV FINNHUB_API_KEY=dummy
ENV ALPHA_VANTAGE_API_KEY=dummy
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_ROOT_USER_ACTION=ignore
ENV PYTHONDONTWRITEBYTECODE=1"""

# One command shared by all three stages. report.py diffs run/test/fix by test
# name, so any drift between the stages would show up as phantom transitions.
_PYTEST_CMD = (
    "python -m pytest tests/ -v -rA --tb=short "
    "-p no:cacheprovider --continue-on-collection-errors"
)

# PR 13's test patch adds a top-level ``test.py`` that is a plain script, not a
# runner: on success it prints only its own text and nothing that identifies a
# test. Parsing that by sniffing for tracebacks means a *passing* stage yields
# zero tests, and report.py rejects an instance whose fix stage counts nothing.
# So the stage emits its own verdict line and parse_log reads that instead.
_SCRIPT_TEST_NAME = "test_yfinance_dataflow"
_SCRIPT_STAGE = f"""if [ -f test.py ]; then
    if python test.py 2>&1; then
        echo ">>> RESULT {_SCRIPT_TEST_NAME} PASSED"
    else
        echo ">>> RESULT {_SCRIPT_TEST_NAME} FAILED"
    fi
fi"""

# Only a PR whose test patch *introduces* test.py gets that stage. Verified in
# Docker: PRs 379 and 453 already carry an unrelated top-level test.py at their
# base sha -- a live yfinance download -- so an unconditional stage ran it in
# their baseline, spending a minute of network I/O to record a test id that
# never appears in the other two stages.
_ADDS_SCRIPT_TEST = re.compile(r"^\+\+\+ b/test\.py$", re.MULTILINE)


class TradingAgentsImageBase(Image):
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
        return "python:3.12-slim"

    def image_tag(self) -> str:
        # Constant: this layer carries no PR-specific state, so all 3 PRs share it.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        repo = self.pr.repo

        # Same package list and apt helper Image.dockerfile() uses, so the
        # deprecated-Debian sources rewrite stays in one place.
        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        packages_str = " \\\n    ".join(default_packages + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # The infra the DockerfileEnhancer would have injected, requested
        # explicitly because the syntax directive below opts this file out of
        # automatic enhancement.
        infra = DockerfileEnhancer._infrastructure_block(self, base_img)

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            infra.rstrip("\n"),
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "WORKDIR /home/\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n"
            "ENV LANG=C.UTF-8\n"
            "ENV CI=true"
        )
        sections.append(apt_command)
        sections.append(_DUMMY_ENV)
        sections.append("RUN pip install --no-cache-dir --upgrade pip setuptools wheel")
        sections.append("RUN git config --global --add safe.directory '*'")
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")

        # Cut the remote here so no later layer can fetch the fix commit. Refs
        # and history stay intact -- every PR's base.sha must remain reachable
        # for the per-PR layer to check out.
        sections.append(
            "RUN git remote remove origin 2>/dev/null || true; \\\n"
            "    git config --local gc.auto 0; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            '    git config --local remote.pushDefault ""'
        )

        extra_setup = self.extra_setup()
        if extra_setup:
            sections.append(extra_setup)

        sections.append("WORKDIR /home/")

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class TradingAgentsImageDefault(Image):
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
        return TradingAgentsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def script_stage(self) -> str:
        """The test.py stage, for the one PR whose test patch adds that file."""
        if _ADDS_SCRIPT_TEST.search(self.pr.test_patch or ""):
            return _SCRIPT_STAGE
        return ""

    def files(self) -> list[File]:
        repo = self.pr.repo
        script_stage = self.script_stage()
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git diff --name-only
git diff --name-only --cached
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
git config --global --add safe.directory '*'
cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

# Install deps -- handle both eras (pyproject.toml from v0.2.x, requirements.txt
# from v0.1.x). Tolerant: a resolver failure on one era must not fail the build,
# the test stages surface the real breakage.
if [ -f pyproject.toml ]; then
    pip install --no-cache-dir -e . 2>&1 || true
fi
if [ -f requirements.txt ] && [ "$(head -1 requirements.txt)" != "." ]; then
    pip install --no-cache-dir -r requirements.txt 2>&1 || true
fi
# Test framework -- not declared by either era.
pip install --no-cache-dir pytest 2>&1 || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}

# Baseline stage: no patches. report.py uses it as temporal proof of which
# tests existed before test.patch, so it runs the same command as the other
# two stages rather than reporting nothing.
if [ -d tests/ ]; then
    {_PYTEST_CMD} 2>&1
fi

{script_stage}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch \\
    && ! git apply --whitespace=nowarn --3way /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi

# The test patch can add a test-only dependency via pyproject.toml.
if [ -f pyproject.toml ]; then
    pip install --no-cache-dir -e . 2>&1 || true
fi

if [ -d tests/ ]; then
    {_PYTEST_CMD} 2>&1
fi

# PR 13 predates tests/: its test patch adds a top-level test.py.
{script_stage}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch \\
    && ! git apply --whitespace=nowarn --3way /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch \\
    && ! git apply --whitespace=nowarn --3way /home/fix.patch; then
    echo "Error: git apply of fix.patch failed" >&2
    exit 1
fi

if [ -f pyproject.toml ]; then
    pip install --no-cache-dir -e . 2>&1 || true
fi
if [ -f requirements.txt ] && [ "$(head -1 requirements.txt)" != "." ]; then
    pip install --no-cache-dir -r requirements.txt 2>&1 || true
fi

if [ -d tests/ ]; then
    {_PYTEST_CMD} 2>&1
fi

{script_stage}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # dependency() is an Image, so DockerfileEnhancer.enhance() returns this
        # verbatim and injects no hardening -- embed it here. prepare.sh has
        # already checked out base.sha; the block detaches at that literal sha
        # and strips every other ref so the fix commit is unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {dep.image_full_name()}",
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(copy_commands)
        sections.append("RUN bash /home/prepare.sh")
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(hardening)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


@Instance.register("TauricResearch", "TradingAgents")
class TradingAgents(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TradingAgentsImageDefault(self.pr, self._config)

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        log = ansi_re.sub("", log)

        # pytest verbose: tests/xxx.py::test_name PASSED/FAILED/SKIPPED
        pytest_verbose_re = re.compile(
            r"^(tests/\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)",
            re.MULTILINE,
        )
        for m in pytest_verbose_re.finditer(log):
            name = m.group(1)
            status = m.group(2)
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # -rA short summary: "PASSED tests/xxx.py::test_name", and the
        # file-level "ERROR tests/xxx.py" that --continue-on-collection-errors
        # emits for an import that never yields test ids.
        pytest_summary_re = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(tests/\S+)",
            re.MULTILINE,
        )
        for m in pytest_summary_re.finditer(log):
            status = m.group(1)
            name = m.group(2)
            if status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

        # PR 13's script stage, which reports its own verdict (see _SCRIPT_STAGE).
        script_re = re.compile(
            r"^>>> RESULT (\S+) (PASSED|FAILED)\b", re.MULTILINE
        )
        for m in script_re.finditer(log):
            if m.group(2) == "PASSED":
                passed_tests.add(m.group(1))
            else:
                failed_tests.add(m.group(1))

        # Deduplication: same test in both passed and failed -> it failed.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
