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
#     (no --platform pin).  Caveats, accepted intentionally:
#       - PR 502's bundle carries a buggy ARM-NEON `k_quants.c`
#         (`incompatible types ... uint32x4_t` / `vdotq_s32`) that does
#         NOT compile on aarch64 with any modern gcc (verified gcc 12/16).
#         On arm64 that single instance build-fails (run_llama_cpp emits
#         LLAMA_STAGE=FAIL make and exits 0 -> empty TestResult); the other
#         10 PRs build on arm64.  All 11 build on amd64.
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
#     baked into the base image and restored into models/ before the test
#     run.  This takes test-tokenizer-0 from FAIL to PASS (PR 51/109/502/
#     1237 -> 100% of tests pass).
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
    if ! cmake .. ; then
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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \\
    git ca-certificates cmake build-essential python3 \\
    && rm -rf /var/lib/apt/lists/*

{code}

RUN git -C /home/{self.pr.repo} cat-file blob {VOCAB_SRC_COMMIT}:models/ggml-vocab.bin > /home/ggml-vocab.bin

{self.clear_env}

"""


_HARDENING_BLOCK = r"""RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi"""


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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{_HARDENING_BLOCK}

{self.clear_env}

"""


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
