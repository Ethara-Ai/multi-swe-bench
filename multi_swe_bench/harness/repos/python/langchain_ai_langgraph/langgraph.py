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
                # Dash-joined EXPLICIT bundle list -- never a range like "146-157",
                # which would wrongly imply every PR in between is included.
                # [146, 147, 150, 155, 157] -> "146-147-150-155-157"
                pr._lg_number_interval = "-".join(
                    str(p).strip() for p in raw["prs_in_bundle"] if str(p).strip()
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
            if not ni:
                # No usable prs_in_bundle on the raw record (absent, empty, or not
                # a langchain-ai/langgraph row). Keep whatever the loader carried;
                # otherwise fall back to the bare PR number, so the output row is
                # NEVER empty -- SOP 11a ("single-PR instance -> just the number")
                # and 11c ("every record non-empty number_interval").
                ni = (ds.number_interval or "").strip() or str(pr.number)
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
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

# Canonical MITM cert symlinks (verbatim from image.py _CERT_SYMLINKS).
# This base is a `# syntax` opt-out so the enhancer never injects them;
# SOP 2a directs adding them by hand. Placed AFTER the apt-get above so
# /etc/ssl/certs/ca-certificates.crt already exists when they are made.
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# SOP 11b: the JSONL and this registry ship together; the trajectory team's
# harness resolves Instance.create() -> f"{org}/{number_interval}", so every
# dash-joined bundle value in the delivered JSONL must be a routing key here.
# Bundle-level (one key per instance); single-era repo -> all point at Langgraph.
# Data-derived: REGENERATE when the delivered bundle set changes.
_BUNDLE_NIS_Langgraph = [
    "1342-1905-2149-2435-2541-2782-2825-2826-2827-2828-2830-2834-2839-2843-2845-2846-2847-2848-2849-2850-2857-2860-2861-2865-2880-2881-2885-2888-2894-2899-2901-2902-2903-2910-2913-2914-2922-2923-2925-2926-2933-2948-2949-2951-2956-2958-2959-2960-2971-2973-2974-2976-2977-2978-2982-2984-2986-2987-2988-2989-2990-2993-2994-2995-3001-3002-3008",
    "2006-3982-4096-4828-4926-4953-4970-5033-5034-5035-5044-5045-5047-5049-5051-5052-5055-5057-5058-5059-5060-5066-5067-5079-5080-5081-5082-5083-5093-5095-5098",
    "2044-3126-3134-3157-3228-3229-3233-3238-3239-3241-3242-3243-3244-3248-3251-3253-3254-3255-3256-3263-3264-3268-3269-3270-3272-3274-3280-3282-3288-3290-3292-3293-3295-3297-3305-3306-3307-3308-3311-3312-3313-3315-3318-3321-3337-3338-3340-3341-3342",
    "2071-2378-2430-2468-2502-2517-2544-2552-2580-2589-2590-2592-2593-2594-2596-2598-2600-2601-2602-2611-2612-2613-2614-2615-2616-2617-2619-2620-2621-2622-2623-2624-2625-2626-2627-2628-2629-2630-2631-2632-2633-2634-2635-2636-2637-2638-2639-2640-2641-2642-2643-2646-2649-2651-2652-2653-2655-2656-2658-2659-2660-2661-2667-2669-2670-2673-2675-2679-2682-2683-2684-2685-2686-2688-2689-2691-2692-2693-2694-2695-2696-2697-2699-2705-2720-2721-2722-2724-2725-2726-2727-2728-2735-2736-2738-2739-2742-2743-2744-2750-2752-2754-2757-2761-2762",
    "2393-2400-2410",
    "2494-2520-2534-2535-2536-2540-2543-2545-2546-2547-2548-2553-2554-2558-2560-2561-2562-2564-2565-2566-2567",
    "2516-3559-3704-3725-3727-3728-3739-3740-3741-3742-3743-3745-3760-3761-3762-3763-3765-3766-3767-3773-3775-3777-3780-3781-3782-3786-3790",
    "2700-2702-2703-2704-2706-2707-2708-2709-2710-2711-2714-2715-2716-2717-2718",
    "3510-3516-3517-3521-3524-3525-3526-3527-3528-3533-3534-3536-3539-3540-3541-3542-3551-3553-3558-3560-3565-3568-3571-3573-3577-3578-3579-3580-3582-3583-3585-3589-3591-3596-3597-3598-3600-3601-3602-3603-3606-3607-3609-3610-3611-3620-3621-3622-3623-3624-3626-3632",
    "3572-3588-3719-3737-3751-3823-3843-3846-3850-3852-3854-3856-3857-3859-3871-3872-3878-3879-3880-3881-3882-3883-3886-3888-3889-3890-3891-3893-3894-3896-3900-3901-3902-3905-3907-3908-3910-3912-3916-3918-3919-3922-3923-3924-3925-3926-3931-3932-3935-3944-3945-3947-3948-3949-3955-3959-3960-3962-3976-3977",
    "4117-4122-4124",
    "5243-5252-5295-5324-5325-5340-5341-5405-5424-5432-5481-5489-5518-5520-5529-5535-5543-5546-5559-5561-5562-5566-5569-5571-5575-5577-5580-5581-5584-5593-5597-5600-5601-5603-5605-5606-5607-5608-5611-5619-5621-5622-5635-5640",
    "6961-6991-6992-7004-7044-7069-7070-7071-7073-7074-7075-7076-7092-7095",
    "7038-7072-7096-7100-7102-7103-7106-7108-7115-7116-7118-7120-7122-7131-7132-7134-7135-7140-7148-7151",
    "7233-7274-7394-7519-7573-7574-7582-7586-7594-7596-7599-7610-7623-7625-7627-7631-7635-7637-7639-7640-7643-7645-7646-7647-7648-7650-7657-7659-7660-7662-7663-7664-7665-7666-7667-7668-7670-7671-7673-7674-7675-7677-7678-7679-7680-7681-7682-7696-7697-7698-7699-7701-7702-7704-7705-7706-7710-7712-7713-7728-7732-7734",
    "7383-7392-7429-7444-7448-7449-7450-7451-7453-7454-7456-7457-7458-7459-7468-7472-7474-7475-7476-7477-7498-7502-7503-7504-7505-7506-7507-7508-7511-7517-7518-7520-7521-7522-7523-7524-7525-7526-7527-7528-7529-7530-7531",
    "7438-7773-7775",
]

for _ni in _BUNDLE_NIS_Langgraph:
    Instance.register("langchain-ai", _ni)(Langgraph)
