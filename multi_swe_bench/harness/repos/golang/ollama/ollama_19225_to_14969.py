import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_ENV = """export CGO_ENABLED=1
export GOPATH=/go
export GOCACHE=/root/.cache/go-build
export GOMODCACHE=/go/pkg/mod
export PATH=$GOPATH/bin:$PATH
"""

_GO_PKG_SNIPPET = r"""
_extract_go_packages() {
    local paths
    paths=$(
        set +e +o pipefail
        grep -h '^diff --git a/' "$@" 2>/dev/null \
            | sed 's|diff --git a/||;s| b/.*||' \
            | grep '\.go$' \
            | grep -v '^vendor/' \
            | xargs -r -n1 dirname \
            | sort -u
    )

    local result=""
    local pkg
    for pkg in $paths; do
        if [ -d "$pkg" ]; then
            result="$result ./$pkg"
        fi
    done
    echo "$result"
}

GO_TEST_PKGS=$(_extract_go_packages /home/test.patch /home/fix.patch)
"""

_HARDENING = r"""RUN set -eux; \
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
"""


class OllamaImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "golang:1.26.10-bookworm"

    def image_tag(self) -> str:
        return "base-19225_to_14969"

    def workdir(self) -> str:
        return "base-19225_to_14969"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class OllamaImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return OllamaImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

""".format()
                + _GO_ENV
                + """
cd /home/{pr.repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout --detach {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

go mod download all || true
""".format(pr=self.pr)
                + _GO_PKG_SNIPPET
                + """
if [ -n "$GO_TEST_PKGS" ]; then
    go test -count=1 -run NoSuchTestWarmupOnly $GO_TEST_PKGS || true
fi

go version
go list -m all > /dev/null
go build ./api/... ./envconfig/...
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

"""
                + _GO_ENV
                + """
cd /home/{pr.repo}
""".format(pr=self.pr)
                + _GO_PKG_SNIPPET
                + """
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No testable Go packages found in patches"
    exit 0
fi
echo "Testing packages: $GO_TEST_PKGS"
go test -v -count=1 -timeout 20m $GO_TEST_PKGS
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

"""
                + _GO_ENV
                + """
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
""".format(pr=self.pr)
                + _GO_PKG_SNIPPET
                + """
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No testable Go packages found in patches"
    exit 0
fi
echo "Testing packages: $GO_TEST_PKGS"
go test -v -count=1 -timeout 20m $GO_TEST_PKGS
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

"""
                + _GO_ENV
                + """
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
""".format(pr=self.pr)
                + _GO_PKG_SNIPPET
                + """
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No testable Go packages found in patches"
    exit 0
fi
echo "Testing packages: $GO_TEST_PKGS"
go test -v -count=1 -timeout 20m $GO_TEST_PKGS
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{_HARDENING}"""


@Instance.register("ollama", "ollama_19225_to_14969")
class OLLAMA_19225_TO_14969(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OllamaImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [re.compile(r"^--- PASS:\s+(\S+)")]
        re_fail_tests = [re.compile(r"^--- FAIL:\s+(\S+)")]
        re_skip_tests = [re.compile(r"^--- SKIP:\s+(\S+)")]
        re_pkg_fail = re.compile(
            r"^FAIL\s+(\S+)(?:\s+\[(?:build|setup) failed\]|\s+[\d.]+s)\s*$"
        )

        def get_package_name(raw: str) -> str:
            name = raw
            if name.startswith("./"):
                name = name[2:]
            prefix = f"github.com/{self.pr.org}/{self.pr.repo}/"
            if name.startswith(prefix):
                name = name[len(prefix) :]
            return name.rstrip("/") or raw

        for line in clean_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    passed_tests.add(pass_match.group(1))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    skipped_tests.add(skip_match.group(1))

            pkg_fail_match = re_pkg_fail.match(line)
            if pkg_fail_match:
                failed_tests.add(get_package_name(pkg_fail_match.group(1)))

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


Instance.register("ollama", "ollama")(OLLAMA_19225_TO_14969)
