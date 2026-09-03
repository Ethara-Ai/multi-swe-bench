import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "golang:1.9"

    def image_tag(self) -> str:
        return "base-go1-9"

    def workdir(self) -> str:
        return "base-go1-9"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

ENV DEBIAN_FRONTEND=noninteractive
ENV GO111MODULE=off
ENV GOPATH=/go

RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --allow-unauthenticated git build-essential

{self.global_env}

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

RUN mkdir -p /go/src/github.com/ethereum && ln -sfn /home/{self.pr.repo} /go/src/github.com/ethereum/go-ethereum

WORKDIR /home/{self.pr.repo}

{self.clear_env}

"""


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
# Skip entirely on non-native arch (QEMU crashes on go operations)
if [ "$(uname -m)" != "aarch64" ]; then exit 0; fi
set -e

cd /go/src/github.com/ethereum/go-ethereum
git reset --hard
git checkout {pr.base.sha}

GO111MODULE=off go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /go/src/github.com/ethereum/go-ethereum
GO111MODULE=off go test -v -count=1 ./...

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /go/src/github.com/ethereum/go-ethereum
if ! git apply --whitespace=nowarn --allow-binary-replacement /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
GO111MODULE=off go test -v -count=1 ./...

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /go/src/github.com/ethereum/go-ethereum
if ! git apply --whitespace=nowarn --allow-binary-replacement /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
GO111MODULE=off go test -v -count=1 ./...

""",
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
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("ethereum", "go_ethereum_3535_to_3514")
class GO_ETHEREUM_3535_TO_3514(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        passed_tests = set(re.findall(r"--- PASS: ([\S]+)", log))
        failed_tests = set(re.findall(r"--- FAIL: ([\S]+)", log))
        skipped_tests = set(re.findall(r"--- SKIP: ([\S]+)", log))

        # Ensure disjointness: FAIL takes precedence over PASS/SKIP
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
# Registers each delivered bundle's dash-joined number_interval to this era's
# class so Instance.create() resolves f"ethereum/{number_interval}" -> GO_ETHEREUM_3535_TO_3514.
_BUNDLE_NIS_GO_ETHEREUM_3535_TO_3514 = [
    "3514-3592-3605-3625-3631-3635-3636-3641-3645-3648-3649-3651-3656-3662-3665-3668-3670-3671",
    "3535-3536-3538-3542-3544-3545-3546-3548-3551-3553-3555-3558-3559-3560-3568-3570",
]
for _ni in _BUNDLE_NIS_GO_ETHEREUM_3535_TO_3514:
    Instance.register("ethereum", _ni)(GO_ETHEREUM_3535_TO_3514)
