import json
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "k6_6338_to_5714"
_GO_IMAGE = "golang:1.25.12-bookworm"
_BROWSER_PKG = "internal/js/modules/k6/browser/tests"
_BINARY_RE = re.compile(r"^Binary files (?:a/(.+?)|/dev/null) and (?:b/(.+?)|/dev/null) differ$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _lossy_paths(patch: str) -> set:
    out = set()
    for block in re.split(r"(?=^diff --git )", patch or "", flags=re.MULTILINE):
        head = re.match(r"diff --git a/(\S+) b/\S+", block)
        if head and "�" in block:
            out.add(head.group(1))
    return out


def _excludes(*patches: str) -> str:
    paths = {p for patch in patches for m in _BINARY_RE.finditer(patch or "") for p in m.groups() if p}
    paths |= {p for patch in patches for p in _lossy_paths(patch)}
    return " ".join(shlex.quote(f"--exclude={path}") for path in sorted(paths))


def _patch_files(*patches: str) -> list:
    return [f for patch in patches for f in re.findall(r"^diff --git a/\S+ b/(\S+)$", patch or "", re.MULTILINE)]


def _test_dirs(test_patch: str) -> list:
    dirs = set()
    for f in _patch_files(test_patch):
        if not f.endswith(".go") or f.startswith("vendor/") or "/testdata/" in f"/{f}":
            continue
        dirs.add(f.rsplit("/", 1)[0] if "/" in f else ".")
    return sorted(dirs)


def _has_browser_tests(test_patch: str) -> bool:
    return any(d == _BROWSER_PKG or d.startswith(_BROWSER_PKG + "/") for d in _test_dirs(test_patch))


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
    GOPATH=/go \
    GO111MODULE=on \
    GOTOOLCHAIN=local \
    GOFLAGS=-mod=vendor \
    CGO_ENABLED=1 \
    GOPROXY=https://proxy.golang.org,direct

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

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    build-essential \
    git \
    make \
    python3 \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*' && git --version && go version

__GLOBAL_ENV__

__CODE__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

ARG BASE_COMMIT="__SHA__"

__GLOBAL_ENV__

WORKDIR /home/__REPO__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

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
    test "$(git rev-parse HEAD)" = "__SHA__"; \
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

_ENV = r"""export CI=true GOPATH=/go GO111MODULE=on GOTOOLCHAIN=local GOFLAGS=-mod=vendor CGO_ENABLED=1
export PATH="/go/bin:/usr/local/go/bin:${PATH}"
export K6_BROWSER_ARGS="no-sandbox,disable-dev-shm-usage" K6_BROWSER_HEADLESS=true
"""

_INSTALL_BROWSER = r"""if [ "__BROWSER__" = "1" ]; then
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do
        if [ -f "${f}" ]; then
            sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "${f}"
        fi
    done
    grep -rqs "https://deb.debian.org" /etc/apt/sources.list /etc/apt/sources.list.d/
    installed=0
    for attempt in 1 2 3; do
        rm -rf /var/lib/apt/lists/*
        if apt-get -o Acquire::Retries=5 update \
            && apt-get -o Acquire::Retries=5 install -y --no-install-recommends chromium fonts-liberation; then
            installed=1
            break
        fi
        echo "deps: chromium install attempt ${attempt} failed; retrying in 15s" >&2
        apt-get clean
        sleep 15
    done
    rm -rf /var/lib/apt/lists/*
    test "${installed}" = "1"
    chromium --version
fi
"""

_PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

__ENV__cd /home/__REPO__

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

__INSTALL_BROWSER__go version
go build ./...
go test -count=1 -vet=off -run '^$' ./... > /dev/null
echo DEPS_OK
"""

_MARK_BUILD_FAILURES_PY = r"""import json
import os
import re
import sys

log_path, patch_path = sys.argv[1], sys.argv[2]
module = ""
with open("go.mod", encoding="utf-8") as f:
    for line in f:
        if line.startswith("module "):
            module = line.split()[1].strip()
            break
failed = set()
reported = set()
with open(log_path, encoding="utf-8", errors="replace") as f:
    for line in f:
        start = line.find("{")
        if start < 0:
            continue
        try:
            event = json.loads(line[start:])
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        pkg = event.get("Package")
        if not pkg:
            continue
        if event.get("Test"):
            reported.add((pkg, event["Test"]))
        elif event.get("Action") == "fail" and event.get("FailedBuild"):
            failed.add(pkg)
added = {}
current = None
with open(patch_path, encoding="utf-8", errors="replace") as f:
    for line in f:
        m = re.match(r"^diff --git a/\S+ b/(\S+)$", line)
        if m:
            current = m.group(1) if m.group(1).endswith("_test.go") else None
            continue
        if current:
            m = re.match(r"^\+func (Test\w+)\(t \*testing\.T\)", line)
            if m:
                d = os.path.dirname(current)
                added.setdefault(f"{module}/{d}" if d else module, set()).add(m.group(1))
for pkg in sorted(failed):
    for test in sorted(added.get(pkg, ())):
        if (pkg, test) not in reported:
            print(json.dumps({"Action": "fail", "Package": pkg, "Test": test}))
"""

_RUN_TESTS = r"""PKGS=""
for d in __TEST_DIRS__; do
    if [ "${d}" != "__BROWSER_PKG__" ] && [ -d "${d}" ] && ls "${d}"/*.go > /dev/null 2>&1; then
        PKGS="${PKGS} ./${d}"
    fi
done
status=0
: > /tmp/go_test.json
if [ -n "${PKGS}" ]; then
    go test -json -count=1 -vet=off -timeout 30m ${PKGS} 2>&1 | tee -a /tmp/go_test.json || status=1
fi
if [ "__BROWSER__" = "1" ] && [ -d "__BROWSER_PKG__" ]; then
    GOMAXPROCS=1 go test -json -count=1 -vet=off -p 1 -timeout 30m ./__BROWSER_PKG__ 2>&1 | tee -a /tmp/go_test.json || status=1
fi
python3 /home/mark_build_failures.py /tmp/go_test.json /home/test.patch
exit "${status}"
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__ENV__cd /home/__REPO__

__RUN_TESTS__"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__ENV__cd /home/__REPO__

git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch

__RUN_TESTS__"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__ENV__cd /home/__REPO__

git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch

__RUN_TESTS__"""


class _K6Image_6338_TO_5714(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class K6ImageBase_6338_TO_5714(_K6Image_6338_TO_5714):
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
            code = 'RUN git clone "${REPO_URL}" /home/__REPO__ && cd /home/__REPO__ && git rev-parse HEAD > /dev/null'
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


class K6ImageDefault_6338_TO_5714(_K6Image_6338_TO_5714):
    def dependency(self) -> Image:
        return K6ImageBase_6338_TO_5714(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def _render(self, template: str) -> str:
        browser = "1" if _has_browser_tests(self.pr.test_patch) else "0"
        dirs = " ".join(shlex.quote(d) for d in _test_dirs(self.pr.test_patch)) or "."
        return (
            template.replace("__RUN_TESTS__", _RUN_TESTS)
            .replace("__INSTALL_BROWSER__", _INSTALL_BROWSER)
            .replace("__ENV__", _ENV)
            .replace("__TEST_DIRS__", dirs)
            .replace("__BROWSER_PKG__", _BROWSER_PKG)
            .replace("__BROWSER__", browser)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
            .replace("__EXCLUDES__", _excludes(self.pr.test_patch, self.pr.fix_patch))
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mark_build_failures.py", _MARK_BUILD_FAILURES_PY),
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
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )


@Instance.register("grafana", "k6")
@Instance.register("grafana", _INTERVAL)
class K6_6338_TO_5714(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return K6ImageDefault_6338_TO_5714(self.pr, self._config)

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
            name = f"{package}::{test}"
            if status.get(name) != "fail":
                status[name] = action
        passed = {n for n, s in status.items() if s == "pass"}
        failed = {n for n, s in status.items() if s == "fail"}
        skipped = {n for n, s in status.items() if s == "skip"}
        passed -= failed
        skipped -= failed | passed
        return TestResult(
            passed_count=len(passed), failed_count=len(failed), skipped_count=len(skipped),
            passed_tests=passed, failed_tests=failed, skipped_tests=skipped,
        )
