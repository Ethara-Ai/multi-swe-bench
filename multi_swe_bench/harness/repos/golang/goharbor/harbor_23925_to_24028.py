import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "harbor_23925_to_24028"
_ORG = "goharbor"
_REPO = "harbor"
_PRS = frozenset({23925, 24028})
_GO_IMAGE = "golang:1.27.2-bookworm"
_SWAGGER_VERSION = "v0.33.1"
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

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    build-essential \
    pkg-config \
    libldap2-dev \
    postgresql \
    postgresql-contrib \
    redis-server \
    && rm -rf /var/lib/apt/lists/*

RUN GOTOOLCHAIN=local GOBIN=/usr/local/bin go install github.com/go-swagger/go-swagger/cmd/swagger@__SWAGGER_VERSION__

RUN sed -ri 's/(scram-sha-256|md5|peer)[[:space:]]*$/trust/' /etc/postgresql/*/main/pg_hba.conf && \
    service postgresql start && \
    su postgres -c "createdb registry" && \
    service postgresql stop

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
export GOFLAGS=-buildvcs=false
export CGO_ENABLED=1
"""

_TEST_ENV = r"""export POSTGRESQL_HOST=localhost
export POSTGRESQL_PORT=5432
export POSTGRESQL_USR=postgres
export POSTGRESQL_PWD=root123
export POSTGRESQL_DATABASE=registry
export REDIS_HOST=localhost
export HARBOR_ADMIN=admin
export HARBOR_ADMIN_PASSWD=Harbor12345
export CORE_SECRET=tempString
export UTTEST=true
export POSTGRES_MIGRATION_SCRIPTS_PATH=/home/__REPO__/make/migrations/postgresql/
export TOKEN_PRIVATE_KEY_PATH=/home/__REPO__/tests/private_key.pem
"""

_SYNC_DEPS = r"""sync_deps() {
    local attempt
    for attempt in 1 2 3; do
        (cd /home/__REPO__/src && go mod download) && return 0
        sleep 15
    done
    return 1
}
"""

_GEN_APIS = r"""gen_apis() {
    cd /home/__REPO__
    rm -rf src/server/v2.0/models src/server/v2.0/restapi
    swagger generate server \
        --template-dir=tools/swagger/templates \
        --exclude-main \
        --additional-initialism=CVE \
        --additional-initialism=GC \
        --additional-initialism=OIDC \
        -f api/v2.0/swagger.yaml \
        -A harbor \
        --target src/server/v2.0 > /tmp/gen_apis.log 2>&1
}
"""

_SERVICES_UP = r"""services_up() {
    local i
    service postgresql start > /dev/null 2>&1
    redis-server --daemonize yes > /dev/null 2>&1
    for i in $(seq 1 60); do
        pg_isready -h localhost -p 5432 -q && redis-cli ping > /dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}
"""

_TEST_CMD = r"""cd /home/__REPO__/src
PKGS=$(go list ./... | grep -v -E 'tests|testing')
go test -json -p 1 -count=1 -vet=off -timeout 30m ${PKGS}
"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

__GO_ENV__cd /home/__REPO__

git config --global --add safe.directory /home/__REPO__
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

__SYNC_DEPS____GEN_APIS__go version
sync_deps
gen_apis
cd /home/__REPO__/src
go build ./...
go test -count=1 -vet=off -exec /bin/true -run '^$' $(go list ./... | grep -v -E 'tests|testing') > /dev/null
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__GO_ENV____TEST_ENV__cd /home/__REPO__

__GEN_APIS____SERVICES_UP__services_up
gen_apis
__TEST_CMD__"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__GO_ENV____TEST_ENV__cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch

__SYNC_DEPS____GEN_APIS____SERVICES_UP__if grep -qE '^diff --git a/src/(go\.mod|go\.sum) ' /home/test.patch; then
    sync_deps
fi
services_up
gen_apis
__TEST_CMD__"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__GO_ENV____TEST_ENV__cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__SYNC_DEPS____GEN_APIS____SERVICES_UP__if grep -qE '^diff --git a/src/(go\.mod|go\.sum) ' /home/test.patch /home/fix.patch; then
    sync_deps
fi
services_up
gen_apis
__TEST_CMD__"""


class _HarborImage_23925_TO_24028(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class HarborImageBase_23925_TO_24028(_HarborImage_23925_TO_24028):
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
            .replace("__SWAGGER_VERSION__", _SWAGGER_VERSION)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class HarborImageDefault_23925_TO_24028(_HarborImage_23925_TO_24028):
    def dependency(self) -> Image:
        return HarborImageBase_23925_TO_24028(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def _render(self, template: str) -> str:
        return (
            template.replace("__GO_ENV__", _GO_ENV)
            .replace("__TEST_ENV__", _TEST_ENV)
            .replace("__SYNC_DEPS__", _SYNC_DEPS)
            .replace("__GEN_APIS__", _GEN_APIS)
            .replace("__SERVICES_UP__", _SERVICES_UP)
            .replace("__TEST_CMD__", _TEST_CMD)
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
class HARBOR_23925_TO_24028(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HarborImageDefault_23925_TO_24028(self.pr, self._config)

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


if not getattr(PullRequest, "_harbor_23925_to_24028_ni_shim", False):
    _harbor_23925_to_24028_orig_from_dict = PullRequest.from_dict.__func__

    def _harbor_23925_to_24028_from_dict(cls, *args, **kwargs):
        pr = _harbor_23925_to_24028_orig_from_dict(cls, *args, **kwargs)
        if (
            getattr(pr, "org", "") == _ORG
            and getattr(pr, "repo", "") == _REPO
            and not getattr(pr, "number_interval", "")
            and getattr(pr, "number", None) in _PRS
        ):
            pr.number_interval = _INTERVAL
        return pr

    PullRequest.from_dict = classmethod(_harbor_23925_to_24028_from_dict)
    PullRequest._harbor_23925_to_24028_ni_shim = True
