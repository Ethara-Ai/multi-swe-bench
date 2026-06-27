import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# XTLS/Xray-core era "xray_core_6077_to_4584": build/test on the golang:1.26 toolchain.
_GO_IMAGE = "golang:1.26"


# ---------------------------------------------------------------------------
# Build-context scripts (COPY'd into the per-PR image, run at build/eval time).
# ---------------------------------------------------------------------------

# Warms the go module + build cache at base.sha and fetches the geo data the
# app/router, app/dns, infra/conf and common/geodata tests need. Runs BEFORE
# the hardening strip; everything is `|| true` so a flaky baseline (or a
# transient geo-data download failure) never breaks the image build.
#
# Geo data goes into <repo>/resources -- this exactly mirrors upstream CI
# (.github/workflows/test.yml restores a `resources` cache then runs
# `go test ./...` with NO XRAY_LOCATION_ASSET). The infra/conf test init() is
# written for this layout: it copies geoip.dat from <repo>/resources and then
# sets xray.location.asset itself, per test binary. Crucially we do NOT export
# XRAY_LOCATION_ASSET globally -- doing so leaks into the common/platform test
# binary and breaks TestGetAssetLocation, which asserts the default asset dir
# equals the executable dir when no override is set. *.dat is gitignored, so the
# files stay untracked: they do not dirty the worktree and survive both
# `git reset --hard` and the hardening gc (untracked objects are not pruned).
_INSTALL_SH = """#!/bin/bash
set -e

git config --global --add safe.directory /home/{pr.repo} || true
cd /home/{pr.repo}

mkdir -p resources
curl -fsSL -o resources/geoip.dat https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat || true
curl -fsSL -o resources/geosite.dat https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat || true

go mod download || true
go build ./... >/dev/null 2>&1 || true
"""

# Baseline: clean base.sha, no patches. base.sha is still checkout-able after
# the hardening strip because it is HEAD (reachable, not pruned).
_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

go test -timeout 30m -count=1 -v ./...
"""

# Test patch only: the new tests exercise behaviour the fix has not introduced
# yet, so they fail (or their package fails to compile) -- genuine f2p / n2p.
_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn /home/test.patch

go test -timeout 30m -count=1 -v ./...
"""

# Test + fix patches: production fix present, the suite passes.
_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

go test -timeout 30m -count=1 -v ./...
"""

# Archive-resilient apt: the older golang images are Debian bullseye, near
# end-of-life. Try a normal `apt-get update` first and fall back to
# archive.debian.org (dropping -updates) when the mirror has been retired --
# mirrors the stretch/buster handling image.py applies for deprecated bases,
# but keyed off runtime reachability rather than a fixed list.
_APT_INSTALL = (
    "RUN { apt-get update 2>/dev/null || "
    "{ sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && "
    "sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && "
    "sed -i '/-updates/d' /etc/apt/sources.list && "
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


class XrayCoreGo126ImageBase(Image):
    """Level 1: toolchain-only base image (shared by all PRs of the era).

    ``dependency()`` returns a *string* (the Go toolchain image), so the
    pipeline's ``DockerfileEnhancer`` engages and prepends the
    ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image must NOT clone
    the repository -- a shared string-dependency image that performs a
    ``git clone`` is force-pinned to a single ``${BASE_COMMIT}`` and
    history-stripped by the enhancer, which would break ``git checkout`` for
    every other PR sharing the base. So the clone lives in the Default image
    (whose dependency() is an Image, left verbatim by the enhancer), done
    per-PR. This image only provides the Go toolchain, apt deps, and Go env.
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
        return "base-xray_core_6077_to_4584"

    def workdir(self) -> str:
        return "base-xray_core_6077_to_4584"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # No `git clone` here on purpose -- see the class docstring. The string
        # dependency means DockerfileEnhancer injects the ARG/ENV/LABEL infra
        # block (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {_GO_IMAGE}

WORKDIR /home/

{_APT_INSTALL}

ENV GOFLAGS=-buildvcs=false
ENV GOTOOLCHAIN=local

CMD ["/bin/bash"]
"""


class XrayCoreGo126ImageDefault(Image):
    """Level 2: per-PR image (built on the shared toolchain base).

    ``dependency()`` returns the Base image (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim -- no pin, no history
    strip injected by the pipeline. The clone therefore lives here, per-PR: the
    image clones full history, checks out ``${BASE_COMMIT}`` inline, COPYs the
    scripts, warms the build cache + geo data (install.sh), then the verbatim
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
        return XrayCoreGo126ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", _INSTALL_SH.format(pr=self.pr)),
            File(".", "run.sh", _RUN_SH.format(pr=self.pr)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(pr=self.pr)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(pr=self.pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

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


@Instance.register("XTLS", "xray_core_6077_to_4584")
class XRAY_CORE_GO126(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return XrayCoreGo126ImageDefault(self.pr, self._config)

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

        re_result = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        re_pkg = re.compile(r"^(?:ok|FAIL|---)\s+(\S+/\S+)\s")

        # Buffer per-package results so each test name is qualified by its
        # package -> unique across the whole `go test ./...` run.
        pending: list[tuple[str, str]] = []

        def flush(pkg: str) -> None:
            for status, name in pending:
                full = f"{pkg}::{name}" if pkg else name
                if status == "PASS":
                    passed_tests.add(full)
                elif status == "FAIL":
                    failed_tests.add(full)
                else:
                    skipped_tests.add(full)
            pending.clear()

        for line in test_log.splitlines():
            m = re_result.match(line)
            if m:
                pending.append((m.group(1), m.group(2)))
                continue
            m = re_pkg.match(line)
            if m:
                flush(m.group(1))

        # Any results without a trailing package summary line.
        flush("")

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


# Route the dash-joined number_interval (canonical prs_in_bundle format) to the
# same XRAY_CORE_GO126 config. Each cleaned-dataset record in the
# "xray_core_6077_to_4584" era now carries its own bundle key (sorted PR numbers
# joined by "-"). Instance.create() looks up f"{org}/{number_interval}", and
# Instance.register returns the class unchanged so it answers to every key.
Instance.register("XTLS", "5971-5975-5978-5979-5981-5982-5985-5989-5992-6005")(XRAY_CORE_GO126)
Instance.register("XTLS", "5948-5949-5951-5952-5959-5960")(XRAY_CORE_GO126)
Instance.register("XTLS", "5891-5920-5932-5934-5935-5936-5941-5942-5945-5946-5947")(XRAY_CORE_GO126)
Instance.register("XTLS", "5762-5990-6013-6015-6020-6022-6027-6032-6034-6035-6041-6053-6055-6057-6060-6062-6063-6066-6068")(XRAY_CORE_GO126)
Instance.register("XTLS", "5693-5776-5814-5858-5860-5866-5869-5870-5872-5876-5880-5885-5887-5888-5890-5898-5899-5915-5916-5924-5927-5928-5929")(XRAY_CORE_GO126)
Instance.register("XTLS", "5565-5567-5569-5570-5572-5574-5577-5582-5587-5589-5590")(XRAY_CORE_GO126)
Instance.register("XTLS", "5535-5537-5544-5549-5550-5551-5553-5557-5558-5559")(XRAY_CORE_GO126)
Instance.register("XTLS", "4981-5080-5154-5397-5402-5410-5411-5417-5424-5425-5426-5427-5430-5440-5455-5459-5464-5465-5466-5467-5468-5469-5480-5486-5488-5489-5496-5499-5501-5502-5508-5509-5512-5522-5526-5527-5528-5530-5531-5532-5535-5537-5544-5549-5550-5551-5553-5557-5558-5559-5565-5567-5569-5570-5572-5574-5577-5582-5587-5589-5590")(XRAY_CORE_GO126)
Instance.register("XTLS", "4584-5554-5657-5661-5665-5668-5671-5676-5678-5679-5680-5685-5686-5689-5695-5698-5708-5711-5713-5715-5720-5722-5725-5728-5732-5733-5737-5738-5749-5752-5755-5759-5760-5761-5764-5765-5772-5773-5777-5782-5786-5793-5802-5803-5811-5812-5821-5832-5833-5834-5836-5837-5838-5843-5850")(XRAY_CORE_GO126)
