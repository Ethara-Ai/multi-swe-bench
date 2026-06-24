import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# lazygit v0.61.x (release line 0.61, 2025) targets a recent Go toolchain.
# golang:1.25-bookworm covers the go.mod directive for this era; GOTOOLCHAIN=local
# keeps the hardened (offline) image from trying to fetch a newer toolchain.
_GO_IMAGE = "golang:1.25-bookworm"


# ---------------------------------------------------------------------------
# Build-context scripts (COPY'd into the image, run at build/eval time).
# ---------------------------------------------------------------------------

# Warms the go module + build cache at base.sha so the three eval runs start
# from a compiled state. Runs BEFORE the hardening strip, so it may still see
# the full clone; `|| true` keeps a flaky baseline from breaking the build.
_INSTALL_SH = """#!/bin/bash
set -e

git config --global --add safe.directory /home/lazygit || true
cd /home/lazygit

go mod download || true
go build ./... >/dev/null 2>&1 || true
go test -count=1 ./... >/dev/null 2>&1 || true
"""

# Baseline: clean base.sha, no patches. lazygit's pkg/integration suite needs a
# real TUI/git harness and is excluded from the unit signal (matching the prior
# registry). base.sha is HEAD, so it stays checkout-able after the hardening
# strip.
_RUN_SH = """#!/bin/bash
set -uxo pipefail

cd /home/lazygit
git config --global --add safe.directory /home/lazygit || true
git reset --hard
git checkout {pr.base.sha}

go test -v -count=1 -timeout 30m $(go list ./... | grep -v integration)
"""

# Test patch only: the new tests exercise behaviour the fix has not introduced
# yet, so they fail (or their package fails to compile) -- genuine f2p / n2p.
_TEST_RUN_SH = """#!/bin/bash
set -uxo pipefail

cd /home/lazygit
git config --global --add safe.directory /home/lazygit || true
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn /home/test.patch

go test -v -count=1 -timeout 30m $(go list ./... | grep -v integration)
"""

# Test + fix patches: production fix present, the suite passes.
_FIX_RUN_SH = """#!/bin/bash
set -uxo pipefail

cd /home/lazygit
git config --global --add safe.directory /home/lazygit || true
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn /home/fix.patch
git apply --whitespace=nowarn /home/test.patch

go test -v -count=1 -timeout 30m $(go list ./... | grep -v integration)
"""

# Archive-resilient apt: golang:1.25 is Debian bookworm (currently live). Try a
# normal `apt-get update` first and fall back to archive.debian.org when the
# mirror is eventually retired -- keyed off runtime reachability, not a fixed
# list.
_APT_INSTALL = (
    "RUN { apt-get update 2>/dev/null || "
    "{ sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; "
    "sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list 2>/dev/null; "
    "apt-get update; }; } && \\\n"
    "    apt-get install -y --no-install-recommends \\\n"
    "    ca-certificates \\\n"
    "    curl \\\n"
    "    build-essential \\\n"
    "    git \\\n"
    "    gnupg \\\n"
    "    make \\\n"
    "    python3 \\\n"
    "    sudo \\\n"
    "    wget \\\n"
    "    patch \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)


class LazygitImageBase(Image):
    """Level 1: toolchain-only base image (shared by all PRs of the era).

    ``dependency()`` returns a *string* (the Go toolchain image), so the
    pipeline's ``DockerfileEnhancer`` engages and prepends the
    ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image must NOT clone
    the repository -- a shared string-dependency image that performs a
    ``git clone`` is force-pinned to a single ``${BASE_COMMIT}`` and
    history-stripped by the enhancer, which would break ``git checkout`` for
    every other PR sharing the base. So the clone lives in LazygitImageDefault
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
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

{_APT_INSTALL}

ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=local

CMD ["/bin/bash"]
"""


class LazygitImageDefault(Image):
    """Level 2: per-PR image (built on the shared toolchain base).

    ``dependency()`` returns LazygitImageBase (an Image, not a string), so the
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
        return LazygitImageBase(self.pr, self._config)

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


# PRs in this v0.61 (5487..5508) bundle carry
# number_interval="lazygit_5487_to_12", and Instance.create() routes on
# f"{org}/{number_interval}" -- so the same config must be registered under both
# the default repo key and the interval key.
_NUMBER_INTERVAL = "lazygit_5487_to_12"


@Instance.register("jesseduffield", "lazygit_5487_to_12")
class Lazygit(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LazygitImageDefault(self.pr, self._config)

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

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"^\s*--- PASS: (\S+)"),
            re.compile(r"^ok\s+(\S+)\s+"),
        ]
        re_fail_tests = [
            re.compile(r"^\s*--- FAIL: (\S+)"),
            re.compile(r"^FAIL\s+(\S+)"),
        ]
        re_skip_tests = [
            re.compile(r"^\s*--- SKIP: (\S+)"),
        ]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

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


# Route PRs that carry number_interval="lazygit_5487_to_12" to the same Lazygit
# config (Instance.create looks up f"{org}/{number_interval}").
Instance.register("jesseduffield", _NUMBER_INTERVAL)(Lazygit)

# Also route PRs that carry the dash-joined number_interval (the list of
# prs_in_bundle for the v0.61 5487..5508 bundle) to the same Lazygit config.
_NUMBER_INTERVAL_PRS = "5487-5490-5495-5498-5501-5507-5508"
Instance.register("jesseduffield", _NUMBER_INTERVAL_PRS)(Lazygit)
