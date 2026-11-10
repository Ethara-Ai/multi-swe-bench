from __future__ import annotations

import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "kubescape_4301_to_3824"
_PR_LO = 3824
_PR_HI = 4301

_BASE_IMAGE = "golang:1.26.10-bookworm"
_APT_PACKAGES = "ca-certificates curl git make"
_MODULE_PREFIX = "github.com/kubescape/kubescape/v4"
_GO_PARALLEL = 2
_TEST_CMD = f"go test -p {_GO_PARALLEL} -json -count=1 -timeout 60m ./... 2>&1"

_GO_ENV = """export CI=true
export CGO_ENABLED=0
export GOTOOLCHAIN=local
export GOPATH=/go
export PATH=/usr/local/go/bin:/go/bin:$PATH"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_PLAIN_PKG_RE = re.compile(r"^(ok|FAIL)\s+(\S+)(?:\s|$)")
_ACTIONS = ("pass", "fail", "skip")


class KubescapeImageBase_4301_TO_3824(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return "base-4301_to_3824"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_BASE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    GOPATH=/go \\
    GOTOOLCHAIN=local \\
    CGO_ENABLED=0 \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
        {_APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN go version

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class KubescapeImageDefault_4301_TO_3824(Image):
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
        return KubescapeImageBase_4301_TO_3824(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        check_git_changes = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

        prepare = f"""#!/bin/bash
set -euo pipefail

{_GO_ENV}

cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

go env GOVERSION
awk '/^go /{{print "prepare.sh: go.mod requires go" $2; exit}}' go.mod

go mod download
go build -p {_GO_PARALLEL} ./...

go test -p {_GO_PARALLEL} -count=1 -timeout 60m -run '^$' ./...

echo "DEPS_OK"
"""

        run_tests = f"""#!/bin/bash
set -eo pipefail

{_GO_ENV}

cd /home/{repo}

{_TEST_CMD}
"""

        run = f"""#!/bin/bash
set -eo pipefail

{_GO_ENV}

cd /home/{repo}

bash /home/run_tests.sh
"""

        test_run = f"""#!/bin/bash
set -eo pipefail

{_GO_ENV}

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch

bash /home/run_tests.sh
"""

        fix_run = f"""#!/bin/bash
set -eo pipefail

{_GO_ENV}

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

bash /home/run_tests.sh
"""

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {self.dependency().image_full_name()}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout {sha}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
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
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


def _strip_module(package: str) -> str:
    if package == _MODULE_PREFIX:
        return "."
    prefix = _MODULE_PREFIX + "/"
    if package.startswith(prefix):
        return package[len(prefix) :]
    return package


def _is_own_package(package: str) -> bool:
    return package == _MODULE_PREFIX or package.startswith(_MODULE_PREFIX + "/")


def _parse_kubescape_log(test_log: str) -> TestResult:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    buckets = {"pass": passed, "fail": failed, "skip": skipped}
    plain = {"ok": "pass", "FAIL": "fail"}

    for raw in _ANSI_RE.sub("", test_log).splitlines():
        line = raw.strip()

        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            action = event.get("Action")
            if action not in _ACTIONS:
                continue
            package = event.get("Package") or ""
            if not _is_own_package(package):
                continue
            package = _strip_module(package)
            test = event.get("Test")
            if test:
                buckets[action].add(f"{package}::{test}")
            elif action != "skip":
                buckets[action].add(package)
            continue

        match = _PLAIN_PKG_RE.match(line)
        if match and _is_own_package(match.group(2)):
            buckets[plain[match.group(1)]].add(_strip_module(match.group(2)))

    passed -= failed
    passed -= skipped
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("kubescape", _INTERVAL_NAME)
class KUBESCAPE_4301_TO_3824(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return KubescapeImageDefault_4301_TO_3824(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_kubescape_log(test_log)


_INCUMBENT = Instance._registry.get("kubescape/kubescape")


@Instance.register("kubescape", "kubescape")
class KubescapeBareName_4301_TO_3824(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _PR_LO <= pr.number <= _PR_HI:
            return KUBESCAPE_4301_TO_3824(pr, config, *args, **kwargs)

        if _INCUMBENT is None:
            raise ValueError(
                f"kubescape/kubescape#{pr.number} falls outside the "
                f"{_PR_LO}..{_PR_HI} interval this adapter serves, and no previous "
                f"holder of the bare 'kubescape/kubescape' key was captured at "
                f"import time. Import "
                f"multi_swe_bench.harness.repos.golang.kubescape.kubescape "
                f"before this module (see kubescape/__init__.py)."
            )
        return _INCUMBENT(pr, config, *args, **kwargs)
