from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_BASE_IMAGE = "golang:1.26-bookworm"
_APT_PACKAGES = "git ca-certificates build-essential make"
_MODULE_PREFIX = "github.com/kyverno/kyverno"
_GO_PARALLEL = 2
_TEST_TIMEOUT = "60m"
# Grade only tests under pkg/ and cmd/.  test/integration/* needs a live
# Kubernetes cluster and test/cli/* is driven by the kyverno CLI runner, not
# `go test` -- running `./...` would surface failures that have nothing to do
# with the PR under review.
_GO_TEST_TARGETS = "./pkg/... ./cmd/..."
_TEST_CMD = f"go test -v -count=1 -timeout {_TEST_TIMEOUT} {_GO_TEST_TARGETS}"

_GO_ENV = """export CI=true
export CGO_ENABLED=0
export GOTOOLCHAIN=auto
export GOPATH=/go
export GOFLAGS=-p=2
export PATH=/usr/local/go/bin:/go/bin:$PATH"""


class KyvernoImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

FROM {_BASE_IMAGE}

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
    GOTOOLCHAIN=auto \\
    CGO_ENABLED=0 \\
    CI=true

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

WORKDIR /home/

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
        {_APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN go version

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class KyvernoImageDefault(Image):
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
        return KyvernoImageBase(self.pr, self._config)

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
set -eo pipefail

{_GO_ENV}

# --- Section 1: PIN ---
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

# --- Section 2: PROVISION ---
# Warm the module graph and the build cache for the graded test targets so
# the per-stage `go test` runs do not need network access.  Build failures
# here are tolerated -- the fix patch may be what makes the tree compile --
# but the gate below is non-tolerant and will catch an unusable toolchain.
go env GOVERSION
awk '/^go /{{print "prepare.sh: go.mod requires go" $2; exit}}' go.mod
go mod download

go build -p {_GO_PARALLEL} {_GO_TEST_TARGETS} || true
go test -p {_GO_PARALLEL} -count=1 -timeout {_TEST_TIMEOUT} -run '^$' {_GO_TEST_TARGETS} || true

# --- Section 3: GATE ---
# Hard, non-tolerant: `go list` must succeed, proving the module graph loads
# and every graded package is at least discoverable.  A half-resolved graph
# cannot slip through to the run stages and report 0/0/0.
go list {_GO_TEST_TARGETS} > /dev/null
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

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
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
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("kyverno", "kyverno")
class Kyverno(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KyvernoImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")
        re_pkg_fail = re.compile(
            rf"^FAIL\s+({re.escape(_MODULE_PREFIX)}\S*)\s+(?:\[|[\d.])"
        )

        def base_name(test_name: str) -> str:
            idx = test_name.rfind("/")
            return test_name if idx == -1 else test_name[:idx]

        for raw in test_log.splitlines():
            line = raw.strip()

            m = re_pass.match(line)
            if m:
                name = base_name(m.group(1))
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1)
                # Only accept leaf test names (Go names begin uppercase);
                # suppress stray "FAIL: tool" or other text that happens to
                # look like a failure line.
                if not name[:1].isupper():
                    continue
                name = base_name(name)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue

            m = re_skip.match(line)
            if m:
                name = base_name(m.group(1))
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
                continue

            m = re_pkg_fail.match(line)
            if m:
                failed_tests.add(m.group(1))

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
