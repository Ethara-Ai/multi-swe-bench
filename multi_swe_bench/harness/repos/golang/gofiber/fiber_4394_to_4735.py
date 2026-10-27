import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "fiber_4394_to_4735"
_ORG = "gofiber"
_REPO = "fiber"
_PRS = frozenset({4394, 4397, 4400, 4420, 4486, 4560, 4570, 4735})
_GO_IMAGE = "golang:1.26-bookworm"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __GO_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

__GLOBAL_ENV__

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential && rm -rf /var/lib/apt/lists/*

__CODE__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

ARG BASE_COMMIT="__SHA__"

__GLOBAL_ENV__

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

__COPY_COMMANDS__
RUN bash /home/prepare.sh

__HARDENING__
__CLEAR_ENV__
"""

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1
test -z "$(git status --porcelain)"
"""

_GO_ENV = r"""export CI=true
export GOTOOLCHAIN=local
export GOFLAGS=-mod=mod
export CGO_ENABLED=1
"""

_SYNC_DEPS = r"""sync_deps() {
    local attempt
    for attempt in 1 2 3; do
        go mod download && return 0
        sleep 15
    done
    return 1
}
"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

__GO_ENV__cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
    git fetch --no-tags origin "__SHA__"
fi
git checkout --detach "__SHA__"
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

__SYNC_DEPS__go version
sync_deps
go build ./...
go test -count=1 -vet=off -run '^$' ./... > /dev/null
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__GO_ENV__cd /home/__REPO__

go test -json -count=1 -vet=off -timeout 30m ./...
"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__GO_ENV__cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch

__SYNC_DEPS__if grep -qE '^diff --git a/(go\.mod|go\.sum) ' /home/test.patch; then
    sync_deps
fi
go test -json -count=1 -vet=off -timeout 30m ./...
"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__GO_ENV__cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__SYNC_DEPS__if grep -qE '^diff --git a/(go\.mod|go\.sum) ' /home/test.patch /home/fix.patch; then
    sync_deps
fi
go test -json -count=1 -vet=off -timeout 30m ./...
"""


class _FiberImage_4394_TO_4735(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class FiberImageBase_4394_TO_4735(_FiberImage_4394_TO_4735):
    def dependency(self) -> str:
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = 'RUN git clone "${REPO_URL}" /home/__REPO__'
        else:
            code = "COPY __REPO__ /home/__REPO__"
        return (
            _BASE_DOCKERFILE.replace("__GO_IMAGE__", _GO_IMAGE)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class FiberImageDefault_4394_TO_4735(_FiberImage_4394_TO_4735):
    def dependency(self) -> Image:
        return FiberImageBase_4394_TO_4735(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def _render(self, template: str) -> str:
        return (
            template.replace("__GO_ENV__", _GO_ENV)
            .replace("__SYNC_DEPS__", _SYNC_DEPS)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency().image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__HARDENING__", Image._HARDENING_BLOCK)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )


@Instance.register(_ORG, _INTERVAL)
class FIBER_4394_TO_4735(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return FiberImageDefault_4394_TO_4735(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        status: dict[str, str] = {}
        for line in _ANSI_RE.sub("", test_log).splitlines():
            start = line.find("{")
            if start < 0:
                continue
            try:
                event = json.loads(line[start:])
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            test, package, action = event.get("Test"), event.get("Package"), event.get("Action")
            if not test or not package or action not in ("pass", "fail", "skip"):
                continue
            status[f"{package}::{test}"] = action
        passed = {n for n, s in status.items() if s == "pass"}
        failed = {n for n, s in status.items() if s == "fail"}
        skipped = {n for n, s in status.items() if s == "skip"}
        passed -= failed
        skipped -= failed | passed
        return TestResult(
            passed_count=len(passed), failed_count=len(failed), skipped_count=len(skipped),
            passed_tests=passed, failed_tests=failed, skipped_tests=skipped,
        )


if not getattr(PullRequest, "_fiber_4394_to_4735_ni_shim", False):
    _fiber_4394_to_4735_orig_from_dict = PullRequest.from_dict.__func__

    def _fiber_4394_to_4735_from_dict(cls, *args, **kwargs):
        pr = _fiber_4394_to_4735_orig_from_dict(cls, *args, **kwargs)
        if (
            getattr(pr, "org", "") == _ORG
            and getattr(pr, "repo", "") == _REPO
            and not getattr(pr, "number_interval", "")
            and getattr(pr, "number", None) in _PRS
        ):
            pr.number_interval = _INTERVAL
        return pr

    PullRequest.from_dict = classmethod(_fiber_4394_to_4735_from_dict)
    PullRequest._fiber_4394_to_4735_ni_shim = True
