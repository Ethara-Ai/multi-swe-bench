import json
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "hugo_4039_to_2652"
_GO_IMAGE = "golang:1.10.8"
_DEP_VERSION = "v0.5.4"
_GOVENDOR_VERSION = "v1.0.9"
_LEAKTEST_COMMIT = "0db74e8cd5adacfcc982838c6e185789e4b44e14"
_BINARY_RE = re.compile(r"^Binary files (?:a/(.+?)|/dev/null) and (?:b/(.+?)|/dev/null) differ$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _lossy_paths(patch: str) -> set:
    out = set()
    for block in re.split(r"(?=^diff --git )", patch or "", flags=re.MULTILINE):
        head = re.match(r"diff --git a/(\S+) b/\S+", block)
        if head and "\ufffd" in block:
            out.add(head.group(1))
    return out


def _excludes(*patches: str) -> str:
    paths = {p for patch in patches for m in _BINARY_RE.finditer(patch or "") for p in m.groups() if p}
    paths |= {p for patch in patches for p in _lossy_paths(patch)}
    return " ".join(shlex.quote(f"--exclude={path}") for path in sorted(paths))


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
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    CI=true \
    GOPATH=/go

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

WORKDIR /home/

RUN curl -fsSL -o /tmp/debian-archive-keyring.deb http://archive.debian.org/debian/pool/main/d/debian-archive-keyring/debian-archive-keyring_2019.1+deb10u1_all.deb \
    && dpkg -i /tmp/debian-archive-keyring.deb \
    && rm -f /tmp/debian-archive-keyring.deb \
    && printf 'deb http://archive.debian.org/debian stretch main\n' > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/*.list \
    && printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99archive \
    && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    build-essential \
    git \
    gnupg \
    make \
    python3 \
    sudo \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN git --version && go version

RUN git clone -q --depth 1 --branch __DEP_VERSION__ https://github.com/golang/dep.git /go/src/github.com/golang/dep \
    && cd /go/src/github.com/golang/dep && go install ./cmd/dep \
    && git clone -q --depth 1 --branch __GOVENDOR_VERSION__ https://github.com/kardianos/govendor.git /go/src/github.com/kardianos/govendor \
    && cd /go/src/github.com/kardianos/govendor && go install . \
    && git clone -q https://github.com/fortytw2/leaktest.git /go/src/github.com/fortytw2/leaktest \
    && git -C /go/src/github.com/fortytw2/leaktest -c advice.detachedHead=false checkout -q __LEAKTEST_COMMIT__ \
    && test -x /go/bin/dep && test -x /go/bin/govendor

__GLOBAL_ENV__

__CODE__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

ARG BASE_COMMIT="__SHA__"

__GLOBAL_ENV__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 1; \
    git config --local pack.windowMemory 32m; \
    git config --local pack.packSizeLimit 128m; \
    git config --local pack.deltaCacheSize 32m; \
    git gc --prune=now; \
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
            git config --local pack.threads 1; \
            git config --local pack.windowMemory 32m; \
            git config --local pack.packSizeLimit 128m; \
            git config --local pack.deltaCacheSize 32m; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi

__CLEAR_ENV__
"""

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"; exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"; git status --porcelain; exit 1
fi
echo "check_git_changes: No uncommitted changes"
"""

_GO_PKG_DIR = r"""if grep -q '"github.com/spf13/hugo/' main.go; then
    GO_PKG_DIR=/go/src/github.com/spf13/hugo
else
    GO_PKG_DIR=/go/src/github.com/gohugoio/hugo
fi
test -d "${GO_PKG_DIR}"
"""

_SYNC_DEPS = r"""sync_deps() {
    local attempt
    for attempt in 1 2 3; do
        if [ -f Gopkg.lock ]; then
            dep ensure -vendor-only -v && return 0
        elif [ -f vendor/vendor.json ]; then
            govendor sync -v && return 0
        else
            return 0
        fi
        if [ "${attempt}" -eq 3 ]; then
            echo "deps: vendor sync failed after ${attempt} attempts" >&2
            exit 1
        fi
        echo "deps: vendor sync attempt ${attempt} failed; retrying in 15s" >&2
        sleep 15
    done
}
"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/__REPO__

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

__SYNC_DEPS____GO_PKG_DIR__cd "${GO_PKG_DIR}"
go version
sync_deps
go build ./...
go test -count=1 -vet=off -run '^$' ./... > /dev/null
echo DEPS_OK
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/__REPO__

__GO_PKG_DIR__cd "${GO_PKG_DIR}"
go test -json -count=1 -vet=off -timeout 30m ./...
"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/__REPO__

git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch

__SYNC_DEPS____GO_PKG_DIR__cd "${GO_PKG_DIR}"
if grep -qE '^diff --git a/(vendor/vendor\.json|Gopkg\.lock|Gopkg\.toml) ' /home/test.patch; then
    sync_deps
fi
go test -json -count=1 -vet=off -timeout 30m ./...
"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/__REPO__

git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch

__SYNC_DEPS____GO_PKG_DIR__cd "${GO_PKG_DIR}"
if grep -qE '^diff --git a/(vendor/vendor\.json|Gopkg\.lock|Gopkg\.toml) ' /home/test.patch /home/fix.patch; then
    sync_deps
fi
go test -json -count=1 -vet=off -timeout 30m ./...
"""


class _HugoImage_4039_TO_2652(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class HugoImageBase_4039_TO_2652(_HugoImage_4039_TO_2652):
    def dependency(self) -> str:
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        src = "/go/src/github.com/gohugoio/hugo"
        if self.config.need_clone:
            acquire = f'RUN git clone "${{REPO_URL}}" {src}'
        else:
            acquire = f"COPY __REPO__ {src}"
        code = (
            f"{acquire}\n\n"
            f"RUN ln -s {src} /home/__REPO__ \\\n"
            "    && mkdir -p /go/src/github.com/spf13 \\\n"
            f"    && ln -s {src} /go/src/github.com/spf13/hugo \\\n"
            "    && cd /home/__REPO__ && git rev-parse HEAD > /dev/null"
        )
        return (
            _BASE_DOCKERFILE.replace("__GO_IMAGE__", _GO_IMAGE)
            .replace("__DEP_VERSION__", _DEP_VERSION)
            .replace("__GOVENDOR_VERSION__", _GOVENDOR_VERSION)
            .replace("__LEAKTEST_COMMIT__", _LEAKTEST_COMMIT)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class HugoImageDefault_4039_TO_2652(_HugoImage_4039_TO_2652):
    def dependency(self) -> Image:
        return HugoImageBase_4039_TO_2652(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def _render(self, template: str) -> str:
        return (
            template.replace("__SYNC_DEPS__", _SYNC_DEPS)
            .replace("__GO_PKG_DIR__", _GO_PKG_DIR)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
            .replace("__EXCLUDES__", _excludes(self.pr.test_patch, self.pr.fix_patch))
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
            .replace("__SHA__", self.pr.base.sha)
        )


@Instance.register("gohugoio", _INTERVAL)
class HUGO_4039_TO_2652(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HugoImageDefault_4039_TO_2652(self.pr, self._config)

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
