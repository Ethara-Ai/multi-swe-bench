import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

GO_IMAGE = "golang:1.20"
BACKEND_DIR = "backend"
TEST_CMD = "go test -v -count=1 ./..."
GATE_CMD = "go test -count=1 -run '^$' ./... && echo DEPS_OK"

BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM {base_image}

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
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV CGO_ENABLED=0
ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=local

WORKDIR /home/

RUN set -eux; \\
    for i in 1 2 3; do \\
        apt-get update && \\
        apt-get install -y --no-install-recommends --fix-missing \\
            ca-certificates curl git make \\
        && break || {{ echo "apt attempt $i failed, retrying"; sleep 10; }}; \\
    done; \\
    rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""

GIT_HARDENING = """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

PREPARE_SH = """#!/bin/bash
set -e
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

cd /home/[[REPO]]/[[BACKEND]]
go mod download

[[GATE]]
"""

RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd

cd /home/[[REPO]]/[[BACKEND]]
[[TEST_CMD]]
"""

TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

cd /home/[[REPO]]/[[BACKEND]]
[[TEST_CMD]]
"""

FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

cd /home/[[REPO]]/[[BACKEND]]
[[TEST_CMD]]
"""


def render_script(template: str, repo: str, sha: str = "") -> str:
    return (
        template.replace("[[REPO]]", repo)
        .replace("[[SHA]]", sha)
        .replace("[[BACKEND]]", BACKEND_DIR)
        .replace("[[GATE]]", GATE_CMD)
        .replace("[[TEST_CMD]]", TEST_CMD)
    )


class ConsoleImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return BASE_DOCKERFILE.format(
            base_image=GO_IMAGE,
            org=self.pr.org,
            repo=self.pr.repo,
        )


class ConsoleImageDefault(Image):
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
        return ConsoleImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", render_script(PREPARE_SH, repo, sha)),
            File(".", "run.sh", render_script(RUN_SH, repo)),
            File(".", "test-run.sh", render_script(TEST_RUN_SH, repo)),
            File(".", "fix-run.sh", render_script(FIX_RUN_SH, repo)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        sections = ["FROM {name}:{tag}".format(name=name, tag=tag)]

        if self.global_env:
            sections.append(self.global_env)

        sections.append('ARG BASE_COMMIT="{sha}"'.format(sha=self.pr.base.sha))
        sections.append(copy_commands.strip())
        sections.append("RUN bash /home/prepare.sh")
        sections.append(GIT_HARDENING.strip())

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("redpanda-data", "console")
class Console(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ConsoleImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        status_re = re.compile(
            r"^\s*--- (PASS|FAIL|SKIP): (\S+) \(\d+(?:\.\d+)?s\)\s*$"
        )

        for line in clean_log.splitlines():
            match = status_re.match(line)
            if not match:
                continue

            status, name = match.group(1), match.group(2)
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
