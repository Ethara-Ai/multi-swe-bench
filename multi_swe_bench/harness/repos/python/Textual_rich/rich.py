import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Textualize/rich — a Python library for rich text & formatting in the terminal.
#
# TWO-IMAGE, shared-base design (efficient + self-contained, proxy/cert-free):
#
#   ImageBase  ->  mswebench/textualize_m_rich:base   (ONE shared image)
#       Clones the repo ONCE (full history) and installs the COMMON deps
#       (rich's runtime deps via an editable install at the default branch +
#       the pytest toolchain). No BASE_COMMIT is used and NO hardening runs
#       here — the base must keep full history so every PR can check out its
#       own commit. Built once and reused by all PRs.
#
#   ImageDefault  ->  mswebench/textualize_m_rich:pr-<N>   (per PR)
#       FROM the shared base, so the common deps are already present. Checks
#       out the PR's BASE_COMMIT (baked as an ARG default), runs install.sh to
#       pick up only the deps that differ for that commit (common ones are
#       reused from the base, not reinstalled), bakes the eval scripts +
#       patches, and THEN runs the _HARDENING_BLOCK that detaches HEAD and
#       strips every other ref/remote/commit. That scrub is what closes the
#       reward-hacking hole (git log / show / diff origin) and it lives in the
#       PR image because it can only run after the per-PR checkout.
#
# Both images are SELF-CONTAINED and proxy/cert-free: each dockerfile() emits
# the complete Dockerfile starting with "# syntax=docker/dockerfile:1.6", which
# makes DockerfileEnhancer.enhance() return it unchanged (early-return on the
# directive). So the enhancer's proxy / CA-cert / MITM injection never runs for
# rich, regardless of which image.py builds it.
#
# Single pure-Python package (rich/) with a root poetry pyproject and a root
# tests/ tree. pytest is the runner throughout -> one parse_log. System pip
# (no venv / no uv); native arm64 build keeps the git tree clean for hardening.


# git apply excludes: rich's test/fix patches carry binary blobs (SVG/PNG/etc.
# rendered-output fixtures) whose diff hunks abort an otherwise-good apply.
_EXCLUDES = (
    "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
    "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip"
)


def _target_test_files(test_patch: str) -> list[str]:
    """Test files the test_patch touches — the only ones worth running.

    Scoping to these (instead of the whole tests/ tree) is the standard SWE-bench
    F2P/P2P convention; it also sidesteps unrelated tests in recent rich versions
    that hang forever on a tty/stdin read. We read the destination ("+++ b/...")
    side so newly-added test files are captured too (get_modified_files drops
    them, since their source is /dev/null). Deleted files map to +++ /dev/null and
    are naturally excluded.
    """
    files: list[str] = []
    for dst in re.findall(r"^\+\+\+ b/(.+?)\s*$", test_patch, re.M):
        if dst.endswith(".py") and (dst.startswith("tests/") or "/test" in dst or dst.startswith("test")):
            if dst not in files:
                files.append(dst)
    return files


class ImageBase(Image):
    """Shared base image: clone once + install common deps. No checkout, no hardening."""

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
        return "python:3.12-bookworm"

    def image_tag(self) -> str:
        # Single shared tag — identical for every PR, so it builds once and is
        # reused (Image equality / dedup is on image_full_name).
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_install = """#!/bin/bash
# COMMON dependency install, shared by every PR. Editable install at the default
# branch pulls rich's runtime deps (pygments, markdown-it-py, ...) plus the
# pytest toolchain. Per-PR images reuse all of this; their install.sh only adds
# whatever differs at their specific commit.
set -uo pipefail
cd /home/__REPO__
pip install --no-cache-dir -e ".[jupyter]" >/dev/null 2>&1 \\
  || pip install --no-cache-dir -e . >/dev/null 2>&1 || true
pip install --no-cache-dir pytest pytest-asyncio pytest-mock pytest-timeout >/dev/null 2>&1 || true
""".replace("__REPO__", repo)
        return [File(".", "base_install.sh", base_install)]

    def dockerfile(self) -> str:
        repo = self.pr.repo
        org = self.pr.org
        repo_url = f"https://github.com/{org}/{repo}.git"
        # Self-contained + proxy/cert-free (syntax directive -> enhancer skips).
        # Clones FULL history (no checkout) so PRs can check out any commit.
        template = """# syntax=docker/dockerfile:1.6

FROM python:3.12-bookworm

ARG TARGETARCH
ARG REPO_URL="__REPO_URL__"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ base image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
ENV PIP_ROOT_USER_ACTION=ignore
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${REPO_URL}" /home/__REPO__

COPY base_install.sh /home/base_install.sh
RUN bash /home/base_install.sh || true

CMD ["/bin/bash"]
"""
        return (
            template.replace("__REPO_URL__", repo_url)
            .replace("__ORG__", org)
            .replace("__REPO__", repo)
        )


class ImageDefault(Image):
    """Per-PR image: FROM shared base, checkout the PR commit, install PR-specific
    deps (reusing the base's common deps), bake patches/scripts, then harden."""

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
        # Depend on the shared base Image (not a string) -> this PR image is
        # built FROM mswebench/textualize_m_rich:base, reusing its deps.
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = f"{base.image_name()}:{base.image_tag()}"
        org, repo = self.pr.org, self.pr.repo
        sha = self.pr.base.sha
        # Self-contained Dockerfile. BASE_COMMIT is baked as the ARG default
        # (build_dataset only passes it for string-dependency images, i.e. the
        # base — so the PR image must carry its own commit). The hardening block
        # uses ${BASE_COMMIT} and runs AFTER checkout, scrubbing all history.
        template = """# syntax=docker/dockerfile:1.6

FROM __BASE__

ARG TARGETARCH
ARG BASE_COMMIT=__SHA__

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

ENV PIP_ROOT_USER_ACTION=ignore
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY install.sh /home/install.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/install.sh || true

__HARDENING__

CMD ["/bin/bash"]
"""
        return (
            template.replace("__BASE__", base_name)
            .replace("__SHA__", sha)
            .replace("__ORG__", org)
            .replace("__REPO__", repo)
            .replace("__HARDENING__", Image._HARDENING_BLOCK.rstrip("\n"))
        )

    def files(self) -> list[File]:
        repo = self.pr.repo

        install = """#!/bin/bash
# Per-PR install: editable install at THIS commit. Common deps are already in
# the shared base, so pip reuses them and only installs what differs here.
# Always (re)ensure the pytest runner + plugins.
set -uo pipefail
cd /home/__REPO__
pip install --no-cache-dir -e ".[jupyter]" >/dev/null 2>&1 \\
  || pip install --no-cache-dir -e . >/dev/null 2>&1 || true
# pytest-timeout is mandatory: some rich versions ship tests that block forever
# on a tty/stdin read or a live-display loop, which would otherwise hang the
# whole suite. A per-test timeout kills only the offending test.
pip install --no-cache-dir pytest pytest-asyncio pytest-mock pytest-timeout >/dev/null 2>&1 || true
""".replace("__REPO__", repo)

        target_tests = " ".join(_target_test_files(self.pr.test_patch))

        # Re-run install (a patch may add a dependency), select the patch's own
        # test files that actually exist on disk (the unpatched baseline run
        # won't have newly-added ones yet), then pytest only those. -v gives one
        # `path::test STATUS` line per test for parse_log; -o addopts="" discards
        # the coverage/plugin flags rich pins in pyproject. --timeout=30 (signal)
        # caps any single test that hangs (e.g. an infinite loop the PR fixes ->
        # the new test must register as a bounded FAILED, the F2P signal).
        run_pytest = """bash /home/install.sh || true
TARGET="__TARGET__"
SEL=""
for f in $TARGET; do [ -e "$f" ] && SEL="$SEL $f"; done
if [ -z "${SEL// }" ]; then
    echo ">>>>> No target test files present; nothing to run."
else
    timeout -k 30 1200 python -m pytest $SEL -v -rA --continue-on-collection-errors \\
        -o addopts="" -p no:cacheprovider --timeout=30 --timeout-method=signal || true
fi""".replace("__TARGET__", target_tests)

        run_sh = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO__
__RUN_PYTEST__
""".replace("__REPO__", repo).replace("__RUN_PYTEST__", run_pytest)

        test_run = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
__RUN_PYTEST__
""".replace("__REPO__", repo).replace("__EXCLUDES__", _EXCLUDES).replace(
            "__RUN_PYTEST__", run_pytest
        )

        fix_run = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
__RUN_PYTEST__
""".replace("__REPO__", repo).replace("__EXCLUDES__", _EXCLUDES).replace(
            "__RUN_PYTEST__", run_pytest
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", install),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


@Instance.register("Textualize", "rich")
class Rich(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        # Strip real ANSI escape sequences (note: a nodeid may contain the
        # *literal* text "\x1b" — pytest escapes control chars in param ids —
        # which this leaves intact).
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Parse ONLY pytest's `-v` progress lines, anchored on the unambiguous
        # "[ NN%]" suffix. Non-greedy nodeid keeps params containing spaces or
        # " - " whole; the `-rA` summary lines are deliberately ignored.
        line_re = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\(.*?\))?\s+\[\s*\d+%\]\s*$"
        )

        for line in clean.splitlines():
            m = line_re.match(line.rstrip())
            if not m:
                continue
            name = m.group(1).strip()
            status = m.group(2)
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
