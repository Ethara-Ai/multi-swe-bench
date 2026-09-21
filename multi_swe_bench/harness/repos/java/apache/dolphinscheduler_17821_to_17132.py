import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-17821_to_17132"

_MVN_TEST_FLAGS = (
    "-Dcheckstyle.skip=true "
    "-Drat.skip=true "
    "-Dspotbugs.skip=true "
    "-Dlicense.skip=true "
    "-Dspotless.check.skip=true "
    "-Denforcer.skip=true "
    "-Ddependency-check.skip=true "
    "-Djacoco.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-DreuseForks=false "
    "-DskipUT=false "
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120 "
    "-Dmaven.wagon.rto=120000"
)

_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

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

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN --mount=type=secret,id=mitm_ca,required=0 \
    if [ -f /run/secrets/mitm_ca ]; then \
        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \
    fi

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__

WORKDIR /home/__REPO__

CMD ["/bin/bash"]
"""

_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \
    cd /home/__REPO__; \
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
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
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
"""

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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

_DERIVE_TARGETS_SH = r"""TEST_PATHS="$(grep '^diff --git a/' /home/test.patch | sed -e 's|^diff --git a/||' -e 's| b/.*$||' || true)"
MODULES="$(printf '%s\n' "$TEST_PATHS" | grep '/src/test/' | cut -d/ -f1 | sort -u || true)"
JAVA_TEST_PATHS="$(printf '%s\n' "$TEST_PATHS" | grep '/src/test/java/.*\.java$' || true)"
CLASSES="$(printf '%s\n' "$JAVA_TEST_PATHS" | sed -e 's|.*/||' -e 's|\.java$||' | sort -u || true)"
SEL=""
for module in $MODULES; do
    if [ -f "${module}/pom.xml" ]; then
        SEL="${SEL:+${SEL},}${module}"
    fi
done
CLASSES="$(printf '%s\n' "$CLASSES" | paste -sd, -)"
"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach "__BASE_SHA__"
bash /home/check_git_changes.sh

export CI=true
export MAVEN_OPTS="-Xmx2g -XX:MaxMetaspaceSize=512m"

mkdir -p /root/.m2
printf '%s\n' \
    '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' \
    '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' \
    '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' \
    '</settings>' > /root/.m2/settings.xml

proxy_url="${http_proxy:-${https_proxy:-}}"
if [ -n "$proxy_url" ]; then
    proxy_stripped="${proxy_url#*://}"
    proxy_stripped="${proxy_stripped%%/*}"
    proxy_stripped="${proxy_stripped##*@}"
    proxy_host="${proxy_stripped%%:*}"
    proxy_port="${proxy_stripped##*:}"
    if [ "$proxy_port" = "$proxy_stripped" ]; then
        proxy_port="80"
    fi
    proxy_nonhosts="$(printf '%s' "${no_proxy:-localhost,127.0.0.1}" | tr ',' '|')"
    sed -i '$d' /root/.m2/settings.xml
    printf '%s\n' \
        '    <proxies>' \
        '        <proxy>' \
        '            <id>build-proxy</id>' \
        '            <active>true</active>' \
        '            <protocol>http</protocol>' \
        "            <host>${proxy_host}</host>" \
        "            <port>${proxy_port}</port>" \
        "            <nonProxyHosts>${proxy_nonhosts}</nonProxyHosts>" \
        '        </proxy>' \
        '    </proxies>' \
        '</settings>' >> /root/.m2/settings.xml
fi

__DERIVE_TARGETS__

test -n "$SEL"
test -n "$CLASSES"

attempt=1
until timeout 3600 mvn -B test -pl "$SEL" -am -Dtest="$CLASSES" __MVN_FLAGS__ > /tmp/warm-mvn.log 2>&1; do
    if [ "$attempt" -ge 3 ]; then
        echo "maven warm build failed after 3 attempts" >&2
        exit 1
    fi
    sleep "$((attempt * 15))"
    attempt=$((attempt + 1))
done

sed -i '/<proxies>/,/<\/proxies>/d' /root/.m2/settings.xml

set +e
timeout 3600 mvn -o -B test -pl "$SEL" -am -Dtest="$CLASSES" __MVN_FLAGS__ > /tmp/gate-mvn.log 2>&1
GATE_STATUS=$?
set -e
echo "GATE_MVN_EXIT=$GATE_STATUS"
test "$GATE_STATUS" -eq 0
grep -q "Tests run:" /tmp/gate-mvn.log
echo "DEPS_OK"
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx2g -XX:MaxMetaspaceSize=512m"

cd /home/__REPO__

__DERIVE_TARGETS__

test -n "$SEL"
test -n "$CLASSES"

find . -type d -name surefire-reports -prune -exec rm -rf {} +

set +e
timeout 3600 mvn -o -B test -pl "$SEL" -am -Dtest="$CLASSES" __MVN_FLAGS__ 2>&1 | tee /tmp/mvn-stage.log
MVN_STATUS=${PIPESTATUS[0]}
set -e

echo "modules ok: $(grep -cE '^\[INFO\] .+ SUCCESS \[' /tmp/mvn-stage.log || true)"
echo "modules failed: $(grep -cE '^\[INFO\] .+ FAILURE \[' /tmp/mvn-stage.log || true)"
echo "surefire reports: $(find . -path '*/target/surefire-reports/TEST-*.xml' | wc -l)"
echo "===== SUREFIRE REPORTS BEGIN ====="
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} +
echo "===== SUREFIRE REPORTS END ====="

exit $MVN_STATUS
"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx2g -XX:MaxMetaspaceSize=512m"

cd /home/__REPO__

__DERIVE_TARGETS__

test -n "$SEL"
test -n "$CLASSES"

git apply --whitespace=nowarn /home/test.patch

find . -type d -name surefire-reports -prune -exec rm -rf {} +

set +e
timeout 3600 mvn -o -B test -pl "$SEL" -am -Dtest="$CLASSES" __MVN_FLAGS__ 2>&1 | tee /tmp/mvn-stage.log
MVN_STATUS=${PIPESTATUS[0]}
set -e

echo "modules ok: $(grep -cE '^\[INFO\] .+ SUCCESS \[' /tmp/mvn-stage.log || true)"
echo "modules failed: $(grep -cE '^\[INFO\] .+ FAILURE \[' /tmp/mvn-stage.log || true)"
echo "surefire reports: $(find . -path '*/target/surefire-reports/TEST-*.xml' | wc -l)"
echo "===== SUREFIRE REPORTS BEGIN ====="
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} +
echo "===== SUREFIRE REPORTS END ====="

exit $MVN_STATUS
"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx2g -XX:MaxMetaspaceSize=512m"

cd /home/__REPO__

__DERIVE_TARGETS__

test -n "$SEL"
test -n "$CLASSES"

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

find . -type d -name surefire-reports -prune -exec rm -rf {} +

set +e
timeout 3600 mvn -o -B test -pl "$SEL" -am -Dtest="$CLASSES" __MVN_FLAGS__ 2>&1 | tee /tmp/mvn-stage.log
MVN_STATUS=${PIPESTATUS[0]}
set -e

echo "modules ok: $(grep -cE '^\[INFO\] .+ SUCCESS \[' /tmp/mvn-stage.log || true)"
echo "modules failed: $(grep -cE '^\[INFO\] .+ FAILURE \[' /tmp/mvn-stage.log || true)"
echo "surefire reports: $(find . -path '*/target/surefire-reports/TEST-*.xml' | wc -l)"
echo "===== SUREFIRE REPORTS BEGIN ====="
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} +
echo "===== SUREFIRE REPORTS END ====="

exit $MVN_STATUS
"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def dolphinscheduler_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

    re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
    re_attr = re.compile(r'(\w+)="([^"]*)"')

    for match in re_case.finditer(clean_log):
        attrs = dict(re_attr.findall(match.group(1)))
        method = attrs.get("name", "")
        if not method:
            continue
        classname = attrs.get("classname", "")
        name = f"{classname}#{method}" if classname else method
        body = match.group(3) or ""
        if "<failure" in body or "<error" in body:
            failed_tests.add(name)
        elif "<skipped" in body:
            skipped_tests.add(name)
        else:
            passed_tests.add(name)

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


class DolphinschedulerImageBase(Image):

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
        return "maven:3.8.4-eclipse-temurin-8"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class DolphinschedulerImageDefault(Image):

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
        return DolphinschedulerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__DERIVE_TARGETS__", _DERIVE_TARGETS_SH)
            .replace("__MVN_FLAGS__", _MVN_TEST_FLAGS)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return (
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__COPY_COMMANDS__", copy_commands)
        )


@Instance.register("apache", "dolphinscheduler_17821_to_17132")
class DOLPHINSCHEDULER_17821_TO_17132(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DolphinschedulerImageDefault(self.pr, self._config)

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
        return dolphinscheduler_parse_log(test_log)
