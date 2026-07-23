import json as _ggml_json  # underscore-aliased: `import *` must not re-export it
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# ggml-org/llama.cpp  (C/C++, CMake + CTest)
#
# Dataset: 11 bundled instances, base commits 2023-03-18 .. 2023-07-04.
#
# Discovery (interactive Docker, verified for PRs 51, 109, 181, 502, 1087,
# 1237, 1530, 2035, 2234, 2306, 2397):
#
#   * Build system is CMake throughout the range.  `cmake .. && make &&
#     ctest` is the canonical flow; the bundled fix_patch modernises every
#     base (even the cmake-3.8 / C++20 ones) to cmake-3.12 / C++11, so a
#     single toolchain covers all eras -- no era split is needed.
#
#   * Multi-arch (linux/amd64 + linux/arm64), built natively per platform
#     (no --platform pin).  Caveats:
#       - PR 502's bundle carries a buggy ARM-NEON `k_quants.c`
#         (`incompatible types ... uint32x4_t` / `vdotq_s32`) that does not
#         compile on aarch64 with modern gcc's strict NEON vector-type
#         checking (verified gcc 12/16).  `-flax-vector-conversions` (GCC's
#         own diagnostic suggests this exact flag) restores the pre-gcc-12
#         implicit-conversion behavior and lets it build; passed globally
#         via CMAKE_C_FLAGS below since it only affects NEON code paths and
#         is a no-op for the other 10 PRs / on amd64.
#       - llama.cpp is SIMD quantization code: test-sampling /
#         test-quantize-* / test-tokenizer can yield different pass/fail
#         on AVX vs NEON, so arm64 results may diverge from amd64.  The
#         original dataset's f2p labels were established on x86_64.
#
#   * gcc:12 is the contemporary compiler for mid-2023 llama.cpp.
#
#   * The dataset's fix_patch / test_patch carry binary files as plain
#     "Binary files ... differ" stubs with NO payload (no `GIT binary
#     patch` blob).  `git apply` is atomic, so those stubs abort the whole
#     patch.  strip_bin.py drops binary-only file sections; the C/C++/CMake
#     source then applies cleanly for all 11 PRs (verified `git apply
#     --check`).
#
#   * The stripped binaries include models/ggml-vocab.bin, which
#     test-tokenizer-0 needs at runtime.  That blob is identical (432610 B)
#     across the whole era and is never modified, so the canonical copy is
#     extracted once into /home/ggml-vocab.bin (outside the work tree) and
#     restored into models/ before the test run.  This takes test-tokenizer-0
#     from FAIL to PASS (PR 51/109/502/1237 -> 100% of tests pass).
#     The extraction lives in the per-PR image and must run BEFORE the
#     hardening block: VOCAB_SRC_COMMIT is unreachable from most base commits,
#     so the gc --prune=now in hardening deletes it.
#
#   * The gguf-beta bundles (PR 2397 range) reference models/
#     ggml-vocab-llama.gguf with an early/incompatible gguf magic; those
#     test-tokenizer-*.llama cases fail at runtime identically in
#     test_patch_run and fix_patch_run (not a fix-to-pass signal) and are
#     left as-is -- the build still succeeds and ctest still reports.
#
# Captured ctest summary lines parse_log keys on (real output):
#   1/5 Test #1: test-quantize-fns ................   Passed    0.00 sec
#   3/4 Test #3: test-sampling ...........Subprocess aborted***Exception:   0.29 sec
#   4/5 Test #4: test-tokenizer-0 .................***Failed    0.00 sec
#   1/4 Test #1: test-quantize-fns ................***Not Run   0.00 sec
#   4/8 Test #4: test-tokenizer-0.llama ...........***Failed    0.00 sec
# ---------------------------------------------------------------------------

# models/ggml-vocab.bin is stable (432610 B) and never modified across the
# whole dataset era; this commit (PR 1237 base) carries the canonical copy.
VOCAB_SRC_COMMIT = "acc111caf93fc6681450924df9f99679c384c59e"

# Drop binary-only file diff sections ("Binary files ... differ" / "GIT
# binary patch") so the otherwise-atomic `git apply` does not abort on
# binary stubs that carry no payload.
STRIP_BIN_PY = r'''import sys

src = open(sys.argv[1], "rb").read().decode("utf-8", "replace").split("\n")
out, i, n = [], 0, len(src)
while i < n:
    line = src[i]
    if line.startswith("diff --git "):
        j = i + 1
        while j < n and not src[j].startswith("diff --git "):
            j += 1
        section = src[i:j]
        is_bin = any(
            s.startswith("Binary files ") or s.startswith("GIT binary patch")
            for s in section
        )
        if not is_bin:
            out.extend(section)
        i = j
    else:
        out.append(line)
        i += 1
sys.stdout.write("\n".join(out))
'''

# Shared shell helper: strip binaries, apply the given patch(es), restore
# the vocab model, then build + test.  Always exits 0 so the harness always
# captures a parseable ctest log (failing/empty ctest just => 0 passed).
BUILD = r"""
run_llama_cpp() {
    set -o pipefail
    cd /home/llama.cpp || { echo "LLAMA_STAGE=FAIL no-repo"; return 0; }

    for p in "$@"; do
        python3 /home/strip_bin.py "$p" > "${p}.clean"
        if ! git apply --whitespace=nowarn "${p}.clean"; then
            echo "LLAMA_STAGE=FAIL apply ${p}"
            return 0
        fi
    done

    mkdir -p models
    cp -f /home/ggml-vocab.bin models/ggml-vocab.bin 2>/dev/null || true

    rm -rf build
    mkdir build
    cd build
    if ! cmake -DCMAKE_C_FLAGS=-flax-vector-conversions .. ; then
        echo "LLAMA_STAGE=FAIL cmake"
        return 0
    fi
    if ! make -j"$(nproc)" ; then
        echo "LLAMA_STAGE=FAIL make"
        return 0
    fi
    ctest --output-on-failure || true
    echo "LLAMA_STAGE=DONE"
    return 0
}
"""


class LlamaCppImageBase(Image):
    """Level 1: toolchain-only base image (SINGLE, shared across all PRs).

    dependency() returns a *string* ("gcc:12"), which would normally make the
    DockerfileEnhancer inject its infra block (proxy ARGs, CA-cert symlink farm,
    MITM secret mount).  This Dockerfile carries the BuildKit ``# syntax``
    directive instead, so ``enhance()`` returns it UNCHANGED (image.py:
    ``if SYNTAX_DIRECTIVE in raw: return raw``) -- NO proxy / cert / MITM
    injection, and image.py is left untouched.  The ARG/ENV/LABEL infra the
    enhancer would have supplied is written out here instead.  Same opt-out
    pattern as Radare2ImageBase.

    IMPORTANT: this image must NOT clone the repository.  image_tag() is the
    constant "base", so exactly one base image is shared by all 11 PRs -- but
    run_evaluation passes ``BASE_COMMIT=<that PR's base.sha>`` as a build arg to
    every string-dependency image.  If the clone lived here, the enhancer's
    _standardize_repo_fetch would rewrite it into clone + checkout ${BASE_COMMIT}
    + the hardening block, force-pinning the ONE shared base to whichever PR
    happened to build it and gc-pruning every other PR's base commit out of the
    object store.  The other 10 PRs' `git checkout <sha>` would then fail.

    So the clone lives in LlamaCppImageDefault (an Image dependency, left
    verbatim by the enhancer), done per-PR.  This image only provides the
    C/CMake build toolchain.  Same rule as FluidSynthImageBase.
    """

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
        return "gcc:12"

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

        org, repo = self.pr.org, self.pr.repo

        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this file verbatim, so no proxy/cert/MITM block is injected; the
        # image uses gcc:12's own CA trust store and ambient network settings.
        #
        # No `git clone` here on purpose (see class docstring) -- the repo is
        # cloned per-PR in LlamaCppImageDefault, so this shared base is never
        # pinned to a single ${BASE_COMMIT}.
        #
        # dependency() is a string, so build_dataset/run_evaluation pass the
        # REPO_URL/BASE_COMMIT build-args; both are declared (REPO_URL baked with
        # its default) purely to consume them, otherwise Docker warns that the
        # build-args went unused.
        repo_url = f"https://github.com/{org}/{repo}.git"
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git ca-certificates cmake build-essential python3 \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

CMD ["/bin/bash"]
"""


class LlamaCppImageDefault(Image):
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
        return LlamaCppImageBase(self.pr, self._config)

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
                "strip_bin.py",
                STRIP_BIN_PY,
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

if [[ -n $(git status --porcelain --ignore-submodules=all) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -ffdx
git checkout {pr.base.sha}

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true
{build}
cd /home/{repo}
git reset --hard
git clean -ffdx
git checkout {sha} || {{ echo "LLAMA_STAGE=FAIL checkout"; exit 0; }}
run_llama_cpp
""".format(build=BUILD, repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true
{build}
cd /home/{repo}
git reset --hard
git clean -ffdx
git checkout {sha} || {{ echo "LLAMA_STAGE=FAIL checkout"; exit 0; }}
run_llama_cpp /home/test.patch
""".format(build=BUILD, repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true
{build}
cd /home/{repo}
git reset --hard
git clean -ffdx
git checkout {sha} || {{ echo "LLAMA_STAGE=FAIL checkout"; exit 0; }}
run_llama_cpp /home/test.patch /home/fix.patch
""".format(build=BUILD, repo=self.pr.repo, sha=self.pr.base.sha),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first.  Because this image's dependency() is an Image, the
        # DockerfileEnhancer returns this Dockerfile verbatim -- the clone and the
        # hardening below are kept as written, and pinning to ${BASE_COMMIT} here
        # is correct: it is per-PR, not the shared base.
        #
        # Ordering is load-bearing: the vocab blob is extracted while full history
        # is still present, because VOCAB_SRC_COMMIT is unreachable from most base
        # commits and the hardening block's `gc --prune=now` deletes it.  The blob
        # lands in /home/ (outside the work tree), so it survives hardening and the
        # `git clean -ffdx` in the run scripts.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

RUN git -C /home/{self.pr.repo} cat-file blob {VOCAB_SRC_COMMIT}:models/ggml-vocab.bin > /home/ggml-vocab.bin

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal, and so this registry tracks the harness block
        # instead of carrying a fork of it.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("ggml-org", "llama.cpp")
class LlamaCpp(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LlamaCppImageDefault(self.pr, self._config)

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
        # Strip ANSI color codes before parsing.
        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # CTest per-test summary line, e.g.:
        #   1/5 Test #1: test-quantize-fns ......   Passed    0.00 sec
        #   3/4 Test #3: test-sampling ...Subprocess aborted***Exception:   0.29 sec
        #   4/5 Test #4: test-tokenizer-0 ......***Failed    0.00 sec
        #   1/4 Test #1: test-quantize-fns ......***Not Run   0.00 sec
        ctest_re = re.compile(
            r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(\S+?)\s*\.{2,}\s*(.+?)\s+[\d.]+\s+sec\s*$"
        )

        for line in clean_log.splitlines():
            m = ctest_re.match(line)
            if not m:
                continue
            name = m.group(1)
            status = m.group(2)
            if "Passed" in status:
                passed_tests.add(name)
            elif "Skipped" in status or "Disabled" in status:
                skipped_tests.add(name)
            else:
                # Failed / ***Exception / Subprocess aborted / Timeout / Not Run
                failed_tests.add(name)

        # Enforce TestResult invariants (a name must not appear in more than
        # one bucket).  A failure outranks a pass which outranks a skip.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval auto-population -- REGISTRY-SCOPED shim (no other file edited).
#
# `number_interval` must enumerate the EXACT PRs in the bundle, dash-joined:
#
#     prs_in_bundle [146, 147, 150, 155, 157]  ->  "146-147-150-155-157"
#
# NOT a "146-157" range: a range implies every PR from 146 to 157, which is not
# what the bundle contains.
#
# The output dataset/report jsonl writes `number_interval` from the loaded
# PullRequest (dataset.Dataset.build and gen_report's dataset lookup both read
# `pr.number_interval`), but PullRequest has no `prs_in_bundle` field, so
# from_json drops the bundle list and leaves number_interval "". As this must
# live ONLY in the registry, two small, idempotent, ggml-org-scoped shims are
# installed at import time (this file is the only one changed):
#
#   1. PullRequest.from_json -- for ggml-org/llama.cpp records, derive
#      number_interval from the raw line's prs_in_bundle. This runs on both
#      parse paths that matter: build_dataset (-> dataset jsonl) and gen_report
#      (-> resolved/report jsonl).
#
#      prs_in_bundle is treated as AUTHORITATIVE whenever present, so a legacy
#      range-format value ("146-157") is rewritten to the enumerated list. That
#      is the one deliberate difference from the radareorg shim, which only
#      fills EMPTY values: radareorg shares its org with era-keyed registry keys
#      whose pre-set number_interval must survive, whereas ggml-org registers
#      exactly one key ("ggml-org/llama.cpp"), so there is no era value to
#      preserve and correcting stale ranges is safe. Records with no
#      prs_in_bundle are left untouched.
#
#   2. Instance.create -- a non-empty number_interval makes routing look up
#      `ggml-org/<that-list>`, which is not a registered key; fall back to
#      `ggml-org/llama.cpp` so the build still routes. Other repos are
#      unaffected: shim 1 only fills ggml-org, and the fallback re-raises for
#      anything else.
#
# Both shims chain safely with the equivalent radareorg shims (each wraps the
# previous callable and re-raises for orgs it does not own), regardless of
# module import order.
# ---------------------------------------------------------------------------
_GGML_ORG = "ggml-org"
_GGML_REPO = "llama.cpp"


def _ggml_interval_from_bundle(json_str: str) -> str:
    """Dash-join prs_in_bundle -> "146-147-150-155-157" ("" when absent)."""
    prs = (_ggml_json.loads(json_str) or {}).get("prs_in_bundle") or []
    # Sort numerically so ordering matches the collector (build_lht_dataset
    # joins sorted_pr_numbers) and is stable regardless of the raw line's order.
    return "-".join(str(p) for p in sorted(prs, key=int))


if not getattr(PullRequest, "_ggml_org_ni_shim", False):
    _ggml_orig_from_json = PullRequest.from_json.__func__

    def _ggml_from_json(cls, json_str):
        pr = _ggml_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == _GGML_ORG
                and getattr(pr, "repo", "") == _GGML_REPO
            ):
                interval = _ggml_interval_from_bundle(json_str)
                if interval:
                    pr.number_interval = interval
        except Exception:
            # Never let metadata enrichment break dataset parsing.
            pass
        return pr

    PullRequest.from_json = classmethod(_ggml_from_json)
    PullRequest._ggml_org_ni_shim = True


if not getattr(Instance, "_ggml_org_route_shim", False):
    _ggml_orig_create = Instance.create.__func__

    def _ggml_create(cls, pr, config, *args, **kwargs):
        try:
            return _ggml_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _GGML_ORG
                and getattr(pr, "repo", "") == _GGML_REPO
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_ggml_create)
    Instance._ggml_org_route_shim = True
