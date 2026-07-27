import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness import pull_request as _pull_request
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# --- number_interval auto-fill (kept ENTIRELY inside this registry) ----------
# These records are PR-bundles: each row carries a `prs_in_bundle` list but no
# `number_interval`. The required OUTPUT format is the dash-joined bundle list
# (e.g. [146,147,150,155,157] -> "146-147-150-155-157"), NOT a range like
# "146-157" — a range would wrongly imply every PR in between is included.
#
# IMPORTANT: number_interval doubles as an instance-routing key — Instance.create
# uses `f"{org}/{number_interval}"` as the registry lookup when it is non-empty
# (the "era key" mechanism). Our per-bundle value is NOT a registered era key, so
# populating pr.number_interval before the build would break instance creation
# ("Instance 'langchain-ai/14-15-16-18' is not registered"). We therefore keep
# pr.number_interval EMPTY during build/routing and only stamp the dash-joined
# value onto the OUTPUT row.
#
# Two import-time patches, scoped to this registry (no edits to harness source):
#   1. PullRequest.from_json — `prs_in_bundle` is not a PullRequest field (the
#      schema loader silently drops it), so we re-read the raw json HERE and
#      stash the dash-joined value in a NON-field attr `_lg_number_interval` for
#      langchain-ai/langgraph rows. pr.number_interval stays "" so routing is
#      unaffected, and the attr is not serialized. Both loaders that matter
#      (build_dataset.py and gen_report.py) go through PullRequest.from_json.
#   2. Dataset.build — the harness builder copies pr.number_interval (which we
#      deliberately left empty), so we wrap it to set ds.number_interval from the
#      stashed value. gen_report builds every output row via
#      Dataset.build(self.raw_dataset[id], report) and writes data.json(), so the
#      resolved jsonl then carries the dash-joined number_interval.
if not getattr(_pull_request.PullRequest, "_lg_number_interval_patched", False):
    _lg_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _lg_from_json(cls, json_str):
        pr = _lg_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "langchain-ai"
                and raw.get("repo") == "langgraph"
                and raw.get("prs_in_bundle")
            ):
                # Stash only — do NOT set pr.number_interval (routing key).
                pr._lg_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_lg_from_json)
    _pull_request.PullRequest._lg_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above.
    # Use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_lg_build_patched", False):
        _lg_orig_build = _Dataset.build.__func__

        def _lg_build(cls, pr, report):
            ds = _lg_orig_build(cls, pr, report)
            ni = getattr(pr, "_lg_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_lg_build)
        _Dataset._lg_build_patched = True
# -----------------------------------------------------------------------------


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
#
# Image topology — TWO-TIER: toolchain-only shared base + hardened per-PR layer.
# Same shape as golang/cloudwego/eino.py; this is the fleet-standard layout and
# it is load-bearing, because of how DockerfileEnhancer works:
#
#   * enhance() only rewrites an image whose dependency() is a *string*. For such
#     an image, IF the Dockerfile contains a clone, it force-injects
#     `git checkout ${BASE_COMMIT}` + Image._HARDENING_BLOCK (delete every ref,
#     drop origin, `git gc --prune=now --aggressive`).
#
#   * LanggraphImageBase is SHARED — one `:base` image backs all 60 PRs, which
#     have 60 DISTINCT base SHAs. So the base MUST NOT CLONE. A shared
#     string-dependency image that clones gets force-pinned to whichever PR built
#     first and has every other commit GC'd, breaking `git checkout` for the
#     other 59. With no clone in it, _standardize_repo_fetch has nothing to
#     rewrite and _inject_final_sanitize bails out early, so the shared base is
#     left un-pinned and un-hardened. It provides only python + apt deps + uv.
#
#   * The clone therefore lives in LanggraphImageDefault, per-PR. Its
#     dependency() is an Image, so enhance() returns the Dockerfile verbatim —
#     the clone + hardening below are kept exactly as written, and pinning here
#     is CORRECT because it is per-PR, not the shared base.
#
#   * `ARG BASE_COMMIT="<sha>" / ENV BASE_COMMIT=${BASE_COMMIT}` supplies the
#     value that Image._HARDENING_BLOCK is templated on. This matters: build args
#     are only passed for string-dependency images (build_dataset.py: `if
#     isinstance(dep, str)`), so in this layer ${BASE_COMMIT} would otherwise
#     expand to the empty string. Supplying it as an ARG default lets us use the
#     canonical _HARDENING_BLOCK VERBATIM — no string substitution — so any
#     future change to image.py's hardening propagates here automatically.


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
        # Warm-up script run ONCE in the shared base (changedetection/rich style).
        # It runs at the freshly-cloned HEAD to pre-populate the pip/uv download
        # caches with the common test stack, so each per-PR `prepare.sh` sync is
        # faster. Best-effort: never fail the base build (the Dockerfile calls it
        # with `|| true`), and multi-arch aware — `uv` segfaults under qemu-x86_64,
        # so probe it and fall back to plain pip on the emulated amd64 leg.
        base_install = """#!/bin/bash
set -uo pipefail
cd /home/langgraph 2>/dev/null || exit 0

if uv --version >/dev/null 2>&1; then HAVE_UV=1; else HAVE_UV=0; fi

# Pre-download the common test tooling into pip's cache (arch-correct wheels),
# so the per-PR venvs install them from cache instead of the network.
pip download -q --dest /root/.cache/lg-wheels \\
    pytest pytest-asyncio pytest-mock syrupy pytest-timeout >/dev/null 2>&1 || true

# Best-effort warm of the main package's dependency graph at HEAD. This only
# populates caches; the authoritative per-SHA sync happens in prepare.sh.
if [ "$HAVE_UV" = "1" ] && [ -d libs/langgraph ]; then
  ( cd libs/langgraph && uv sync --all-extras >/dev/null 2>&1 || true )
fi
exit 0
"""
        return [File(".", "base_install.sh", base_install)]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        # Shared base, changedetection/rich style: clone the repo ONCE here (full
        # history) and warm deps via base_install.sh. Each per-PR image then just
        # `git checkout ${BASE_COMMIT}` from this clone and hardens — no re-clone.
        #
        # Keeping FULL history in the shared base is what lets all 60 distinct
        # base SHAs be checked out. It is safe because:
        #   * the `# syntax` directive makes DockerfileEnhancer.enhance() return
        #     this file verbatim, so the enhancer never pins/prunes the clone to a
        #     single commit (and never injects the proxy/MITM/cert infra block);
        #   * the base image itself is NEVER pushed — only the hardened per-PR
        #     images ship, and their prune removes history from the image the
        #     model sees.
        #
        # MULTI-ARCH: no `--platform` pin on FROM, so buildx builds each target
        # arch natively/emulated and the manifest is honest. `uv` segfaults under
        # qemu-x86_64, so base_install.sh (and run_tests.sh) probe it and fall
        # back to pip on the emulated amd64 leg; the amd64 uv wheel is still
        # installed and correct on real amd64 hardware.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} base image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
ENV PIP_ROOT_USER_ACTION=ignore
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    bash \\
    gawk \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    libpq-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Never let git write a commit-graph. A commit-graph built while the full history
# is present survives the per-PR prune and would leak post-base commit metadata
# (an fsck-visible cheat vector) even after the refs are deleted.
RUN git config --global gc.writeCommitGraph false \\
    && git config --global fetch.writeCommitGraph false \\
    && git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo}

COPY base_install.sh /home/base_install.sh
RUN bash /home/base_install.sh || true

{self.clear_env}

CMD ["/bin/bash"]
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

        # Dependency warm-up only. The Dockerfile has already cloned and checked
        # out ${BASE_COMMIT} by the time this runs (see dockerfile() below), and
        # the hardening block that follows asserts HEAD == BASE_COMMIT — so this
        # script must not touch the checkout, only prime the dep cache.
        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
bash /home/check_git_changes.sh

# Warm: sync each package the PR's tests live in (deps cached into the image).
bash /home/run_tests.sh --warm-only || true
""".replace("__REPO__", repo)

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

# Does the `uv` binary actually EXECUTE here? It is a Rust binary and it
# SEGFAULTS under qemu-x86_64, so an emulated amd64 build (multi-arch on an
# arm64 host) cannot run it — even though pip installed the correct amd64
# wheel and it works fine on real amd64 hardware. Probe once; if uv is
# unusable, fall back to a plain pip venv so the image still ships with its
# dependencies installed rather than an empty .venv.
if uv --version >/dev/null 2>&1; then
  HAVE_UV=1
else
  HAVE_UV=0
  echo "### uv is not executable here (emulated build?) — falling back to pip"
fi

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
  elif [ "$HAVE_UV" = "1" ]; then
    uv sync --all-extras --group dev >/dev/null 2>&1 \\
      || uv sync --all-extras >/dev/null 2>&1 \\
      || uv sync >/dev/null 2>&1 \\
      || uv pip install -e . >/dev/null 2>&1 || true
  else
    # pip path: same end state as `uv sync` (an in-project .venv with the
    # package installed editable), just slower. Keeps the emulated amd64 leg
    # functional instead of shipping a cold cache.
    python -m venv .venv >/dev/null 2>&1 || true
    if [ -x .venv/bin/python ]; then
      .venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1 || true
      .venv/bin/python -m pip install -q -e ".[dev]" >/dev/null 2>&1 \\
        || .venv/bin/python -m pip install -q -e . >/dev/null 2>&1 || true
    fi
  fi
  # Top-up test deps, including pytest-timeout for the per-test hang guard.
  # IMPORTANT: uv-created venvs have NO pip, so `.venv/bin/python -m pip` fails
  # on them (the main path). Use `uv pip install` when uv is usable — it installs
  # into the target venv without pip — and fall back to the venv's own pip for
  # poetry-/venv-built environments (emulated amd64 leg, where uv segfaults).
  if [ -x .venv/bin/python ]; then
    if [ "$HAVE_UV" = "1" ]; then
      uv pip install -q --python .venv/bin/python \\
        pytest pytest-asyncio pytest-mock syrupy pytest-timeout \\
        >/dev/null 2>&1 || true
    else
      .venv/bin/python -m pip install -q \\
        pytest pytest-asyncio pytest-mock syrupy pytest-timeout \\
        >/dev/null 2>&1 || true
    fi
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
    # Per-test timeout: a single wedged test is killed at 600s instead of
    # stalling the whole run (two langgraph tests previously blocked a build for
    # 10+ hours at 0% CPU). The `thread` method fires from a separate timer
    # thread even when the test is fully blocked in C/async code, dumps every
    # thread's stack, then hard-exits the process — the PASSED/FAILED lines for
    # tests that already ran are in the -v output above, so parse_log keeps them.
    # Guard: only pass --timeout when pytest-timeout imported OK, else pytest
    # aborts every run with "unrecognized arguments: --timeout".
    TIMEOUT_ARGS=""
    if "$PYBIN" -c "import pytest_timeout" >/dev/null 2>&1; then
      TIMEOUT_ARGS="--timeout=600 --timeout-method=thread"
    fi
    # -o addopts="": discard the pyproject's inherited pytest addopts, which
    # across eras reference plugin-specific flags (e.g. --snapshot-warn-unused)
    # that abort the whole run when that plugin/flag is absent.
    "$PYBIN" -m pytest tests/ -v -rA --continue-on-collection-errors \\
      $TIMEOUT_ARGS -o addopts="" -p no:cacheprovider 2>&1 || true )
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

        # Single COPY of all scripts/patches into /home/ (they land beside the
        # repo, not inside it, so the working tree stays clean for the hardening
        # block's HEAD assertion).
        copy_files = " ".join(file.name for file in self.files())

        # The shared base already CLONED the repo (full history) — so this per-PR
        # image does NOT re-clone; it just checks out ${BASE_COMMIT} from that
        # clone, then hardens. Because this image's dependency() is an Image,
        # DockerfileEnhancer returns the Dockerfile verbatim, so the checkout +
        # hardening below are kept exactly as written.
        #
        # `ARG BASE_COMMIT` + `ENV BASE_COMMIT` is what lets the canonical
        # Image._HARDENING_BLOCK be used VERBATIM below (build args are not passed
        # to Image-dependency builds, so ${BASE_COMMIT} would otherwise be empty).
        #
        # MULTI-ARCH: no `--platform` pin — see LanggraphImageBase.dockerfile.
        # Everything this layer runs at build time (git checkout/gc, apt, pip)
        # works fine under qemu-x86_64; only `uv` segfaults, and install_pkg()
        # falls back to pip when it does.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh || true

"""

        # Anti-reward-hacking hardening — the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + the four post-condition asserts,
        # then the submodule pass). Concatenated raw, NOT through an f-string, so
        # its ${BASE_COMMIT} / %(refname) tokens stay literal.
        #
        # The trailing rm is the one addition beyond the canonical block: the
        # `git reset --hard` above writes .git/ORIG_HEAD, which holds the SHA of
        # the pre-checkout tip (a post-base commit). Its object is gone after the
        # prune and there is no remote left to fetch it from, so it leaks a bare
        # hash rather than content — but there is no reason to ship it.
        tail = f"""
RUN rm -f .git/ORIG_HEAD .git/FETCH_HEAD; \\
    test ! -f .git/ORIG_HEAD

{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


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
