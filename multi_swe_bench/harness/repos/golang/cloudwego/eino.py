
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# cloudwego/eino go.mod pins `go 1.18` across the full PR history (PR 2 .. PR
# 956), but the dependency `github.com/bytedance/sonic` (bumped to v1.13.2 by
# several fix patches) only supports go1.17~1.23: under Go 1.24+ its
# internal/rt/stubs.go references `GoMapIterator`, a runtime internal that was
# removed, so the WHOLE module fails to compile (`undefined: GoMapIterator`).
# That collapsed the fix stage to a build failure and left every report invalid.
# Pin to the newest sonic-compatible toolchain (1.23) and use GOTOOLCHAIN=local
# so a go.mod `toolchain`/`go` directive can never silently auto-fetch a newer,
# sonic-breaking Go. go.mod's `go 1.18` directive is satisfied by local 1.23.
_GO_IMAGE = "golang:1.23-bookworm"


# ---------------------------------------------------------------------------
# Build-context scripts (COPY'd into the image, run at build/eval time).
# ---------------------------------------------------------------------------

# Warms the go module + build cache at base.sha so the three eval runs start from
# a compiled state. Runs BEFORE the hardening strip, so it may still see the full
# clone; `|| true` keeps a flaky baseline from breaking the build.
_INSTALL_SH = """#!/bin/bash
set -e

git config --global --add safe.directory /home/eino || true
cd /home/eino

go mod download || true
go build ./... >/dev/null 2>&1 || true
go test -count=1 ./... >/dev/null 2>&1 || true
"""

# Baseline: clean base.sha, no patches. Full `./...` matches the golden p2p set.
# base.sha stays checkout-able after the hardening strip because it is HEAD
# (reachable, not pruned).
_RUN_SH = """#!/bin/bash
set -uxo pipefail

cd /home/eino
git reset --hard
git checkout {pr.base.sha}

go test -v -count=1 ./...
"""

# Test patch only: the new tests exercise behaviour the fix has not introduced
# yet, so they fail (or their package fails to compile) -- genuine f2p / n2p.
_TEST_RUN_SH = """#!/bin/bash
set -uxo pipefail

cd /home/eino
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.ico' --exclude='*.wasm' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.mp4' --exclude='*.webp' --exclude='*.pdf' --exclude='*.DS_Store' /home/test.patch

go test -v -count=1 ./...
"""

# Test + fix patches: production fix present, the suite passes.
_FIX_RUN_SH = """#!/bin/bash
set -uxo pipefail

cd /home/eino
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.ico' --exclude='*.wasm' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.mp4' --exclude='*.webp' --exclude='*.pdf' --exclude='*.DS_Store' /home/fix.patch
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.ico' --exclude='*.wasm' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.mp4' --exclude='*.webp' --exclude='*.pdf' --exclude='*.DS_Store' /home/test.patch

go test -v -count=1 ./...
"""


class EinoImageBase(Image):
    """Level 1: toolchain-only base image (shared by all PRs).

    ``dependency()`` returns a *string* (the Go toolchain image), so the
    pipeline's ``DockerfileEnhancer`` engages and prepends the
    ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image must NOT clone
    the repository -- a shared string-dependency image that performs a
    ``git clone`` is force-pinned to a single ``${BASE_COMMIT}`` and
    history-stripped by the enhancer, which would break ``git checkout`` for
    every other PR sharing the base. So the clone lives in EinoImageDefault
    (whose dependency() is an Image, left verbatim by the enhancer), done per-PR.
    This image only provides the Go toolchain, apt deps, and Go build env.
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # No `git clone` here on purpose -- see the class docstring. The string
        # dependency means DockerfileEnhancer injects the ARG/ENV/LABEL infra
        # block (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {_GO_IMAGE}

WORKDIR /home/

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
    patch \\
    && rm -rf /var/lib/apt/lists/*

ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=local
RUN git config --global --add safe.directory '*'

CMD ["/bin/bash"]
"""


class EinoImageDefault(Image):
    """Level 2: per-PR image (built on the shared toolchain base).

    ``dependency()`` returns EinoImageBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim -- no pin, no history
    strip injected by the pipeline. The clone therefore lives here, per-PR: the
    image clones full history, checks out ``${BASE_COMMIT}`` inline, COPYs the
    scripts, warms the build cache (install.sh), then the verbatim
    ``Image._HARDENING_BLOCK`` strips origin/refs/future history (with the four
    post-condition asserts + submodule pass) while keeping base.sha reachable.
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
        return EinoImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", _INSTALL_SH),
            File(".", "run.sh", _RUN_SH.format(pr=self.pr)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(pr=self.pr)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(pr=self.pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim -- the clone + hardening below are kept as written
        # (and pinning here is correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/install.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("cloudwego", "eino")
class Eino(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EinoImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        passed_pattern = re.compile(r"--- PASS: (\S+)")
        failed_pattern = re.compile(r"--- FAIL: (\S+)")
        skipped_pattern = re.compile(r"--- SKIP: (\S+)")

        for line in log.splitlines():
            line = line.strip()

            m = passed_pattern.search(line)
            if m:
                if m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))
                continue

            m = failed_pattern.search(line)
            if m:
                passed_tests.discard(m.group(1))
                failed_tests.add(m.group(1))
                continue

            m = skipped_pattern.search(line)
            if m:
                if m.group(1) not in passed_tests and m.group(1) not in failed_tests:
                    skipped_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# Route the dash-joined ``number_interval`` (canonical prs_in_bundle: each
# record's resolved_issues + own PR number, sorted, "-"-joined) of the eino
# LHT dataset to the Eino config. Instance.create() looks up
# f"{org}/{number_interval}" when number_interval is non-empty, so every
# interval string must be registered against this class; Instance.register
# returns the class unchanged, so it answers to the base "eino" key above and
# to every interval below. Sourced from cloudwego__eino_dataset.cleaned.jsonl.
_NUMBER_INTERVALS = [
    "956-990-1000",
    "912-936-941",
    "905-919-924",
    "902-946-958",
    "898-904-906",
    "854-855",
    "847-856-879-886",
    "748-760",
    "694-695",
    "688-719-724-726",
    "682-686",
    "672-691",
    "655-659",
    "646-649",
    "643-650",
    "602-634-657-667-668-673-687",
    "585-587",
    "584-599",
    "560-608-609-611",
    "548-552-553-555-557-558-559-563",
    "533-540",
    "525-620-642",
    "519-828-901-908-917-918",
    "512-539",
    "508-516",
    "496-498-503",
    "449-450",
    "435-436-438",
    "425-457-463-464-466-468-469-470-472-473-475",
    "401-402-411",
    "285-287-288-292",
    "270-276-277",
    "269-403-406-407-408-409",
    "262-417-441-446",
    "242-251-252-253-257",
    "234-235-236",
    "214-218",
    "207-209-213",
    "187-332-341",
    "181-183-190-195-199",
    "167-169",
    "157-164",
    "156-160-162-163",
    "144-145-146-147",
    "138-151-153",
    "137-165-175-176",
    "111-114-116-125-126-131-139-140-141",
    "97-115-117-122-123",
    "96-101-106",
    "86-90-95-102-103",
    "71-72",
    "62-66-67",
    "56-57",
    "29-37-40-42-44-45-46-47-48-49-50-51",
    "28-30-32-33-34-35-36-38-39",
    "25-26",
    "14-15-16-17-18-19-20-21-24",
]

for _ni in _NUMBER_INTERVALS:
    Instance.register("cloudwego", _ni)(Eino)
