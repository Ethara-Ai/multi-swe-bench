import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_SELECT_PKGS = r"""
PKG_DIRS=$(grep -h '^diff --git a/' /home/test.patch /home/fix.patch 2>/dev/null \
    | sed -e 's|^diff --git a/||' -e 's| b/.*$||' \
    | grep -E '_test\.go$' \
    | grep -v '^vendor/' \
    | xargs -r -n1 dirname \
    | sort -u) || PKG_DIRS=""

GO_TEST_PKGS=""
for d in $PKG_DIRS; do
    if [ ! -d "$d" ]; then
        continue
    fi
    # Drop directories owned by a nested module: moby carries its own go.mod
    # under api/, and the root module cannot load those packages at all
    # ("main module does not contain package ...").  Skipping them keeps the
    # package set identical and loadable across all three stages.
    nested=0
    probe="$d"
    while [ "$probe" != "." ]; do
        if [ -f "$probe/go.mod" ]; then
            nested=1
            break
        fi
        probe=$(dirname "$probe")
    done
    if [ "$nested" -eq 1 ]; then
        echo "Skipping $d: owned by nested module $probe" >&2
        continue
    fi
    GO_TEST_PKGS="$GO_TEST_PKGS ./$d"
done
"""


class MobyImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "golang:1.26-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        extra_env = f"\n{self.global_env}\n" if self.global_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
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

ENV GOPATH=/go \\
    GOTOOLCHAIN=local \\
    CGO_ENABLED=1 \\
    GOFLAGS=-mod=vendor
{extra_env}
WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class MobyImageDefault(Image):
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
        return MobyImageBase(self.pr, self.config)

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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

        prepare = """#!/bin/bash
set -e

# --- Section 1: PIN ---
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

# --- Section 2: PROVISION ---
# Pre-`go.mod` moby (CalVer era) keeps its module graph in vendor.mod/vendor.sum
# and expects them to be linked into place before any go command runs; this is
# what moby's own hack/with-go-mod.sh does.
if [ ! -f go.mod ] && [ -f vendor.mod ]; then
    ln -sf vendor.mod go.mod
    ln -sf vendor.sum go.sum
fi

# Warm the build cache for the packages the three stages will exercise.  The
# fix patch may be what creates them, so a failure here is expected and the
# gate below is what actually proves the toolchain and vendor tree are usable.
__SELECT_PKGS__
if [ -n "$GO_TEST_PKGS" ]; then
    go build $GO_TEST_PKGS || true
fi

# --- Section 3: GATE ---
go version
test -f go.mod
go list ./daemon/... > /dev/null
echo "DEPS_OK"
"""

        run = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
__SELECT_PKGS__
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No testable Go packages found in patches"
    exit 0
fi
echo "Testing packages: $GO_TEST_PKGS"
go test -v -count=1 $GO_TEST_PKGS
"""

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch

__SELECT_PKGS__
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No testable Go packages found in patches"
    exit 0
fi
echo "Testing packages: $GO_TEST_PKGS"
go test -v -count=1 $GO_TEST_PKGS
"""

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__SELECT_PKGS__
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No testable Go packages found in patches"
    exit 0
fi
echo "Testing packages: $GO_TEST_PKGS"
go test -v -count=1 $GO_TEST_PKGS
"""

        def render(body: str) -> str:
            return (
                body.replace("__SELECT_PKGS__", _SELECT_PKGS.strip())
                .replace("__REPO__", repo)
                .replace("__BASE_SHA__", sha)
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "prepare.sh", render(prepare)),
            File(".", "run.sh", render(run)),
            File(".", "test-run.sh", render(test_run)),
            File(".", "fix-run.sh", render(fix_run)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        extra_env = f"\n{self.global_env}\n" if self.global_env else ""

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
{extra_env}
{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("moby", "moby")
class Moby(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MobyImageDefault(self.pr, self._config)

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

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        re_pkg_fail = re.compile(
            r"^FAIL\s+(github\.com/(?:moby/moby|docker/docker)\S*)\s+(?:\[|[\d.])"
        )

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in failed_tests:
                        continue
                    skipped_tests.discard(base_name)
                    passed_tests.add(base_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    base_name = get_base_name(test_name)
                    passed_tests.discard(base_name)
                    skipped_tests.discard(base_name)
                    failed_tests.add(base_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name not in passed_tests and base_name not in failed_tests:
                        skipped_tests.add(base_name)

            pkg_fail_match = re_pkg_fail.match(line)
            if pkg_fail_match:
                pkg_name = pkg_fail_match.group(1)
                failed_tests.add(pkg_name)

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
