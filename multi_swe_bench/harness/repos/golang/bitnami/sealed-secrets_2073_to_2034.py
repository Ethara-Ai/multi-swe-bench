import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.26-bookworm"
_ERA = "2073_to_2034"
_REPO_URL = "https://github.com/bitnami-labs/sealed-secrets.git"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: not inside a git work tree"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: working tree is dirty"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: working tree is clean"
exit 0
"""

_PREPARE_SH = """#!/bin/bash
set -e

git config --global --add safe.directory '*'

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

export GOTOOLCHAIN=local
go version
go env GOMODCACHE
go mod download all

test "$(git rev-parse HEAD)" = "{pr.base.sha}"
test -s /home/check_git_changes.sh
test -s /home/common.sh
test -s /home/run.sh
test -s /home/test-run.sh
test -s /home/fix-run.sh
test -s go.mod
go test -run '^$' -count=1 ./...
echo "prepare: OK at {pr.base.sha}"
"""

_COMMON_SH = """#!/bin/bash

apply_patch() {
  local f="$1"
  if [ ! -s "$f" ]; then
    echo "apply_patch: skipping empty patch $f"
    return 0
  fi
  echo "=== Applying patch $f ==="
  git apply --whitespace=nowarn "$f"
}

run_go_tests() {
  export GOTOOLCHAIN=local
  echo "=== Running go test on all packages ==="
  go test -v -count=1 -timeout=1800s ./...
}
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
source /home/common.sh

run_go_tests
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch

run_go_tests
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
apply_patch /home/fix.patch

run_go_tests
"""


class SealedSecretsEraImageBase(Image):
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
        return f"base-{_ERA}"

    def workdir(self) -> str:
        return f"base-{_ERA}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {_GO_IMAGE}

ARG TARGETARCH
ARG REPO_URL="{_REPO_URL}"
ARG BASE_COMMIT
ARG http_proxy
ARG https_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GOFLAGS=-mod=mod \\
    GOTOOLCHAIN=local \\
    CGO_ENABLED=1 \\
    CI=true

LABEL org.opencontainers.image.title="{repo}" \\
      org.opencontainers.image.description="Shared Go build base for {repo} era {_ERA}" \\
      org.opencontainers.image.source="{_REPO_URL}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs \\
 && ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.crt \\
 && ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/cert.pem \\
 && ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.pem \\
 && ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/cacert.pem \\
 && ln -sf ${{CA_CERT_PATH}} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \\
 && ln -sf ${{CA_CERT_PATH}} /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    openssl \\
    make \\
    build-essential \\
 && rm -rf /var/lib/apt/lists/*

RUN go version && go env GOPATH

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class SealedSecretsEraImageDefault(Image):
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
        return SealedSecretsEraImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _PREPARE_SH.format(pr=self.pr)),
            File(".", "common.sh", _COMMON_SH),
            File(".", "run.sh", _RUN_SH.format(pr=self.pr)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(pr=self.pr)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(pr=self.pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "".join(
            f"COPY {file.name} /home/\n" for file in self.files()
        )

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard

RUN git checkout --detach {sha}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
      git submodule foreach --recursive 'git remote remove origin 2>/dev/null || true; git reflog expire --expire=now --all; git reflog expire --expire-unreachable=now --all; git gc --prune=now --aggressive'; \\
    fi
"""


@Instance.register("bitnami", "sealed-secrets_2073_to_2034")
class SEALED_SECRETS_2073_TO_2034(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SealedSecretsEraImageDefault(self.pr, self._config)

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

        pass_re = re.compile(r"^--- PASS: (\S+)")
        fail_re = re.compile(r"^--- FAIL: (\S+)")
        skip_re = re.compile(r"^--- SKIP: (\S+)")
        pkg_re = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            m = pass_re.match(line)
            if m:
                pending_pass.add(m.group(1))
                continue

            m = fail_re.match(line)
            if m:
                pending_fail.add(m.group(1))
                continue

            m = skip_re.match(line)
            if m:
                pending_skip.add(m.group(1))
                continue

            m = pkg_re.match(line)
            if m:
                flush(m.group(1))
                continue

        flush("unknown")

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("bitnami", "sealed-secrets")(SEALED_SECRETS_2073_TO_2034)
