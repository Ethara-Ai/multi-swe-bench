import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "hugo_14234_to_12641"
_GO_IMAGE = "golang:1.25-trixie"
_DIFF_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)
_BINARY_RE = re.compile(r"^(?:GIT binary patch|Binary files .* differ)$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
_PACKAGE_RE = re.compile(r"^(ok|FAIL)\s+(\S+)(.*)$")
_BROKEN_RE = re.compile(r"\[(?:build|setup) failed\]")


def _apply_excludes(*patches: str) -> str:
    paths: set[str] = set()
    for patch in patches:
        for section in re.split(r"^(?=diff --git )", patch or "", flags=re.MULTILINE):
            head = _DIFF_RE.match(section)
            if head and (_BINARY_RE.search(section) or "�" in section):
                paths.update(head.groups())
    return "".join(f" --exclude={shlex.quote(path)}" for path in sorted(paths))


_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

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

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

ENV CI=true \\
    GOPATH=/go \\
    CGO_ENABLED=1 \\
    GORACE=halt_on_error=1 \\
    GIT_TERMINAL_PROMPT=0 \\
    GO111MODULE=on \\
    GOPROXY=https://proxy.golang.org,direct \\
    GOFLAGS=-mod=mod \\
    GOTOOLCHAIN=local

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git --version && go version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

__CODE__

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = """FROM __BASE_IMAGE__

__GLOBAL_ENV__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/__REPO__; \\
    git checkout --detach "__SHA__"; \\
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
    test "$(git rev-parse HEAD)" = "__SHA__"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \\
        cd /home/__REPO__ && git submodule foreach --recursive ' \\
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

__CLEAR_ENV__
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
  git fetch --no-tags origin "__SHA__"
fi
git checkout --detach "__SHA__"
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

go version
for attempt in 1 2 3; do go mod download && break; sleep 20; done

go build ./...
go test -count=1 -vet=off -run '^$' ./... > /dev/null
echo DEPS_OK
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
status=0
for d in $(find . -name '*.go' -not -path './vendor/*' -not -path '*/testdata/*' -printf '%h\n' | sort -u); do
  go test -v -count=1 -vet=off -timeout 20m "$d" || status=$?
done
exit "$status"
"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git ls-files -z go.mod go.sum | xargs -0 -r git checkout --
git apply --whitespace=nowarn__EXCLUDES__ /home/test.patch
status=0
for d in $(find . -name '*.go' -not -path './vendor/*' -not -path '*/testdata/*' -printf '%h\n' | sort -u); do
  go test -v -count=1 -vet=off -timeout 20m "$d" || status=$?
done
exit "$status"
"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git ls-files -z go.mod go.sum | xargs -0 -r git checkout --
git apply --whitespace=nowarn__EXCLUDES__ /home/test.patch /home/fix.patch
status=0
for d in $(find . -name '*.go' -not -path './vendor/*' -not -path '*/testdata/*' -printf '%h\n' | sort -u); do
  go test -v -count=1 -vet=off -timeout 20m "$d" || status=$?
done
exit "$status"
"""


class HugoImageBase_HUGO_14234_TO_12641(Image):
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
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"
        return (
            _BASE_DOCKERFILE.replace("__GO_IMAGE__", _GO_IMAGE)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class HugoImageDefault_HUGO_14234_TO_12641(Image):
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
        return HugoImageBase_HUGO_14234_TO_12641(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str, excludes: str = "") -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
            .replace("__EXCLUDES__", excludes)
        )

    def files(self) -> list[File]:
        test_excludes = _apply_excludes(self.pr.test_patch)
        fix_excludes = _apply_excludes(self.pr.test_patch, self.pr.fix_patch)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH, test_excludes)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH, fix_excludes)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        rendered = (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", f"{image.image_name()}:{image.image_tag()}")
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )
        return re.sub(r"\n{3,}", "\n\n", rendered).rstrip("\n") + "\n"


@Instance.register("gohugoio", _INTERVAL)
class HUGO_14234_TO_12641(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HugoImageDefault_HUGO_14234_TO_12641(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}
        pending: list[tuple[str, str]] = []
        for line in _ANSI_RE.sub("", test_log).splitlines():
            result = _RESULT_RE.match(line)
            if result:
                pending.append((result.group(1), result.group(2)))
                continue
            package = _PACKAGE_RE.match(line)
            if not package:
                continue
            for status, name in pending:
                buckets[status].add(f"{package.group(2)}::{name}")
            pending.clear()
            if package.group(1) == "FAIL" and _BROKEN_RE.search(package.group(3)):
                failed_tests.add(package.group(2))
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
