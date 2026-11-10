import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "osmandapp"
_REPO = "OsmAnd"

_BASE_TAG = "base"

_BASE_IMAGE = "eclipse-temurin:17-jdk-noble"
_BASE_APT = "git ca-certificates curl unzip"

_RESOURCES_REPO = "https://github.com/osmandapp/OsmAnd-resources.git"
_RESOURCES_DIR = "resources"
_RESOURCES_SPARSE = (
    "bundled_assets color-palette models obf_creation poi proj "
    "rendering_styles routing test-resources voice"
)

_CMDLINE_TOOLS_ZIP = "commandlinetools-linux-13114758_latest.zip"
_CMDLINE_TOOLS_SHA1 = "5fdcc763663eefb86a5b8879697aa6088b041e70"
_ANDROID_HOME = "/opt/android-sdk"
_ANDROID_API = "35"
_ANDROID_BUILD_TOOLS = "35.0.0"

_REGIONS_OCBF_URL = "https://builder.osmand.net/basemap/regions.ocbf"
_REGIONS_CACHE = "/opt/osmand-cache/regions.ocbf"
_REGIONS_REL = "OsmAnd-java/src/main/resources/net/osmand/map/regions.ocbf"

_SHARED_MODULE = "OsmAnd-shared"
_SHARED_TASK = ":OsmAnd-shared:jvmTest"
_JAVA_MODULE = "OsmAnd-java"
_JAVA_TASK = ":OsmAnd-java:test"
_ANDROID_MODULE = "OsmAnd"

_GRADLE_FLAGS = (
    "--no-daemon "
    "--console=plain "
    "--stacktrace "
    "--continue "
    "-Dorg.gradle.parallel=false "
    "-Dorg.gradle.caching=false "
    "-Dkotlin.incremental=false "
    "-Dkotlin.compiler.execution.strategy=in-process"
)

_GRADLE_TIMEOUT = "timeout -k 60 7200"
_GRADLE_TIMEOUT_PREPARE = "timeout -k 60 3600"
_GRADLE_TIMEOUT_WARMUP = "timeout -k 60 900"

_GRADLE_OPTS = "-Dorg.gradle.jvmargs=-Xmx5g -Dkotlin.daemon.jvmargs=-Xmx2g"

_CURL_OPTS = (
    "-fsSL --connect-timeout 30 --retry 20 --retry-delay 3 --retry-all-errors "
    "--speed-limit 409600 --speed-time 30"
)
_GIT_SLOW_GUARD = "-c http.lowSpeedLimit=409600 -c http.lowSpeedTime=30"
_NO_PROXY_ENV = "env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy"

_CHECK_GIT_CHANGES = r"""#!/bin/bash
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

_APPLY_FN = r"""_apply_patches() {
    local _ex
    _ex="$(awk '/^diff --git /{p=$NF; sub(/^b\//, "", p)} /^Binary files .* differ$/{print "--exclude=" p}' "$@" | sort -u)"
    git apply --whitespace=nowarn $_ex "$@"
}"""

_FETCH_DEPS = r"""_fetch_resources() {
    if [ -d "@@RESOURCES_DIR@@/routing" ]; then return 0; fi
    _i=0
    while :; do
        if git @@GIT_SLOW_GUARD@@ clone --depth 1 --filter=blob:none --no-checkout \
            "@@RESOURCES_REPO@@" "@@RESOURCES_DIR@@" \
            && cd "@@RESOURCES_DIR@@" \
            && git sparse-checkout init --cone \
            && git sparse-checkout set @@RESOURCES_SPARSE@@ \
            && git @@GIT_SLOW_GUARD@@ checkout; then
            cd @@REPO_DIR@@
            return 0
        fi
        cd @@REPO_DIR@@
        _i=$((_i + 1))
        echo "prepare.sh: resources fetch attempt $_i failed"
        find "@@RESOURCES_DIR@@" -mindepth 1 -delete 2>/dev/null || true
        if [ "$_i" -ge 5 ]; then return 1; fi
        sleep 20
    done
}

_fetch_regions() {
    mkdir -p "$(dirname @@REGIONS_CACHE@@)"
    if [ -s "@@REGIONS_CACHE@@" ]; then return 0; fi
    curl @@CURL_OPTS@@ -o "@@REGIONS_CACHE@@" "@@REGIONS_OCBF_URL@@"
}"""

_SEED_REGIONS = r"""_seed_regions() {
    if [ -f "@@REGIONS_CACHE@@" ]; then
        mkdir -p "$(dirname @@REPO_DIR@@/@@REGIONS_REL@@)"
        cp -f "@@REGIONS_CACHE@@" "@@REPO_DIR@@/@@REGIONS_REL@@"
    fi
}"""

_DERIVE_TARGETS = r"""_PATHS="$(sed -nE 's#^diff --git a/.* b/(.*)$#\1#p' /home/test.patch | sort -u || true)"

SHARED_SEL=""
JAVA_SEL=""
ANDROID_SEL=""

for _p in $_PATHS; do
    case "$_p" in
        @@SHARED_MODULE@@/src/*Test/*)
            SHARED_SEL="yes"
            ;;
        @@JAVA_MODULE@@/src/test/*)
            JAVA_SEL="yes"
            ;;
        @@ANDROID_MODULE@@/test/*)
            ANDROID_SEL="$ANDROID_SEL $_p"
            ;;
    esac
done

ANDROID_SEL="$(printf '%s\n' $ANDROID_SEL | sed '/^$/d' | sort -u | paste -sd' ' -)"
"""

_RUN_TARGETS = r"""if [ -n "$ANDROID_SEL" ]; then
    echo "ANDROID_INSTRUMENTATION_TESTS_NOT_RUNNABLE=$ANDROID_SEL"
fi

if [ -z "$SHARED_SEL" ] && [ -z "$JAVA_SEL" ]; then
    echo "NO_TEST_MODULES_AT_THIS_STAGE"
    exit 0
fi

cd @@REPO_DIR@@
find . -path '*/build/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

_seed_regions

GRADLE_RC=0

if [ -n "$SHARED_SEL" ]; then
    echo "SELECTED_GRADLE_TASK=@@SHARED_TASK@@"
    @@GRADLE_TIMEOUT@@ ./gradlew @@GRADLE_FLAGS@@ @@SHARED_TASK@@ || GRADLE_RC=$?
fi

if [ -n "$JAVA_SEL" ]; then
    echo "SELECTED_GRADLE_TASK=@@JAVA_TASK@@"
    @@GRADLE_TIMEOUT@@ ./gradlew @@GRADLE_FLAGS@@ @@JAVA_TASK@@ \
        -x :@@JAVA_MODULE@@:collectRegionsInfoResources || GRADLE_RC=$?
fi

find . -path '*/build/test-results/*' -name 'TEST-*.xml' -exec cat {} +

if [ "$GRADLE_RC" -ne 0 ]; then
    echo "GRADLE_EXIT_CODE=$GRADLE_RC"
fi
"""

_PREPARE = r"""#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_USER_HOME=/root/.gradle
export ANDROID_HOME=@@ANDROID_HOME@@
export ANDROID_SDK_ROOT=@@ANDROID_HOME@@

[ -n "${http_proxy:-}" ] || unset http_proxy
[ -n "${https_proxy:-}" ] || unset https_proxy
[ -n "${HTTP_PROXY:-}" ] || unset HTTP_PROXY
[ -n "${HTTPS_PROXY:-}" ] || unset HTTPS_PROXY

cd @@REPO_DIR@@

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "@@SHA@@"
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "@@SHA@@"

@@SEED_REGIONS@@

@@DERIVE_TARGETS@@

echo "prepare.sh: shared=[$SHARED_SEL] java=[$JAVA_SEL] android=[$ANDROID_SEL]"

_gradle_retry() {
    local _max="$1"
    shift
    local _i=1
    while :; do
        if "$@"; then return 0; fi
        if [ "$_i" -ge "$_max" ]; then return 1; fi
        echo "prepare.sh: gradle attempt $_i failed, backing off $((_i * 45))s"
        sleep $((_i * 45))
        _i=$((_i + 1))
    done
}

chmod +x ./gradlew

@@FETCH_DEPS@@

_fetch_resources || true
_fetch_regions || true
_seed_regions

_gradle_retry 6 @@GRADLE_TIMEOUT_WARMUP@@ ./gradlew @@GRADLE_FLAGS@@ --version || true

if _gradle_retry 3 @@GRADLE_TIMEOUT_PREPARE@@ ./gradlew @@GRADLE_FLAGS@@ projects; then
    echo "PREPARE_GRADLE_CONFIGURE=OK"
else
    echo "PREPARE_GRADLE_CONFIGURE=FAILED"
fi

if [ -n "$SHARED_SEL" ]; then
    _gradle_retry 3 @@GRADLE_TIMEOUT_PREPARE@@ ./gradlew @@GRADLE_FLAGS@@ @@SHARED_TASK@@ || true
fi

if [ -n "$JAVA_SEL" ]; then
    _gradle_retry 3 @@GRADLE_TIMEOUT_PREPARE@@ ./gradlew @@GRADLE_FLAGS@@ @@JAVA_TASK@@ \
        -x :@@JAVA_MODULE@@:collectRegionsInfoResources || true
fi

cd @@REPO_DIR@@
git clean -fdx
git reset --hard
test "$(git rev-parse HEAD)" = "@@SHA@@"

_seed_regions

java -version
./gradlew @@GRADLE_FLAGS@@ --version
test -d @@RESOURCES_DIR@@/routing
test -d @@RESOURCES_DIR@@/test-resources
test -s @@REGIONS_CACHE@@
test -s @@REPO_DIR@@/@@REGIONS_REL@@
test -d @@ANDROID_HOME@@/platforms/android-@@ANDROID_API@@
test -d @@ANDROID_HOME@@/build-tools/@@ANDROID_BUILD_TOOLS@@

cd @@REPO_DIR@@
bash /home/check_git_changes.sh
echo "DEPS_OK"
"""

_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_USER_HOME=/root/.gradle
export ANDROID_HOME=@@ANDROID_HOME@@
export ANDROID_SDK_ROOT=@@ANDROID_HOME@@

[ -n "${http_proxy:-}" ] || unset http_proxy
[ -n "${https_proxy:-}" ] || unset https_proxy
[ -n "${HTTP_PROXY:-}" ] || unset HTTP_PROXY
[ -n "${HTTPS_PROXY:-}" ] || unset HTTPS_PROXY

@@SEED_REGIONS@@

cd @@REPO_DIR@@

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

_TEST_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_USER_HOME=/root/.gradle
export ANDROID_HOME=@@ANDROID_HOME@@
export ANDROID_SDK_ROOT=@@ANDROID_HOME@@

[ -n "${http_proxy:-}" ] || unset http_proxy
[ -n "${https_proxy:-}" ] || unset https_proxy
[ -n "${HTTP_PROXY:-}" ] || unset HTTP_PROXY
[ -n "${HTTPS_PROXY:-}" ] || unset HTTPS_PROXY

@@APPLY_FN@@

@@SEED_REGIONS@@

cd @@REPO_DIR@@

_apply_patches /home/test.patch

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

_FIX_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_USER_HOME=/root/.gradle
export ANDROID_HOME=@@ANDROID_HOME@@
export ANDROID_SDK_ROOT=@@ANDROID_HOME@@

[ -n "${http_proxy:-}" ] || unset http_proxy
[ -n "${https_proxy:-}" ] || unset https_proxy
[ -n "${HTTP_PROXY:-}" ] || unset HTTP_PROXY
[ -n "${HTTPS_PROXY:-}" ] || unset HTTPS_PROXY

@@APPLY_FN@@

@@SEED_REGIONS@@

cd @@REPO_DIR@@

_apply_patches /home/test.patch /home/fix.patch

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM @@IMAGE@@

ARG TARGETARCH
ARG REPO_URL="https://github.com/@@ORG@@/@@REPO@@.git"
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
@@GLOBAL_ENV@@
LABEL org.opencontainers.image.title="@@ORG@@/@@REPO@@" \
      org.opencontainers.image.description="@@ORG@@/@@REPO@@ Docker image" \
      org.opencontainers.image.source="https://github.com/@@ORG@@/@@REPO@@" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN mkdir -p /usr/share/binfmts && \
    apt-get update && \
    apt-get install -y --no-install-recommends @@APT@@ && \
    rm -rf /var/lib/apt/lists/*

ENV ANDROID_HOME=@@ANDROID_HOME@@ \
    ANDROID_SDK_ROOT=@@ANDROID_HOME@@ \
    GRADLE_USER_HOME=/root/.gradle \
    GRADLE_OPTS="@@GRADLE_OPTS@@"

RUN set -eux; \
    mkdir -p "${ANDROID_HOME}/cmdline-tools"; \
    curl @@CURL_OPTS@@ -o /tmp/cmdline-tools.zip "https://dl.google.com/android/repository/@@CMDLINE_TOOLS_ZIP@@"; \
    echo "@@CMDLINE_TOOLS_SHA1@@  /tmp/cmdline-tools.zip" | sha1sum -c -; \
    unzip -q /tmp/cmdline-tools.zip -d "${ANDROID_HOME}/cmdline-tools"; \
    mv "${ANDROID_HOME}/cmdline-tools/cmdline-tools" "${ANDROID_HOME}/cmdline-tools/latest"; \
    ln -s "${ANDROID_HOME}/cmdline-tools/latest/bin/sdkmanager" /usr/local/bin/sdkmanager; \
    rm -f /tmp/cmdline-tools.zip; \
    yes | @@NO_PROXY_ENV@@ sdkmanager --licenses > /tmp/lic.log 2>&1 || { tail -40 /tmp/lic.log; exit 1; }; \
    @@NO_PROXY_ENV@@ sdkmanager "platform-tools" "platforms;android-@@ANDROID_API@@" "build-tools;@@ANDROID_BUILD_TOOLS@@" > /tmp/sdk.log 2>&1 || { tail -40 /tmp/sdk.log; exit 1; }; \
    test -f "${ANDROID_HOME}/platforms/android-@@ANDROID_API@@/android.jar"; \
    test -f "${ANDROID_HOME}/build-tools/@@ANDROID_BUILD_TOOLS@@/aapt2"; \
    java -version; \
    @@NO_PROXY_ENV@@ sdkmanager --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

@@CODE@@

WORKDIR /home/@@REPO@@
@@CLEAR_ENV@@
CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM @@BASE_IMAGE@@

ARG BASE_COMMIT="@@SHA@@"
@@GLOBAL_ENV@@
@@COPY@@

RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

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
@@CLEAR_ENV@@
"""


def _render(body: str, values: dict) -> str:
    for key, value in values.items():
        body = body.replace("@@%s@@" % key, value)
    return body


def _optional_block(block: str) -> str:
    return "\n%s\n" % block if block else ""


class OsmAndImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = (
                'RUN git clone "${REPO_URL}" /home/%s && \\\n    cd /home/%s && git rev-parse HEAD >/dev/null'
                % (
                    self.pr.repo,
                    self.pr.repo,
                )
            )
        else:
            code = "COPY %s /home/%s" % (self.pr.repo, self.pr.repo)

        return _render(
            _BASE_DOCKERFILE,
            {
                "IMAGE": str(self.dependency()),
                "ORG": self.pr.org,
                "REPO": self.pr.repo,
                "APT": _BASE_APT,
                "ANDROID_HOME": _ANDROID_HOME,
                "ANDROID_API": _ANDROID_API,
                "ANDROID_BUILD_TOOLS": _ANDROID_BUILD_TOOLS,
                "CMDLINE_TOOLS_ZIP": _CMDLINE_TOOLS_ZIP,
                "CMDLINE_TOOLS_SHA1": _CMDLINE_TOOLS_SHA1,
                "GRADLE_OPTS": _GRADLE_OPTS,
                "CURL_OPTS": _CURL_OPTS,
                "NO_PROXY_ENV": _NO_PROXY_ENV,
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class OsmAndImageDefault(Image):
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
        return OsmAndImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-%d" % self.pr.number

    def workdir(self) -> str:
        return "pr-%d" % self.pr.number

    def _values(self) -> dict:
        repo_dir = "/home/%s" % self.pr.repo
        return {
            "REPO_DIR": repo_dir,
            "SHA": self.pr.base.sha,
            "ANDROID_HOME": _ANDROID_HOME,
            "ANDROID_API": _ANDROID_API,
            "ANDROID_BUILD_TOOLS": _ANDROID_BUILD_TOOLS,
            "RESOURCES_DIR": "/home/%s" % _RESOURCES_DIR,
            "RESOURCES_REPO": _RESOURCES_REPO,
            "RESOURCES_SPARSE": _RESOURCES_SPARSE,
            "REGIONS_OCBF_URL": _REGIONS_OCBF_URL,
            "REGIONS_CACHE": _REGIONS_CACHE,
            "REGIONS_REL": _REGIONS_REL,
            "SHARED_MODULE": _SHARED_MODULE,
            "SHARED_TASK": _SHARED_TASK,
            "JAVA_MODULE": _JAVA_MODULE,
            "JAVA_TASK": _JAVA_TASK,
            "ANDROID_MODULE": _ANDROID_MODULE,
            "GRADLE_FLAGS": _GRADLE_FLAGS,
            "GRADLE_TIMEOUT": _GRADLE_TIMEOUT,
            "GRADLE_TIMEOUT_PREPARE": _GRADLE_TIMEOUT_PREPARE,
            "GRADLE_TIMEOUT_WARMUP": _GRADLE_TIMEOUT_WARMUP,
            "CURL_OPTS": _CURL_OPTS,
            "GIT_SLOW_GUARD": _GIT_SLOW_GUARD,
        }

    def _script(self, body: str) -> str:
        body = _render(
            body,
            {
                "APPLY_FN": _APPLY_FN,
                "FETCH_DEPS": _FETCH_DEPS,
                "SEED_REGIONS": _SEED_REGIONS,
                "DERIVE_TARGETS": _DERIVE_TARGETS,
                "RUN_TARGETS": _RUN_TARGETS,
            },
        )
        return _render(body, self._values())

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
        copy_commands = "\n".join("COPY %s /home/" % f.name for f in self.files())
        values = self._values()
        values.update(
            {
                "BASE_IMAGE": self.dependency().image_full_name(),
                "COPY": copy_commands,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            }
        )
        return _render(_PR_DOCKERFILE, values)


def parse_junit_xml(test_log: str) -> TestResult:
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
    name_re = re.compile(r'\bname="([^"]*)"')
    classname_re = re.compile(r'\bclassname="([^"]*)"')
    file_re = re.compile(r'\bfile="([^"]*)"')

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
        file_match = file_re.search(attrs)
        file_path = file_match.group(1) if file_match else ""
        if file_path:
            test_id = "%s::%s#%s" % (file_path, classname, name) if classname else "%s::%s" % (file_path, name)
        else:
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


@Instance.register(_ORG, _REPO)
class OSMAND(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return OsmAndImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_junit_xml(test_log)
