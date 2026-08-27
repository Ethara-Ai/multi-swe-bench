from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# livepeer/go-livepeer  —  Go, but NOT a pure-Go build.
#
# 1. FFMPEG IS MANDATORY, AND IT MUST BE LIVEPEER'S FORK.
#    The graded package `common` imports github.com/livepeer/lpms/ffmpeg, which
#    is CGO. Building it against Debian's ffmpeg dev packages fails at link
#    time (verified in-container):
#        undefined reference to `avfilter_compare_sign_bypath'
#        undefined reference to `avfilter_compare_sign_bybuff'
#    Those symbols exist only in livepeer's FFmpeg fork, which the repo's own
#    install_ffmpeg.sh builds from source (nasm, x264, x265, libvpx, then the
#    pinned fork). Livepeer publish a prebuilt `livepeer/ffmpeg-base` image but
#    it is a single-platform amd64 manifest, so it cannot serve the arm64 half
#    of the multi-arch build — the source build is the only portable option.
#
#    Measured: ~10 min on amd64. The arm64 leg is slower under emulation but
#    works — nasm/x264/x265/libvpx all build on aarch64 and FFmpeg compiles its
#    native aarch64 NEON assembly, so no cross-arch patching is needed.
#
#    It lives in the BASE image on purpose. It is PR-independent and expensive,
#    so paying it once per repo (not once per PR, and not again on every edit to
#    a run script) is the whole point of the base/PR split.
#
# 2. SCOPE IS ./common/... — the only package test.patch touches.
#    Running the repo's full test.sh is not appropriate here: it pulls in
#    network-dependent packages and race-detector reruns whose flakiness would
#    land as a PASS(test) -> FAIL(fix) transition and trip Report.check() rule 2,
#    rejecting the whole instance. `common` holds ParseAccelDevices (the graded
#    function) plus 43 existing tests, which is the p2p body.
#
# 3. EXPECTED SHAPE: n2p, not f2p. test.patch's five new tests reference the
#    package-level seams `getGPU` / `getPCI`, which fix.patch introduces
#    (`var getGPU = getGPUDefault`). At the test stage the package therefore
#    fails to COMPILE, go emits no per-test JSON for it, and the new tests read
#    NONE -> NONE -> PASS. parse_log below turns that compile failure into an
#    explicit `<pkg>::[build]` entry so the stage is never silently empty.
# ---------------------------------------------------------------------------

REPO_DIR = "/home/go-livepeer"
FFMPEG_ROOT = "/root"

# -count=1 disables Go's test result cache: a cached PASS from an earlier stage
# would otherwise be replayed instead of the current tree being exercised.
# 2>&1 folds the compile errors (which go writes to stderr as plain text, not
# JSON) into the same stream parse_log reads, so a build failure is visible.
GO_TEST_CMD = "go test -json -count=1 ./common/... 2>&1"


class GoLivepeerImageBase(Image):
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
        # go.mod declares `go 1.13`, but that is only the language-compat floor.
        # .github/workflows/test.yaml pins actions/setup-go to 1.17, which is
        # what this commit is actually tested against.
        return "golang:1.17-bullseye"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

        # These are install_ffmpeg.sh's build dependencies, taken from the
        # repo's own CI step. clang is required by x265's cmake probe; nasm and
        # yasm are used by the x86 assembler paths and are simply unused on
        # aarch64 (both ship for arm64 on Debian, so one package list serves
        # both arches — no TARGETARCH branching, and nothing arch-pinned).
        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV CI=true
ENV GOFLAGS=-mod=mod
ENV PKG_CONFIG_PATH={FFMPEG_ROOT}/compiled/lib/pkgconfig

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential pkg-config autoconf automake \\
    libtool cmake clang python3 nasm yasm libnuma-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

# lpms/ffmpeg is CGO against livepeer's FFmpeg fork; Debian's libav*-dev does
# not provide avfilter_compare_sign_bypath/_bybuff and fails at link time.
RUN cd {REPO_DIR} && ./install_ffmpeg.sh {FFMPEG_ROOT}

# Warm the module cache so the graded stages do not re-download on every run.
RUN cd {REPO_DIR} && go mod download

{self.clear_env}

"""


class GoLivepeerImageDefault(Image):
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
        return GoLivepeerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                "prepare.sh",
                """#!/bin/bash
set -e

cd {repo_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Pre-build the graded package's dependency tree so the run stage is not
# paying for CGO compilation of lpms/ffmpeg. `|| true` because this is only a
# warm-up: a build error here must surface in the graded stage, where it is
# recorded as a result, rather than killing the image build.
go build ./common/... || true
""".format(repo_dir=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

{gotest}
""".format(repo_dir=REPO_DIR, gotest=GO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

{gotest}
""".format(repo_dir=REPO_DIR, gotest=GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi

{gotest}
""".format(repo_dir=REPO_DIR, gotest=GO_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # prepare.sh needs only itself and check_git_changes.sh, so those are
        # copied first and everything else lands in a cheap layer AFTER the
        # `RUN prepare.sh`. Editing a run script then costs a trivial COPY
        # rebuild instead of re-running the whole prepare step on both arches.
        prepare_inputs = {"prepare.sh", "check_git_changes.sh"}
        pre, post = "", ""
        for file in self.files():
            line = f"COPY {file.name} /home/\n"
            if file.name in prepare_inputs:
                pre += line
            else:
                post += line

        return f"""FROM {name}:{tag}

{self.global_env}

{pre}
RUN bash /home/prepare.sh

{post}
{self.clear_env}

"""


@Instance.register("livepeer", "go-livepeer")
class GO_LIVEPEER(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GoLivepeerImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # `go test -json` emits one JSON object per line. Test-level events
        # carry both Package and Test, so the ID is unique across packages (Go
        # permits the same TestX in many packages) and subtests keep their
        # "TestX/sub" path. Package-level events have no Test field. Nothing
        # variable (elapsed time, output) enters the ID, so the same test parses
        # identically in all three stages.
        pkg_actions: dict[str, str] = {}
        pkg_has_tests: set[str] = set()

        # A package that fails to COMPILE emits no JSON at all — go writes the
        # compile errors to stderr as plain text and closes with
        # "FAIL <pkg> [build failed]". That is exactly this PR's test stage
        # (test.patch calls getGPU/getPCI, which fix.patch adds), so without
        # this branch the stage would be silently empty.
        build_failed_re = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]")

        for raw in log.split("\n"):
            line = raw.strip()
            if not line.startswith("{"):
                m = build_failed_re.match(line)
                if m:
                    failed_tests.add(f"{m.group(1)}::[build]")
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue

            action = ev.get("Action")
            pkg = ev.get("Package")
            test = ev.get("Test")
            if not pkg or action not in ("pass", "fail", "skip"):
                continue

            if not test:
                pkg_actions[pkg] = action
                continue

            pkg_has_tests.add(pkg)
            name = f"{pkg}::{test}"
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A package that failed without producing a single test event did not
        # build (or died in TestMain). Surface it so the stage is not credited
        # with zero tests and mistaken for "the runner never started".
        for pkg, action in pkg_actions.items():
            if action == "fail" and pkg not in pkg_has_tests:
                failed_tests.add(f"{pkg}::[build]")

        # TestResult.__post_init__ rejects overlapping sets; a retried test may
        # appear twice, and failure wins over a later pass, then skip.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
