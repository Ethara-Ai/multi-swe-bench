from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.apache.druid_0_to_16976 import (
    _CHECK_GIT_CHANGES_SH,
    _CLONE_CODE,
    _COPY_CODE,
    _EXCLUDED_MODULES,
    _FIX_RUN_SH,
    _MVN_FLAGS,
    _RUN_SH,
    _SHELL_ENV,
    _TEST_BLOCK,
    _extract_test_classes_from_patch,
    _patch_paths,
    _TEST_RUN_SH,
    _build_pl_flag,
    _parse_log,
    _scope_flags,
)

_BASE_TAG = "base-16977_to_18789"
_MAVEN_IMAGE = "maven:3.9.11-eclipse-temurin-25"
_BASE_IMAGE = "eclipse-temurin:25-jdk"
_WARM_ATTEMPTS = 3
_TRANSFER_ERROR_RE = (
    "Could not transfer artifact|Could not resolve dependencies|"
    "Connection timed out|Connection reset|UnknownHostException|"
    "failed to transfer from"
)
_MAVEN_OPTS = "-Xmx2g -Djava.net.preferIPv4Stack=true"
_NET_FLAGS = (
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.http.retryHandler.requestSentEnabled=true "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120 "
    "-Dmaven.wagon.rto=60000"
)
_LOCAL_SHELL_ENV = _SHELL_ENV.replace(
    "MAVEN_OPTS=-Xmx2g", 'MAVEN_OPTS="' + _MAVEN_OPTS + '"'
)


def _graded_classes(pr: PullRequest) -> set:
    classes = set(_extract_test_classes_from_patch(pr.test_patch, _EXCLUDED_MODULES))
    for path in _patch_paths(pr.fix_patch):
        if not path.endswith(".java"):
            continue
        stem = path.rsplit("/", 1)[-1][: -len(".java")]
        classes.add(stem if "/src/test/" in path else stem + "Test")
    return classes


def _pkg_dirs(pr: PullRequest) -> str:
    dirs = set()
    for path in _patch_paths(pr.test_patch) + _patch_paths(pr.fix_patch):
        if not path.endswith(".java"):
            continue
        parent = path.rsplit("/", 1)[0]
        dirs.add(parent.replace("/src/main/java/", "/src/test/java/"))
    return " ".join(sorted(dirs))


def _module_paths(pl_flag: str) -> str:
    if not pl_flag:
        return "."
    parts = pl_flag.split()
    if "-pl" not in parts:
        return "."
    return parts[parts.index("-pl") + 1].replace(",", " ")
_JDK_HOME = "/opt/java/openjdk"


_LOCAL_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

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
    CURL_CA_BUNDLE=${CA_CERT_PATH} \\
    JAVA_HOME=__JDK_HOME__ \\
    MAVEN_OPTS="__MAVEN_OPTS__" \\
    PATH=/opt/maven/bin:__JDK_HOME__/bin:${PATH}

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates curl git \\
    && rm -rf /var/lib/apt/lists/* \\
    && java -version

COPY --from=__MAVEN_IMAGE__ /usr/share/maven /opt/maven

RUN /opt/maven/bin/mvn --version

__GLOBAL_ENV__

__CODE__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


_LOCAL_PREPARE_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

__SHELL_ENV__

java -version
mvn --version

GRADED_TESTS="__SPECIFIC_CSV__"
RUNNABLE_AT_BASE=0
for c in __SPECIFIC_LIST__; do
    for f in $(find __MODULE_PATHS__ -path '*/src/test/java/*' -name "$c.java" 2>/dev/null); do
        if grep -q '@Test' "$f" && ! grep -qE '^[[:space:]]*(public[[:space:]]+)?abstract[[:space:]]+class' "$f"; then
            RUNNABLE_AT_BASE=1
            break
        fi
    done
    if [ "$RUNNABLE_AT_BASE" -eq 1 ]; then
        break
    fi
done
echo "GRADED_SPECIFIC_RUNNABLE_AT_BASE=$RUNNABLE_AT_BASE"
if [ "$RUNNABLE_AT_BASE" -eq 0 ]; then
    EXPAND_DIRS="__PKG_DIRS__"
    if [ -z "$EXPAND_DIRS" ]; then
        EXPAND_DIRS="__MODULE_PATHS__"
    fi
    for f in $(find $EXPAND_DIRS -path '*/src/test/java/*' -name '*Test.java' 2>/dev/null); do
        if grep -q '@Test' "$f" && ! grep -qE '^[[:space:]]*(public[[:space:]]+)?abstract[[:space:]]+class' "$f"; then
            GRADED_TESTS="$GRADED_TESTS,$(basename "$f" .java)"
        fi
    done
fi
echo "$GRADED_TESTS" | sed 's/^,//;s/,$//' > /home/graded_tests.txt
echo "GRADED_TESTS_COUNT=$(awk -F, '{print NF}' /home/graded_tests.txt)"

find . -path '*/target/surefire-reports' -type d -prune -exec rm -rf {} +

WARM_REPORTS=0
WARM_ATTEMPT=0
until [ "$WARM_ATTEMPT" -ge __WARM_ATTEMPTS__ ]; do
    WARM_ATTEMPT=$((WARM_ATTEMPT + 1))
    if [ "$WARM_ATTEMPT" -gt 1 ]; then
        find /root/.m2 -name '*.lastUpdated' -delete 2>/dev/null || true
        mvn clean test __NET_FLAGS__ __MVN_FLAGS__ __SCOPE_FLAGS__ -U > /tmp/warm-mvn.log 2>&1 || true
    else
        mvn clean test __NET_FLAGS__ __MVN_FLAGS__ __SCOPE_FLAGS__ > /tmp/warm-mvn.log 2>&1 || true
    fi
    grep -E '^\\[INFO\\] BUILD|SUCCESS \\[|FAILURE \\[|^\\[ERROR\\]' /tmp/warm-mvn.log | tail -60
    WARM_REPORTS="$(find . -path '*/target/surefire-reports/TEST-*.xml' | wc -l | tr -d ' ')"
    echo "WARM_ATTEMPT=$WARM_ATTEMPT WARM_REPORTS=$WARM_REPORTS"
    if ! grep -qE '__TRANSFER_ERROR_RE__' /tmp/warm-mvn.log; then
        break
    fi
    sleep 15
done

echo "WARM_JUNIT_REPORTS=$WARM_REPORTS"

mvn dependency:go-offline -B __NET_FLAGS__ __PL_FLAG__ > /tmp/go-offline.log 2>&1 || true

SUREFIRE_PROVIDERS="$(find /root/.m2 -name 'surefire-junit*.jar' -o -name 'surefire-testng*.jar' | wc -l | tr -d ' ')"
echo "SUREFIRE_PROVIDERS=$SUREFIRE_PROVIDERS"

if [ "$SUREFIRE_PROVIDERS" -eq 0 ]; then
    FALLBACK_CLASS=""
    for f in $(find __MODULE_PATHS__ -path '*/src/test/java/*' -name '*Test.java' 2>/dev/null | head -60); do
        if grep -q '@Test' "$f" && ! grep -qE '^[[:space:]]*(public[[:space:]]+)?abstract[[:space:]]+class' "$f"; then
            FALLBACK_CLASS="$(basename "$f" .java)"
            break
        fi
    done
    echo "SUREFIRE_FALLBACK_CLASS=$FALLBACK_CLASS"
    if [ -n "$FALLBACK_CLASS" ]; then
        mvn test __NET_FLAGS__ __MVN_FLAGS__ __PL_FLAG__ -Dtest="$FALLBACK_CLASS" > /tmp/fallback-mvn.log 2>&1 || true
        grep -E '^\\[INFO\\] BUILD|Tests run:' /tmp/fallback-mvn.log | tail -5
        SUREFIRE_PROVIDERS="$(find /root/.m2 -name 'surefire-junit*.jar' -o -name 'surefire-testng*.jar' | wc -l | tr -d ' ')"
        echo "SUREFIRE_PROVIDERS_AFTER_FALLBACK=$SUREFIRE_PROVIDERS"
    fi
fi

mvn test-compile -o -B __PL_FLAG__
echo "GATE_MVN_EXIT=0"

test "$SUREFIRE_PROVIDERS" -gt 0

bash /home/check_git_changes.sh
echo "DEPS_OK"
"""

_LOCAL_TEST_BLOCK = _TEST_BLOCK.replace(
    "mvn test __MVN_FLAGS__ __SCOPE_FLAGS__",
    'TEST_FILTER="$(cat /home/graded_tests.txt 2>/dev/null || true)"\n'
    'if [ -n "$TEST_FILTER" ]; then\n'
    '    mvn test __MVN_FLAGS__ __PL_FLAG__ -Dtest="$TEST_FILTER"\n'
    'else\n'
    '    mvn test __MVN_FLAGS__ __PL_FLAG__\n'
    "fi",
)

_LOCAL_PR_DOCKERFILE = """FROM __BASE_IMAGE__

__GLOBAL_ENV__

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout __BASE_SHA__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

__HARDENING__
__CLEAR_ENV__
"""


class DruidJdk11ImageBase(Image):

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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        code = _CLONE_CODE if self.config.need_clone else _COPY_CODE

        return (
            _LOCAL_BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency())
            .replace("__MAVEN_IMAGE__", _MAVEN_IMAGE)
            .replace("__MAVEN_OPTS__", _MAVEN_OPTS)
            .replace("__JDK_HOME__", _JDK_HOME)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class DruidJdk11ImageDefault(Image):

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
        return DruidJdk11ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _LOCAL_TEST_BLOCK)
            .replace("__SHELL_ENV__", _LOCAL_SHELL_ENV)
            .replace("__NET_FLAGS__", _NET_FLAGS)
            .replace("__WARM_ATTEMPTS__", str(_WARM_ATTEMPTS))
            .replace("__TRANSFER_ERROR_RE__", _TRANSFER_ERROR_RE)
            .replace(
                "__MODULE_PATHS__",
                _module_paths(_build_pl_flag(self.pr, _EXCLUDED_MODULES)),
            )
            .replace(
                "__SPECIFIC_CSV__",
                ",".join(f"{c}*" for c in sorted(_graded_classes(self.pr))),
            )
            .replace(
                "__SPECIFIC_LIST__",
                " ".join(sorted(_graded_classes(self.pr))),
            )
            .replace("__PKG_DIRS__", _pkg_dirs(self.pr))
            .replace("__MVN_FLAGS__", _MVN_FLAGS)
            .replace("__SCOPE_FLAGS__", _scope_flags(self.pr))
            .replace("__PL_FLAG__", _build_pl_flag(self.pr, _EXCLUDED_MODULES))
            .replace("__JDK_HOME__", _JDK_HOME)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_LOCAL_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = (
            Image._HARDENING_BLOCK
            .replace(" --aggressive", "")
            .replace('"${BASE_COMMIT}"', self.pr.base.sha)
        )

        return (
            _LOCAL_PR_DOCKERFILE.replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__HARDENING__", hardening)
        )


@Instance.register("apache", "druid_16977_to_18789")
class DruidJdk11(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return DruidJdk11ImageDefault(self.pr, self._config)

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
        return _parse_log(test_log)


def _era_bounds(module_name: str) -> tuple[int, int]:
    parts = module_name.rsplit(".", 1)[-1].split("_")
    numbers = [int(p) for p in parts if p.isdigit()]
    return numbers[0], numbers[1]


_ERA_MIN, _ERA_MAX = _era_bounds(__name__)

_INCUMBENT = Instance._registry.get("apache/druid")


@Instance.register("apache", "druid")
class DruidEraDispatch16977To18789(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _ERA_MIN <= pr.number <= _ERA_MAX:
            return DruidJdk11(pr, config, *args, **kwargs)
        if _INCUMBENT is not None:
            return _INCUMBENT(pr, config, *args, **kwargs)
        raise ValueError(
            f"apache/druid#{pr.number} is outside {_ERA_MIN}-{_ERA_MAX} and no "
            f"other apache/druid adapter owns the bare name"
        )
