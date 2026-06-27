import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# XTLS/Xray-core era "xray_core_5071_to_2477": build/test on the golang:1.24 toolchain.
_GO_IMAGE = "golang:1.24"


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


class XrayCoreGo124ImageBase(Image):
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
        return "base-xray_core_5071_to_2477"

    def workdir(self) -> str:
        return "base-xray_core_5071_to_2477"

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


class XrayCoreGo124ImageDefault(Image):
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
        return XrayCoreGo124ImageBase(self.pr, self._config)

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


@Instance.register("XTLS", "xray_core_5071_to_2477")
class XRAY_CORE_GO124(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return XrayCoreGo124ImageDefault(self.pr, self._config)

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
# same XRAY_CORE_GO124 config. Each cleaned-dataset record in the
# "xray_core_5071_to_2477" era now carries its own bundle key (sorted PR numbers
# joined by "-"). Instance.create() looks up f"{org}/{number_interval}", and
# Instance.register returns the class unchanged so it answers to every key.
Instance.register("XTLS", "4945-4947-4949-4968-4970-4973-4976")(XRAY_CORE_GO124)
Instance.register("XTLS", "4553-4666-4667-4718-4723-4738-4739-4750-4752-4754-4757-4761-4763-4767-4775-4777-4782-4783-4786-4789-4790-4792-4793")(XRAY_CORE_GO124)
Instance.register("XTLS", "4497-4498-4504-4506-4510-4513-4515-4516-4523-4526-4530-4536-4539-4547-4549-4551-4560-4561-4564-4566-4568-4571-4576-4577-4581-4585-4594-4595-4596-4597-4598-4602-4616-4627-4630-4634-4640-4642-4655-4659-4661-4663-4664-4671-4673-4680-4695-4698-4702-4703-4707-4708-4709-4710-4719-4721-4726-4729-4730-4732")(XRAY_CORE_GO124)
Instance.register("XTLS", "4497-4498-4504-4506-4510-4513-4515-4516-4523-4526-4530-4536-4539-4547-4549-4551-4560-4561-4564-4566-4568-4571")(XRAY_CORE_GO124)
Instance.register("XTLS", "3813-4260-4681-4804-4809-4816-4818-4824-4831-4835-4840-4857-4860-4861-4869-4870-4880-4881-4882-4885-4892-4899-4902-4903-4905-4906-4913-4914-4915-4919-4921-4922-4924-4929-4931-4933-4936-4937-4940-4942-4943")(XRAY_CORE_GO124)
Instance.register("XTLS", "3819-3830-3832-3835-3838-3845-3852-3855")(XRAY_CORE_GO124)
Instance.register("XTLS", "3813-4940-4942-4943")(XRAY_CORE_GO124)
Instance.register("XTLS", "3453-3613-3637-3644-3744-3751-3753-3754-3757-3762-3766-3769-3774-3776-3782-3783-3784-3786-3792-3793-3794-3797-3798-3799-3802-3804-3808-3809-3810-3812-3816-3817-3818-3819-3827-3830-3832-3835-3838-3845-3852-3855-3866-3867-3871-3873-3884-3889-3893-3895-3899-3900-3903-3906-3908-3910-3919-3921-3955-3960-3965-3967-3968-3971-3976-3977-3978-3979-3985-3986-3987-3990-3991-3994-3999-4000-4002-4008-4010-4012-4015-4017-4018-4019-4021-4026-4028-4030-4038-4042-4045-4050-4060-4065-4071-4075-4095-4110-4126-4128-4142-4143-4150-4156-4163-4175-4177-4181-4182-4192-4193-4203-4204-4206-4233-4234-4238-4239-4247-4253-4262-4268-4272-4284-4288-4290-4295-4297-4298-4303-4306-4319-4322-4325-4329-4330-4331-4343-4349-4350-4351-4352-4355-4360-4362-4363-4369-4375-4378-4382-4386-4390-4395-4401-4407-4409-4413-4416-4420-4433-4438-4439-4440-4451-4462-4463-4465-4469")(XRAY_CORE_GO124)
Instance.register("XTLS", "3533-3543-3546")(XRAY_CORE_GO124)
Instance.register("XTLS", "3453-3613-3744-3774-3776-3782-3783-3784-3786-3792-3793-3794-3797-3798-3799-3802-3804-3808-3809-3810-3812-3818")(XRAY_CORE_GO124)
Instance.register("XTLS", "3446-3465-3468-3473-3484-3485-3517-3527")(XRAY_CORE_GO124)
Instance.register("XTLS", "3391-3412-3413-3427-3428-3430-3449-3454")(XRAY_CORE_GO124)
Instance.register("XTLS", "3308-3317-3356-3371")(XRAY_CORE_GO124)
Instance.register("XTLS", "3060-3073-3076-3089")(XRAY_CORE_GO124)
Instance.register("XTLS", "2911-2914-2927-2930-2941-2943-2999")(XRAY_CORE_GO124)
Instance.register("XTLS", "2758-2794-2819-2844-2854-2878-2882")(XRAY_CORE_GO124)
Instance.register("XTLS", "2477-2520-2659-2716-2717-2719-2720")(XRAY_CORE_GO124)
