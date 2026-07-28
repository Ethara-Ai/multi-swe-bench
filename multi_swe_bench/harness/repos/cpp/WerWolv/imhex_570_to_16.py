"""WerWolv/ImHex -- era 1 (gcc-11 / C++20, bundles anchored at PR 16..570).

Where the anti-reward-hacking hardening lives, and why:

  * ``ImHexEra1ImageBase`` is tagged ``:base-cxx20`` -- ONE image shared by all
    16 era-1 bundles.  It must therefore keep the repository's FULL history,
    because each bundle checks out its own ``pr.base.sha``.  Hardening it (which
    detaches at a single ``${BASE_COMMIT}`` and prunes everything else) would
    delete the base.sha of the other 15 bundles, and their prepare.sh would then
    die on ``git checkout <sha>`` with exit 128.
  * ``ImHexEra1ImageDefault`` is tagged ``:pr-<n>`` -- one image per bundle.  Its
    prepare.sh checks out this bundle's base.sha and initialises submodules, and
    only THEN does the canonical ``Image._HARDENING_BLOCK`` run, pinned to that
    sha.  This is the image handed to the model, so this is where history ends.

Both Dockerfiles carry the ``# syntax=docker/dockerfile:1.6`` directive so
``DockerfileEnhancer.enhance()`` returns them verbatim (image.py:317).  That is
what keeps the enhancer from injecting hardening into the shared base -- but it
also means the rest of the enhancer's infra is spelled out here: OCI labels and
the ``${REPO_URL}`` clone.  No proxy ARG/ENV, no CA-bundle symlinks and no MITM
CA secret mount are emitted -- matching the enhancer after that logic was removed
from image.py; builds use the ambient network and the default Debian trust store
shipped by the ca-certificates package.

``Image._HARDENING_BLOCK`` is referenced, never copied, so future edits to
image.py propagate here automatically.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Shared by test-run.sh and fix-run.sh in BOTH eras (era 2 imports this symbol).
#
# Why this exists: the ImHex dataset's fix_patch/test_patch were generated
# WITHOUT `git diff --binary`, so every binary change (icons, fonts) is a bare
# `Binary files ... differ` marker with no delta -- 0 sections carry a real
# `GIT binary patch`. `git apply` correctly refuses those, and because it applies
# atomically, one bad binary hunk would abort every text hunk beside it.
#
# The prior applier hid that: `git apply --reject ... 2>/dev/null || true` writes
# .rej files, silences the error and swallows the exit code, so an incomplete or
# fully-failed patch still ran the tests and could pass as green. This function
# replaces that with a LOUD applier:
#   * it strips the marker-only binary sections so the text hunks can still apply;
#   * it PRINTS every dropped path ("[patch] ... DROPPED n binary file(s)"), so an
#     incomplete fix is visible in the eval log instead of passing silently;
#   * it FAILS hard (return 1) if the remaining text hunks do not apply -- no
#     `--reject` fallback, because that "succeeds" while leaving .rej behind, the
#     exact silent pass this removes.
#
# For ImHex the dropped paths are inert assets (fonts/icons), so the C++
# unit_tests build is unaffected; the drop is logged, not silently correct.
# This is a STOPGAP diagnostic: it cannot recreate bytes the dataset never
# carried. Delete it once fix_patch is regenerated with `git diff --binary`.
#
# NOTE: injected via .format(apply_fn=...); its awk braces are a substituted
# VALUE, not part of the format template, so they must NOT be doubled.
_APPLY_PATCH_FN = r'''apply_patch_tolerant() {
    local patch="$1" label="${2:-$1}"
    if [ ! -s "$patch" ]; then
        echo "[patch] $label: empty, nothing to apply"
        return 0
    fi

    local filtered dropped
    filtered=$(mktemp)
    dropped=$(mktemp)

    # Buffer each per-file section in full and emit it only if it carries no
    # binary marker. Buffering matters: emitting line-by-line leaves orphan
    # "diff --git"/"index" headers with no body, which git apply then treats as
    # an empty-file creation.
    awk -v dropfile="$dropped" '
      BEGIN { n = 0; bin = 0; path = "?" }
      function flush(   i) {
          if (n == 0) return
          if (bin) { print path > dropfile }
          else     { for (i = 1; i <= n; i++) print buf[i] }
          n = 0; bin = 0; path = "?"
      }
      /^diff --git / { flush(); path = $4; sub(/^b\//, "", path) }
      /^GIT binary patch/ { bin = 1 }
      /^Binary files / { bin = 1 }
      { buf[++n] = $0 }
      END { flush(); close(dropfile) }
    ' "$patch" > "$filtered"

    local ndrop=0
    if [ -s "$dropped" ]; then ndrop=$(wc -l < "$dropped"); fi
    if [ "$ndrop" -gt 0 ]; then
        echo "[patch] $label: DROPPED $ndrop binary file(s) -- dataset patch has"
        echo "[patch]   'Binary files ... differ' markers with no delta, so this"
        echo "[patch]   patch is applied INCOMPLETELY:"
        sed 's/^/[patch]     - /' "$dropped"
    fi

    local rc=0
    if [ -s "$filtered" ]; then
        if git apply --whitespace=nowarn "$filtered"; then
            echo "[patch] $label: text hunks applied cleanly ($ndrop binary dropped)"
        elif git apply --whitespace=nowarn --3way "$filtered"; then
            echo "[patch] $label: text hunks applied via --3way ($ndrop binary dropped)"
        else
            echo "[patch] $label: ERROR text hunks FAILED to apply" >&2
            rc=1
        fi
    else
        echo "[patch] $label: nothing left after dropping binary sections" >&2
        rc=1
    fi

    rm -f "$filtered" "$dropped"
    return $rc
}'''

# One entry per era-1 bundle: prs_in_bundle joined with "-" exactly as the
# dataset stores it (an explicit anchor-first list, NOT a first-to-last range).
# Instance.create() looks up f"{org}/{pr.number_interval}", so these strings are
# the routing keys. Generated from WerWolv__ImHex_lht_final.jsonl.
_BUNDLE_NIS = [
    "16-29-33-35-38",
    "31-47-53-59-69-76-79-85-86-95-100-102-111-119",
    "82-138-152-153-156",
    "120-121-126-132",
    "176-178-179-181-185-186-187-191-193-196-199-215-223",
    "232-299-303-305-306",
    "255-257-258-274-278-281",
    "316-317-319-321-325-327-332-335-338-339-343-345-347-348",
    "377-380-382-395",
    "399-401-404-406",
    "411-416-417-418-419-425-426-433-434-435-437-439-440-441",
    "443-444-445-446-447-448-451-457-458",
    "463-464-466-472-474-475-477-482-483-487",
    "502-505-509-510-512-513",
    "524-526-529-530-531-537-546-548-550-552-553-556-558-559-562-564-566-567",
    "570-572-573-578-579",
]


class ImHexEra1ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-cxx20"

    def workdir(self) -> str:
        return "base-cxx20"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        # The clone is unconditional (no `COPY {repo}` branch): build_dataset
        # skips copy_source_code whenever dependency() is a string, which it is
        # here, so a COPY would reference a path that is never staged into the
        # build context. REPO_URL is passed as a build arg for the same reason
        # (build_dataset.py:616) and the ARG default covers a direct docker build.
        #
        # Deliberately NO `git checkout` and NO hardening in this layer -- see the
        # module docstring. Full history stays, per-bundle pruning happens in
        # ImHexEra1ImageDefault.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        build-essential gcc-11 g++-11 lld pkg-config cmake ccache ninja-build make \\
        git ca-certificates python3 python3-dev perl autoconf automake libtool \\
        libglfw3-dev libglm-dev libmagic-dev libmbedtls-dev libfreetype-dev \\
        libdbus-1-dev libcurl4-gnutls-dev libgtk-3-dev libssl-dev libcrypto++-dev \\
        nlohmann-json3-dev libcapstone-dev libyara-dev libcli11-dev libfmt-dev \\
        zlib1g-dev libbz2-dev liblzma-dev libzstd-dev libpsl-dev \\
        libssh2-1-dev libidn2-dev libnghttp2-dev librtmp-dev libkrb5-dev libldap2-dev \\
        libarchive-dev liblz4-dev libmd4c-dev libmd4c-html0-dev libfontconfig-dev llvm-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-11 100 && \\
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-11 100

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

"""


class ImHexEra1ImageDefault(Image):
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
        return ImHexEra1ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

""",
            ),
            File(
                ".",
                "init_submodules.sh",
                """#!/bin/bash
# Robust submodule init.
# Standard `git submodule update --init` silently swallows failures (network
# under QEMU, deleted submodule refs, etc.). When a submodule dir stays empty
# (or worse, has only a .git gitfile pointer and no real content), downstream
# cmake fails with "External dependency ... is empty".
#
# Strategy:
#   1) git submodule sync (refresh URL state)
#   2) git submodule update --init --recursive --force --depth=1 (twice, swallow errors)
#   3) For every .gitmodules entry, check if the path is empty *of real content*
#      (any non-hidden file/dir). If empty, rm -rf and explicit clone from URL,
#      then recursively init the clone's own submodules.
set +e

# Single pass that processes a .gitmodules-bearing directory.
init_one_repo() {
  local here="$1"
  ( cd "$here" 2>/dev/null || return 0
    [ ! -f .gitmodules ] && return 0
    git submodule sync --recursive 2>&1
    git submodule update --init --recursive --force --depth=1 2>&1
    git submodule update --init --recursive --force --depth=1 2>&1
    git config --file .gitmodules --get-regexp '^submodule\\..*\\.path$' 2>/dev/null | while read key path; do
      name=$(echo "$key" | sed 's/^submodule\\.//; s/\\.path$//')
      url=$(git config --file .gitmodules --get "submodule.${name}.url" 2>/dev/null)
      # "Empty of real content" = no non-hidden entries (ls without -A ignores dotfiles).
      # A populated submodule will have CMakeLists.txt or src/ etc.; a stale .git pointer alone
      # leaves ls "" so the fallback fires.
      visible_count=$(ls -1 "$path" 2>/dev/null | wc -l)
      if [ -n "$url" ] && { [ ! -d "$path" ] || [ "$visible_count" = "0" ]; }; then
        echo "init_submodules: fallback clone $url -> $here/$path"
        rm -rf "$path"
        git clone --depth=1 --recursive "$url" "$path" 2>&1 \\
          || git clone --recursive "$url" "$path" 2>&1 \\
          || true
        # Recurse into the newly cloned dir to init ITS nested submodules.
        init_one_repo "$path"
      fi
    done
  )
}

init_one_repo "$(pwd)"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
git clean -fdx -e build
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/init_submodules.sh
bash /home/check_git_changes.sh
mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
bash /home/init_submodules.sh
cd build
cmake -GNinja \\
  -DCMAKE_BUILD_TYPE=Release \\
  -DCMAKE_C_COMPILER=gcc-11 \\
  -DCMAKE_CXX_COMPILER=g++-11 \\
  -DIMHEX_ENABLE_UNIT_TESTS=ON \\
  -DIMHEX_OFFLINE_BUILD=ON \\
  -DIMHEX_STRICT_WARNINGS=OFF \\
  -DIMHEX_IGNORE_BAD_COMPILER=ON \\
  -DIMHEX_BUNDLE_DOTNET=OFF \\
  -DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON \\
  ..
if ! cmake --build . -j $(nproc) --target unit_tests; then
  cmake --build . -j $(nproc)
fi
ctest --output-on-failure
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

{apply_fn}

apply_patch_tolerant /home/test.patch test.patch || exit 1
bash /home/init_submodules.sh
cd build
cmake -GNinja \\
  -DCMAKE_BUILD_TYPE=Release \\
  -DCMAKE_C_COMPILER=gcc-11 \\
  -DCMAKE_CXX_COMPILER=g++-11 \\
  -DIMHEX_ENABLE_UNIT_TESTS=ON \\
  -DIMHEX_OFFLINE_BUILD=ON \\
  -DIMHEX_STRICT_WARNINGS=OFF \\
  -DIMHEX_IGNORE_BAD_COMPILER=ON \\
  -DIMHEX_BUNDLE_DOTNET=OFF \\
  -DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON \\
  .. || true
if ! cmake --build . -j $(nproc) --target unit_tests -- -k 0; then
  cmake --build . -j $(nproc) -- -k 0 || true
fi
ctest --output-on-failure

""".format(pr=self.pr, apply_fn=_APPLY_PATCH_FN),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

{apply_fn}

apply_patch_tolerant /home/test.patch test.patch || exit 1
apply_patch_tolerant /home/fix.patch  fix.patch  || exit 1
# Submodule init AFTER patches: a PR may add a new submodule (e.g. external/
# libromfs) to .gitmodules, and plain `git submodule update` cannot fetch it
# because git apply does not write the gitlink into the index. init_submodules.sh
# has a .gitmodules-driven fallback that clones such empty/new submodules by URL.
bash /home/init_submodules.sh
cd build
cmake -GNinja \\
  -DCMAKE_BUILD_TYPE=Release \\
  -DCMAKE_C_COMPILER=gcc-11 \\
  -DCMAKE_CXX_COMPILER=g++-11 \\
  -DIMHEX_ENABLE_UNIT_TESTS=ON \\
  -DIMHEX_OFFLINE_BUILD=ON \\
  -DIMHEX_STRICT_WARNINGS=OFF \\
  -DIMHEX_IGNORE_BAD_COMPILER=ON \\
  -DIMHEX_BUNDLE_DOTNET=OFF \\
  -DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON \\
  .. || true
if ! cmake --build . -j $(nproc) --target unit_tests -- -k 0; then
  cmake --build . -j $(nproc) -- -k 0 || true
fi
ctest --output-on-failure

""".format(pr=self.pr, apply_fn=_APPLY_PATCH_FN),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # The shared base carries the full clone; prepare.sh checks out this
        # bundle's base.sha and populates submodules. Harden AFTER that, so the
        # prune is pinned to this bundle's commit and the submodule pass has real
        # submodule worktrees to strip. BASE_COMMIT is declared as an ARG
        # defaulted to the sha because build_dataset only passes build args when
        # dependency() is a string and ours is an Image (build_dataset.py:616);
        # the default keeps Image._HARDENING_BLOCK usable verbatim instead of
        # forking a literal-sha copy of it.
        #
        # The syntax directive makes DockerfileEnhancer.enhance() a no-op here
        # (image.py:317), which is why CMD is emitted explicitly.
        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("WerWolv", "imhex_570_to_16")
class IMHEX_570_TO_16(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImHexEra1ImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]
        re_skip_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Skipped\s*.*$"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    passed_tests.add(pass_match.group(1).strip())

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1).strip())

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    skipped_tests.add(skip_match.group(1).strip())

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


# Instance.create() routes on f"{org}/{number_interval}". The "imhex_570_to_16"
# key above only matches a record with tag="570_to_16", which this dataset does
# not set, so register every era-1 bundle interval against the same class.
for _ni in _BUNDLE_NIS:
    Instance.register("WerWolv", _ni)(IMHEX_570_TO_16)


# ---------------------------------------------------------------------------
# Routing shims for WerWolv/ImHex (serve BOTH era modules; installed here
# because imhex_570_to_16 is imported first by the package __init__).
#
# WerWolv__ImHex_lht_final.jsonl predates the number_interval field: it carries
# prs_in_bundle but neither number_interval nor tag. PullRequest.from_json
# therefore yields number_interval="" and tag="", Instance.create() looks up
# "WerWolv/ImHex", and every one of the 34 records fails to route -- one key
# cannot serve two eras anyway (gcc-11/C++20 vs gcc-12/C++23).
#
# Two idempotent, WerWolv/ImHex-scoped shims fix that in the registry alone,
# following the same pattern as repos/c/radareorg/radare2.py:
#
#   1. PullRequest.from_json -- when number_interval is empty, fill it from the
#      raw line's prs_in_bundle as the dash-joined EXPLICIT list
#      ("16-29-33-35-38", never a 16-38 range). That value also flows into the
#      generated dataset record via dataset.py.
#   2. Instance.create -- if an interval key is somehow absent (dataset
#      regenerated with different bundling), fall back to the era class implied
#      by the anchor PR number instead of dying. The era keys are looked up
#      lazily at call time, so imhex_1673_to_580 need not be imported yet.
#
# Other repos are untouched: both shims check org/repo first, and era-keyed
# datasets that already set number_interval are left alone.
# ---------------------------------------------------------------------------
import json as _imhex_json  # noqa: E402

from multi_swe_bench.harness.pull_request import (  # noqa: E402
    PullRequest as _ImHexPullRequest,
)

# Highest anchor PR handled by era 1; anything above belongs to era 2.
_ERA1_MAX_ANCHOR = 570

if not getattr(_ImHexPullRequest, "_werwolv_ni_shim", False):
    _imhex_orig_from_json = _ImHexPullRequest.from_json.__func__

    def _imhex_from_json(cls, json_str):
        pr = _imhex_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "WerWolv"
                and getattr(pr, "repo", "") == "ImHex"
                and not getattr(pr, "number_interval", "")
            ):
                prs = (_imhex_json.loads(json_str) or {}).get("prs_in_bundle") or []
                if prs:
                    pr.number_interval = "-".join(str(p) for p in prs)
        except Exception:
            pass
        return pr

    _ImHexPullRequest.from_json = classmethod(_imhex_from_json)
    _ImHexPullRequest._werwolv_ni_shim = True

if not getattr(Instance, "_werwolv_route_shim", False):
    _imhex_orig_create = Instance.create.__func__

    def _imhex_create(cls, pr, config, *args, **kwargs):
        try:
            return _imhex_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == "WerWolv"
                and getattr(pr, "repo", "") == "ImHex"
            ):
                era = (
                    "imhex_570_to_16"
                    if pr.number <= _ERA1_MAX_ANCHOR
                    else "imhex_1673_to_580"
                )
                name = f"WerWolv/{era}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_imhex_create)
    Instance._werwolv_route_shim = True
