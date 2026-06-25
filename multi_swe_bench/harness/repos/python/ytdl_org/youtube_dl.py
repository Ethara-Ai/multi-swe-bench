import json as _json
import re
from typing import Optional

from multi_swe_bench.harness import pull_request as _pull_request
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# --- number_interval auto-fill (kept ENTIRELY inside this registry) ----------
# These records are PR-bundles: each row carries a `prs_in_bundle` list but an
# empty `number_interval`. The required OUTPUT format is the dash-joined bundle
# list (e.g. [146,147,150,155,157] -> "146-147-150-155-157"), NOT a range like
# "146-157" (a range would wrongly imply every PR in between is included).
#
# IMPORTANT: number_interval doubles as an instance-routing key — Instance.create
# uses `f"{org}/{number_interval}"` as the registry lookup when it is non-empty
# (the "era key" mechanism). Our per-bundle value is NOT a registered era key, so
# populating pr.number_interval before the build would break instance creation
# ("Instance 'ytdl-org/14534-14550-...' is not registered"). We therefore keep
# pr.number_interval EMPTY during build/routing and only stamp the dash-joined
# value onto the OUTPUT row.
#
# Two import-time patches, scoped to this registry (no edits to harness source):
#   1. PullRequest.from_json — `prs_in_bundle` is not a PullRequest field (the
#      schema loader drops it), so we re-read the raw json HERE and stash the
#      dash-joined value in a NON-field attr `_ytdl_number_interval` for
#      ytdl-org/youtube-dl rows. pr.number_interval stays "" so routing is
#      unaffected and the attr is not serialized.
#   2. Dataset.build — the harness builder copies most PR fields but NOT
#      number_interval, so we wrap it to set ds.number_interval from the stashed
#      value. gen_report builds every output row via
#      Dataset.build(self.raw_dataset[id], report) and writes data.json(), so the
#      resolved jsonl then carries the dash-joined number_interval.
if not getattr(_pull_request.PullRequest, "_ytdl_number_interval_patched", False):
    _ytdl_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _ytdl_from_json(cls, json_str):
        pr = _ytdl_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "ytdl-org"
                and raw.get("repo") == "youtube-dl"
                and raw.get("prs_in_bundle")
            ):
                # Stash only — do NOT set pr.number_interval (routing key).
                pr._ytdl_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_ytdl_from_json)
    _pull_request.PullRequest._ytdl_number_interval_patched = True

    # Patch Dataset.build to stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_ytdl_build_patched", False):
        _ytdl_orig_build = _Dataset.build.__func__

        def _ytdl_build(cls, pr, report):
            ds = _ytdl_orig_build(cls, pr, report)
            ni = getattr(pr, "_ytdl_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_ytdl_build)
        _Dataset._ytdl_build_patched = True
# -----------------------------------------------------------------------------


# ytdl-org/youtube-dl — command-line program to download videos from the web.
#
# TWO-IMAGE, shared-base design (efficient + self-contained, proxy/cert-free),
# conformed to the hardened harness image.py:
#
#   ImageBase  ->  mswebench/ytdl-org_m_youtube-dl:base   (ONE shared image)
#       Clones the repo ONCE (full history) and installs the COMMON toolchain
#       (the pytest runner; youtube-dl's unit tests have no third-party runtime
#       deps). No BASE_COMMIT is used and NO hardening runs here — the base must
#       keep full history so every PR can check out its own commit. Built once
#       and reused by all PRs.
#
#   ImageDefault  ->  mswebench/ytdl-org_m_youtube-dl:pr-<N>   (per PR)
#       FROM the shared base, so the toolchain is already present. Checks out
#       the PR's BASE_COMMIT (baked as an ARG default), runs install.sh (an
#       editable install at that commit; cheap, deps reused from the base),
#       bakes the eval scripts + patches, and THEN runs the _HARDENING_BLOCK
#       that detaches HEAD and strips every other ref/remote/commit. That scrub
#       is what closes the reward-hacking hole (git log / show / diff origin),
#       and it lives in the PR image because it can only run after the per-PR
#       checkout.
#
# Both images are SELF-CONTAINED and proxy/cert-free: each dockerfile() emits
# the complete Dockerfile starting with "# syntax=docker/dockerfile:1.6", which
# makes DockerfileEnhancer.enhance() return it unchanged (early-return on the
# directive). So the enhancer's proxy / CA-cert / MITM injection never runs for
# youtube-dl, regardless of which image.py builds it.
#
# Single pure-Python package (youtube_dl/) with a root setup.py and a root
# test/ tree. pytest is the runner throughout -> one parse_log.


# Tests that require network access or external resources and cannot run
# reliably inside a Docker container. Mirrors the project's tox.ini / nosetests
# exclusions; these are never selected as F2P targets even if a patch touches
# them, since they can't pass offline.
_EXCLUDED_TEST_FILES = frozenset({
    "test/test_download.py",
    "test/test_age_restriction.py",
    "test/test_subtitles.py",
    "test/test_write_annotations.py",
    "test/test_youtube_lists.py",
    "test/test_iqiyi_sdk_interpreter.py",
    "test/test_socks.py",
})

_EXCLUDED_BASENAMES = frozenset({
    "helper.py",
    "conftest.py",
    "__init__.py",
})

# git apply excludes: be defensive about any binary fixtures a patch may carry,
# whose diff hunks would abort an otherwise-good apply.
_EXCLUDES = (
    "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
    "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip"
)


def _target_test_files(test_patch: str) -> list[str]:
    """Test files the test_patch touches — the only ones worth running.

    Scoping to these (instead of the whole test/ tree) is the standard SWE-bench
    F2P/P2P convention. We read the destination ("+++ b/...") side so newly-added
    test files are captured too. Network-dependent files (see _EXCLUDED_TEST_FILES)
    and non-test helpers are filtered out. Deleted files map to +++ /dev/null and
    are naturally excluded.
    """
    files: list[str] = []
    for dst in re.findall(r"^\+\+\+ b/(.+?)\s*$", test_patch, re.M):
        if not dst.endswith(".py") or not dst.startswith("test/"):
            continue
        basename = dst.rsplit("/", 1)[-1]
        if dst in _EXCLUDED_TEST_FILES or basename in _EXCLUDED_BASENAMES:
            continue
        if dst not in files:
            files.append(dst)
    return files


class ImageBase(Image):
    """Shared base image: clone once + install common toolchain. No checkout,
    no hardening."""

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
        return "python:3.8-slim-bullseye"

    def image_tag(self) -> str:
        # Single shared tag — identical for every PR, so it builds once and is
        # reused (Image equality / dedup is on image_full_name).
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_install = """#!/bin/bash
# COMMON toolchain install, shared by every PR. youtube-dl's unit tests have no
# third-party runtime dependencies, so this just provisions the pytest runner.
# Per-PR images reuse this; their install.sh only adds an editable install at
# their specific commit.
set -uo pipefail
cd /home/__REPO__
pip install --no-cache-dir pytest >/dev/null 2>&1 || true
""".replace("__REPO__", repo)
        return [File(".", "base_install.sh", base_install)]

    def dockerfile(self) -> str:
        repo = self.pr.repo
        org = self.pr.org
        repo_url = f"https://github.com/{org}/{repo}.git"
        # Self-contained + proxy/cert-free (syntax directive -> enhancer skips).
        # Clones FULL history (no checkout) so PRs can check out any commit.
        template = """# syntax=docker/dockerfile:1.6

FROM python:3.8-slim-bullseye

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
    """Per-PR image: FROM shared base, checkout the PR commit, install the
    package at that commit, bake patches/scripts, then harden."""

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
        # built FROM mswebench/ytdl-org_m_youtube-dl:base, reusing its toolchain.
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
# Per-PR install: editable install at THIS commit so `import youtube_dl`
# resolves from source. Common toolchain (pytest) is already in the shared
# base, so this is cheap. Failures are tolerated — youtube-dl's unit tests
# import the package directly from the repo root regardless.
set -uo pipefail
cd /home/__REPO__
pip install --no-cache-dir -e . >/dev/null 2>&1 || true
pip install --no-cache-dir pytest >/dev/null 2>&1 || true
""".replace("__REPO__", repo)

        target_tests = " ".join(_target_test_files(self.pr.test_patch))

        # Select the patch's own test files that actually exist on disk (the
        # unpatched baseline run won't have newly-added ones yet), then pytest
        # only those. -v gives one `path::test STATUS` line per test for
        # parse_log; -o addopts="" discards any pinned plugin flags.
        # --timeout caps any single test that hangs (e.g. a fixed infinite
        # loop -> the new test must register as a bounded FAILED, the F2P
        # signal). pytest-timeout is best-effort; if absent the outer `timeout`
        # still bounds the whole run.
        run_pytest = """TARGET="__TARGET__"
SEL=""
for f in $TARGET; do [ -e "$f" ] && SEL="$SEL $f"; done
if [ -z "${SEL// }" ]; then
    echo ">>>>> No target test files present; nothing to run."
else
    timeout -k 30 1200 python -m pytest $SEL -v -rA --continue-on-collection-errors \\
        -o addopts="" -p no:cacheprovider || true
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


@Instance.register("ytdl-org", "youtube-dl")
class YoutubeDl(Instance):
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
        # Strip real ANSI escape sequences (a nodeid may contain the *literal*
        # text "\x1b" — pytest escapes control chars in param ids — leave intact).
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
