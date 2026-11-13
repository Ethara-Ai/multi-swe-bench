import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "prestodb"
_REPO = "presto"

_BASE_IMAGE = "eclipse-temurin:17-jdk-noble"

_MVN_FLAGS = "-B -ntp -T 1 --fail-at-end -Dstyle.color=never"
_PREPARE_TIMEOUT = "7200"
_TEST_TIMEOUT = "5400"

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM @@BASE_IMAGE@@

ARG TARGETARCH
ARG REPO_URL="https://github.com/@@ORG@@/@@REPO@@.git"
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

LABEL org.opencontainers.image.title="@@ORG@@/@@REPO@@" \\
      org.opencontainers.image.description="@@ORG@@/@@REPO@@ Docker image" \\
      org.opencontainers.image.source="https://github.com/@@ORG@@/@@REPO@@" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    maven \\
    python3 \\
    sudo \\
    unzip \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/@@REPO@@

WORKDIR /home/@@REPO@@

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = """FROM @@BASE_FULL_NAME@@

ARG BASE_COMMIT="@@BASE_SHA@@"

COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/

RUN bash /home/prepare.sh

@@HARDENING@@
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_PROXY_GUARD = """[ -n "${http_proxy:-}" ] || unset http_proxy
[ -n "${https_proxy:-}" ] || unset https_proxy
[ -n "${HTTP_PROXY:-}" ] || unset HTTP_PROXY
[ -n "${HTTPS_PROXY:-}" ] || unset HTTPS_PROXY"""

_SELECT_MODULES = """_select_modules() {
    local _cand _out _d
    _cand="$(sed -nE 's#^diff --git a/([^/]+)/src/.*#\\1#p' /home/test.patch | sort -u)"
    _out=""
    for _d in $_cand; do
        if [ -f "/home/@@REPO@@/$_d/pom.xml" ]; then
            _out="$_out,$_d"
        fi
    done
    printf '%s' "${_out#,}"
}"""

_SELECT_TESTS = """_select_tests() {
    local _cand _out _c
    _cand="$(sed -nE 's#^diff --git a/\\S+ b/.*/src/test/.*/([A-Za-z0-9_]+)\\.(java|kt)$#\\1#p' /home/test.patch | sort -u)"
    _out=""
    for _c in $_cand; do
        case "$_c" in
            Test*) _out="$_out,$_c" ;;
        esac
    done
    printf '%s' "${_out#,}"
}"""

_PREPARE = """#!/bin/bash
set -e
export CI=true

@@PROXY_GUARD@@

cd /home/@@REPO@@

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@BASE_SHA@@
bash /home/check_git_changes.sh

@@SELECT_MODULES@@

_PL="$(_select_modules)"
echo "PROVISION_MODULES=$_PL"

if [ -n "$_PL" ]; then
    timeout -k 60 @@PREPARE_TIMEOUT@@ ./mvnw @@MVN_FLAGS@@ -pl "$_PL" -am -DskipTests install || true
fi

java -version
./mvnw @@MVN_FLAGS@@ -v
test -n "$_PL"
./mvnw @@MVN_FLAGS@@ -o -q -pl "$_PL" test-compile
echo DEPS_OK
"""

_APPLY_PATCHES = """_apply_patches() {
    local _ex
    _ex="$(awk '/^diff --git /{p=$NF; sub(/^b\\//, "", p)} /^Binary files .* differ$/{print "--exclude=" p}' "$@" | sort -u)"
    git apply --whitespace=nowarn $_ex "$@"
}"""

_GRADED_TEST = """@@SELECT_MODULES@@

@@SELECT_TESTS@@

MODULES="$(_select_modules)"
TESTS="$(_select_tests)"
echo "SELECTED_MODULES=$MODULES"
echo "SELECTED_TESTS=$TESTS"

if [ -z "$MODULES" ] || [ -z "$TESTS" ]; then
    echo "NO_RUNNABLE_TESTS_IN_TEST_PATCH"
    exit 0
fi

find . -path '*/target/surefire-reports/TEST-*.xml' -delete 2>/dev/null || true

MVN_RC=0
timeout -k 60 @@TEST_TIMEOUT@@ ./mvnw @@MVN_FLAGS@@ -pl "$MODULES" test \\
    -Dtest="$TESTS" -Dsurefire.failIfNoSpecifiedTests=false \\
    -Dmaven.test.failure.ignore=true -DfailIfNoTests=false || MVN_RC=$?

find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} +

if [ "$MVN_RC" -ne 0 ]; then
    echo "MAVEN_EXIT_CODE=$MVN_RC"
fi
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

@@PROXY_GUARD@@

cd /home/@@REPO@@

""" + _GRADED_TEST

_TEST_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

@@PROXY_GUARD@@

@@APPLY_PATCHES@@

cd /home/@@REPO@@

_apply_patches /home/test.patch

""" + _GRADED_TEST

_FIX_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

@@PROXY_GUARD@@

@@APPLY_PATCHES@@

cd /home/@@REPO@@

_apply_patches /home/test.patch /home/fix.patch

""" + _GRADED_TEST


def parse_junit_xml(test_log: str) -> TestResult:
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
    name_re = re.compile(r'\bname="([^"]*)"')
    classname_re = re.compile(r'\bclassname="([^"]*)"')

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for match in testcase_re.finditer(clean):
        attrs = match.group(1)
        name_match = name_re.search(attrs)
        if not name_match:
            continue
        classname_match = classname_re.search(attrs)
        name = name_match.group(1)
        classname = classname_match.group(1) if classname_match else ""
        test_id = "%s#%s" % (classname, name) if classname else name

        if match.group(2) == "/>":
            passed_tests.add(test_id)
            continue

        inner = match.group(3) or ""
        if "<failure" in inner or "<error" in inner:
            failed_tests.add(test_id)
        elif "<skipped" in inner:
            skipped_tests.add(test_id)
        else:
            passed_tests.add(test_id)

    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    passed_tests -= skipped_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class PrestoImageBase(Image):
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
        return (
            _BASE_DOCKERFILE.replace("@@BASE_IMAGE@@", _BASE_IMAGE)
            .replace("@@ORG@@", _ORG)
            .replace("@@REPO@@", _REPO)
        )


class PrestoImageDefault(Image):
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
        return PrestoImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("@@SELECT_MODULES@@", _SELECT_MODULES)
            .replace("@@SELECT_TESTS@@", _SELECT_TESTS)
            .replace("@@APPLY_PATCHES@@", _APPLY_PATCHES)
            .replace("@@PROXY_GUARD@@", _PROXY_GUARD)
            .replace("@@MVN_FLAGS@@", _MVN_FLAGS)
            .replace("@@PREPARE_TIMEOUT@@", _PREPARE_TIMEOUT)
            .replace("@@TEST_TIMEOUT@@", _TEST_TIMEOUT)
            .replace("@@BASE_SHA@@", self.pr.base.sha)
            .replace("@@REPO@@", _REPO)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._script(_PREPARE)),
            File(".", "run.sh", self._script(_RUN)),
            File(".", "test-run.sh", self._script(_TEST_RUN)),
            File(".", "fix-run.sh", self._script(_FIX_RUN)),
        ]

    def dockerfile(self) -> str:
        return (
            _PR_DOCKERFILE.replace(
                "@@BASE_FULL_NAME@@", self.dependency().image_full_name()
            )
            .replace("@@BASE_SHA@@", self.pr.base.sha)
            .replace("@@HARDENING@@", Image._HARDENING_BLOCK)
        )


@Instance.register(_ORG, _REPO)
class PRESTO(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return PrestoImageDefault(self.pr, self._config)

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
        return parse_junit_xml(test_log)
