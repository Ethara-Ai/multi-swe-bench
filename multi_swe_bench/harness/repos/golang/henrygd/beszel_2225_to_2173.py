import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.27.2-bookworm"
_INTERVAL_NAME = "beszel_2225_to_2173"
_GO_TAGS = "testing no_ui"

_INFRA_BLOCK = r"""ARG TARGETARCH
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
"""

_LANG_ENV = r"""ENV CI=true \
    GOPATH=/go \
    GO111MODULE=on \
    GOTOOLCHAIN=local \
    CGO_ENABLED=1 \
    GOPROXY=https://proxy.golang.org,direct \
    GOFLAGS=-mod=mod \
    GOCACHE=/go/.cache/go-build
"""

_TOOLCHAIN_RUN = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    build-essential \
    git \
    jq \
    make \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m mswb && git config --global --add safe.directory '*' && git --version && go version && jq --version && command -v runuser
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

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "${BASE_COMMIT}^{commit}" 2>/dev/null || git fetch --no-tags origin "+${BASE_COMMIT}:refs/heads/msb-base-commit"
git checkout --detach "${BASE_COMMIT}"
bash /home/check_git_changes.sh

mkdir -p internal/site/dist
test -f internal/site/dist/index.html || echo "beszel" > internal/site/dist/index.html
go mod download || (sleep 15 && go mod download) || (sleep 30 && go mod download)
grep -rqs '"encoding/json/v2"' "$(go list -m -f '{{.Dir}}' github.com/pocketbase/pocketbase)" || export GOEXPERIMENT=nojsonv2
go build -tags='__GO_TAGS__' ./...
go test -tags='__GO_TAGS__' -count=1 -vet=off -run '^$' ./... > /dev/null

test -f internal/site/dist/index.html && go list -m > /dev/null && command -v jq > /dev/null && id mswb > /dev/null && echo "DEPS_OK"
"""

_RUN_TESTS_SH = r"""#!/bin/bash
cd /home/__REPO__
mkdir -p internal/site/dist
test -f internal/site/dist/index.html || echo "beszel" > internal/site/dist/index.html
grep -rqs '"encoding/json/v2"' "$(go list -m -f '{{.Dir}}' github.com/pocketbase/pocketbase)" || export GOEXPERIMENT=nojsonv2
MOD=$(go list -m)
grep -rHE --include='*_test.go' --exclude-dir=.git --exclude-dir=node_modules '^func Test[A-Za-z0-9_]*\(' . \
  | sed -E -e "s|^\./(.*)/([^/]+_test\.go):func (Test[A-Za-z0-9_]*)\(.*|${MOD}/\1 \3 \1/\2|" \
           -e "s|^\./([^/]+_test\.go):func (Test[A-Za-z0-9_]*)\(.*|${MOD} \2 \1|" \
  > /tmp/mswb_map.txt
: > /tmp/mswb_go.err
go list -tags='__GO_TAGS__' -e ./... > /tmp/mswb_pkgs.txt 2>> /tmp/mswb_go.err
awk 'NR == FNR { p[$1]; next } ($1 in p) && $2 != "TestCollectorStartHelpers" && $2 != "TestMain" { print "^" $2 "$", $1 }' /tmp/mswb_pkgs.txt /tmp/mswb_map.txt | sort -u > /tmp/mswb_runs.txt
chown -R mswb /home/__REPO__ /go
xargs -n 2 runuser -u mswb -- env HOME=/home/mswb go test -tags='__GO_TAGS__' -json -count=1 -vet=off -timeout 30m -run < /tmp/mswb_runs.txt > /tmp/mswb_go.json 2>> /tmp/mswb_go.err
jq -R -r 'fromjson? | select(.Test != null and (.Action == "pass" or .Action == "fail" or .Action == "skip")) | "\(.Action) \(.Package) \(.Test)"' /tmp/mswb_go.json > /tmp/mswb_events.txt
jq -R -r 'fromjson? | select(.Test == null and .Action == "fail") | "MSWB_SUITE_ERROR \(.Package)"' /tmp/mswb_go.json
jq -R -r 'fromjson? | select(.Action == "build-output") | "MSWB_BUILD \(.Output | rtrimstr("\n"))"' /tmp/mswb_go.json
tail -n 20 /tmp/mswb_go.err
awk 'NR == FNR { m[$1 " " $2] = $3; next } { split($3, a, "/"); k = $2 " " a[1]; f = (k in m) ? m[k] : $2; s = ($1 == "pass") ? "PASSED" : (($1 == "fail") ? "FAILED" : "SKIPPED"); print "MSWB_RESULT " s " " f "::" $3 }' /tmp/mswb_map.txt /tmp/mswb_events.txt
test -s /tmp/mswb_events.txt || echo "MSWB_NO_TESTS"
exit 0
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git reset --hard
git clean -fdq
bash /home/run_tests.sh
"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git reset --hard
git clean -fdq
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git reset --hard
git clean -fdq
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_PORT_RE = re.compile(r"((?:127\.0\.0\.1|0\.0\.0\.0|localhost|\[::1\]):)\d+")
_PASSED_RE = re.compile(r"^MSWB_RESULT PASSED (\S+)[ \t]*\r?$", re.MULTILINE)
_FAILED_RE = re.compile(r"^MSWB_RESULT FAILED (\S+)[ \t]*\r?$", re.MULTILINE)
_SKIPPED_RE = re.compile(r"^MSWB_RESULT SKIPPED (\S+)[ \t]*\r?$", re.MULTILINE)


def _render(template: str, repo: str) -> str:
    return template.replace("__REPO__", repo).replace("__GO_TAGS__", _GO_TAGS)


def _parse_mswb_log(test_log: str) -> TestResult:
    test_log = _PORT_RE.sub(r"\g<1>PORT", _ANSI_RE.sub("", test_log))
    failed_tests = set(_FAILED_RE.findall(test_log))
    passed_tests = set(_PASSED_RE.findall(test_log)) - failed_tests
    skipped_tests = set(_SKIPPED_RE.findall(test_log)) - failed_tests - passed_tests
    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class BeszelImageBase_2225_TO_2173(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = _INFRA_BLOCK.replace("__ORG__", self.pr.org).replace("__REPO__", self.pr.repo)
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {_GO_IMAGE}\n\n"
            f"{infra}\n"
            f"{_LANG_ENV}\n"
            f"{_TOOLCHAIN_RUN}\n"
            "WORKDIR /home/\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}\n\n'
            'CMD ["/bin/bash"]\n'
        )


class BeszelImageDefault_2225_TO_2173(Image):
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
        return BeszelImageBase_2225_TO_2173(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _render(_PREPARE_SH, repo)),
            File(".", "run.sh", _render(_RUN_SH, repo)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, repo)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, repo)),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, repo)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        hardening = Image._HARDENING_BLOCK.rstrip("\n")
        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            "COPY fix.patch /home/\n"
            "COPY test.patch /home/\n"
            "COPY check_git_changes.sh /home/\n"
            "COPY prepare.sh /home/\n"
            "COPY run.sh /home/\n"
            "COPY test-run.sh /home/\n"
            "COPY fix-run.sh /home/\n"
            "COPY run_tests.sh /home/\n\n"
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\n\n'
            "RUN bash /home/prepare.sh\n\n"
            f"WORKDIR /home/{self.pr.repo}\n\n"
            f"{hardening}\n\n"
        )


@Instance.register("henrygd", "beszel")
@Instance.register("henrygd", _INTERVAL_NAME)
class BESZEL_2225_TO_2173(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BeszelImageDefault_2225_TO_2173(self.pr, self._config)

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
        return _parse_mswb_log(test_log)
