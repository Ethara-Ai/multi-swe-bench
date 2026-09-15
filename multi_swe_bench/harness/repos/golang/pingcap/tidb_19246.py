import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_CONFIG_NAME = "tidb_19246"
_PR_NUMBER = 19246

_TEST_FILE_RE = re.compile(r"^diff --git a/(\S+_test\.go) b/", re.MULTILINE)


def _test_packages(pr: PullRequest) -> list[str]:
    packages = []
    for path in _TEST_FILE_RE.findall(pr.test_patch or ""):
        package = path.rsplit("/", 1)[0] if "/" in path else "."
        if package not in packages:
            packages.append(package)
    return packages


_BASE_DOCKERFILE = r"""FROM __BASE_IMAGE__
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
    LC_ALL=C.UTF-8 \
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
WORKDIR /home/
RUN set -eux; \
    mkdir -p /etc/pki/tls/certs /etc/ssl /etc/ssl/certs /etc/pki/ca-trust/extracted/pem; \
    ln -sf ${CA_CERT_PATH} /etc/pki/tls/certs/ca-bundle.crt; \
    ln -sf ${CA_CERT_PATH} /etc/ssl/cert.pem; \
    ln -sf ${CA_CERT_PATH} /etc/ssl/ca-bundle.pem; \
    ln -sf ${CA_CERT_PATH} /etc/pki/tls/cacert.pem; \
    ln -sf ${CA_CERT_PATH} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \
    ln -sf ${CA_CERT_PATH} /etc/ssl/certs/ca-bundle.crt
RUN set -eux; \
    installed=0; \
    for attempt in 1 2 3 4 5; do \
        if apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 update && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 install -y --no-install-recommends git ca-certificates curl gcc libc6-dev make tzdata procps; then \
            installed=1; \
            break; \
        fi; \
        sleep 20; \
    done; \
    test "$installed" -eq 1; \
    rm -rf /var/lib/apt/lists/*
RUN git clone "${REPO_URL}" /home/__REPO__
CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__
WORKDIR /home/__REPO__
RUN git reset --hard
RUN git checkout __BASE_SHA__
RUN set -eux; \
    git checkout --detach "__BASE_SHA__"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "__BASE_SHA__")"; \
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
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
WORKDIR /home/
__COPY_COMMANDS__RUN bash /home/prepare.sh
"""


_CHECK_GIT_CHANGES_SH = r"""set -e

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


_GO_ENV = r"""export PATH="/usr/local/go/bin:/root/go/bin:${PATH}"
export GO111MODULE=on
"""


_PREPARE_SH = r"""set -eo pipefail

cd /home/__REPO__

__GO_ENV__
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

go_minor="$(sed -n 's/^go \([0-9][0-9.]*\)[[:space:]]*$/\1/p' go.mod | head -1)"
test -n "$go_minor"

case "$(dpkg --print-architecture)" in
    amd64) go_arch=amd64 ;;
    arm64) go_arch=arm64 ;;
    *) go_arch="$(dpkg --print-architecture)" ;;
esac

go_version=""
for attempt in 1 2 3 4 5; do
    go_version="$(curl -fsSL 'https://go.dev/dl/?mode=json&include=all' | grep -oE "\"go${go_minor//./\\.}(\.[0-9]+)?\"" | tr -d '"' | sort -uV | tail -1 || true)"
    if [ -n "$go_version" ]; then
        break
    fi
    sleep 15
done
test -n "$go_version"
echo "prepare: go.mod requires ${go_minor}, installing ${go_version} for ${go_arch}"

go_installed=0
for attempt in 1 2 3 4 5; do
    rm -rf /usr/local/go
    if curl -fsSL "https://dl.google.com/go/${go_version}.linux-${go_arch}.tar.gz" | tar -xz -C /usr/local; then
        go_installed=1
        break
    fi
    sleep 15
done
test "$go_installed" -eq 1
go version

deps_downloaded=0
for attempt in 1 2 3; do
    if go mod download; then
        deps_downloaded=1
        break
    fi
    echo "prepare: go mod download attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
test "$deps_downloaded" -eq 1

failpoint_package="$(sed -n 's/.*build -o \$@ \([^ ]*failpoint-ctl\)[[:space:]]*$/\1/p' Makefile | head -1)"
test -n "$failpoint_package"
go build -o /usr/local/bin/failpoint-ctl "$failpoint_package"
test -x /usr/local/bin/failpoint-ctl

git checkout -- go.mod go.sum 2>/dev/null || true
bash /home/check_git_changes.sh
echo "DEPS_OK"
"""


_SUITE_BODY = r"""cd /home/__REPO__

__GO_ENV__
makefile_env="$(sed -n '/Running in native mode/,/GOTEST/p' Makefile | grep -oE '[A-Za-z_]+=[^;]+' | tr -d "'\"" || true)"
while IFS= read -r assignment; do
    if [ -n "$assignment" ]; then
        export "$assignment"
    fi
done <<< "$makefile_env"

find "$PWD" -type d | grep -vE "(\.git|tools)" | xargs failpoint-ctl enable

run_suite() {
    label="$1"
    shift
    echo "===== tidb-suite: ${label} ====="
    "$@" || echo "===== tidb-suite-failed: ${label} (exit $?) ====="
}

for package in __TEST_PACKAGES__; do
    if [ -d "$package" ]; then
        run_suite "$package" go test -v -count=1 -timeout 0 "./${package}"
    else
        echo "===== tidb-suite-missing: ${package} ====="
    fi
done

exit 0
"""


_APPLY_TEST_PATCH = r"""cd /home/__REPO__

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

"""


_APPLY_FIX_PATCH = r"""if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi

"""


class TidbImageBase(Image):
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
        return "debian:bookworm-slim"

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

        return DockerfileEnhancer.SYNTAX_DIRECTIVE + "\n" + (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class TidbImageDefault(Image):
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
        return TidbImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        packages = " ".join(f"'{package}'" for package in _test_packages(self.pr))
        return (
            template.replace("__GO_ENV__", _GO_ENV)
            .replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__TEST_PACKAGES__", packages)
        )

    def files(self) -> list[File]:
        header = "set -uo pipefail\n\n"
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(header + _SUITE_BODY)),
            File(
                ".",
                "test-run.sh",
                self._render(header + _APPLY_TEST_PATCH + _SUITE_BODY),
            ),
            File(
                ".",
                "fix-run.sh",
                self._render(
                    header + _APPLY_TEST_PATCH + _APPLY_FIX_PATCH + _SUITE_BODY
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return DockerfileEnhancer.SYNTAX_DIRECTIVE + "\n" + (
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__COPY_COMMANDS__", copy_commands)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_SUITE_RE = re.compile(r"^=+\s*tidb-suite:\s*(.+?)\s*=+$")
_GOCHECK_RE = re.compile(
    r"^(?P<status>PASS|FAIL|PANIC|SKIP|MISS|MISSED|FIXTURE-PANIC|EXPECTED FAILURE):\s+\S+:\d+:\s+(?P<name>[\w.]+)"
)
_GOTEST_RE = re.compile(r"^--- (?P<status>PASS|FAIL|SKIP): (?P<name>\S+)")
_PACKAGE_OK_RE = re.compile(r"^ok\s+(?P<name>\S+)")
_PACKAGE_FAIL_RE = re.compile(r"^FAIL\s+(?P<name>[^\s:]+)(?:\s|$)")

_PASS_STATUS = {"PASS", "EXPECTED FAILURE"}
_FAIL_STATUS = {"FAIL", "PANIC", "FIXTURE-PANIC"}
_SKIP_STATUS = {"SKIP", "MISS", "MISSED"}


def tidb_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log)
    package = ""

    def record(name: str, status: str) -> None:
        if status in _FAIL_STATUS:
            failed_tests.add(name)
        elif status in _PASS_STATUS:
            passed_tests.add(name)
        elif status in _SKIP_STATUS:
            skipped_tests.add(name)

    for raw_line in clean_log.splitlines():
        line = raw_line.rstrip()

        match = _SUITE_RE.match(line.strip())
        if match:
            package = match.group(1)
            continue

        match = _GOCHECK_RE.match(line)
        if match:
            record(f"{package}/{match.group('name')}", match.group("status"))
            continue

        match = _GOTEST_RE.match(line.strip())
        if match:
            record(f"{package}/{match.group('name')}", match.group("status"))
            continue

        match = _PACKAGE_OK_RE.match(line)
        if match:
            passed_tests.add(match.group("name"))
            continue

        match = _PACKAGE_FAIL_RE.match(line)
        if match:
            failed_tests.add(match.group("name"))

    passed_tests -= failed_tests
    skipped_tests -= passed_tests | failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("pingcap", _CONFIG_NAME)
class TIDB_19246(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TidbImageDefault(self.pr, self._config)

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
        return tidb_parse_log(test_log)


Instance._registry.setdefault(f"pingcap/{_PR_NUMBER}", TIDB_19246)
