import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-4431_to_4359"

_BASE_IMAGE = "maven:3.9.5-eclipse-temurin-17"

_MAVEN_OPTS = "-Xmx3g -XX:MaxMetaspaceSize=512m"

_MODULE_DIR = "h2"

_MVN_TEST_FLAGS = (
    "-Dcheckstyle.skip=true "
    "-Drat.skip=true "
    "-Dspotbugs.skip=true "
    "-Dlicense.skip=true "
    "-Denforcer.skip=true "
    "-Djacoco.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dgpg.skip=true "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120 "
    "-Dmaven.wagon.rto=120000"
)

_PATCH_EXCLUDES = (
    "--exclude='*.png' "
    "--exclude='*.jpg' "
    "--exclude='*.jpeg' "
    "--exclude='*.gif' "
    "--exclude='*.ico' "
    "--exclude='*.eot' "
    "--exclude='*.ttf' "
    "--exclude='*.otf' "
    "--exclude='*.woff' "
    "--exclude='*.woff2' "
    "--exclude='*.jar' "
    "--exclude='*.zip'"
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

__GLOBAL_ENV__

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    CI=true \
    JAVA_HOME=/opt/java/openjdk \
    MAVEN_OPTS="__MAVEN_OPTS__" \
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

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

__CLONE__

WORKDIR /home/__REPO__
RUN git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""
WORKDIR /home/

__CLEAR_ENV__

CMD ["/bin/bash"]
"""

_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout __BASE_SHA__

__COPY_COMMANDS__
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

RUN bash /home/prepare.sh

__CLEAR_ENV__
"""

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -euo pipefail

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

_COLLECT_REPORTS_SH = r"""echo "test-result xml files: $(find . -path '*/target/surefire-reports/TEST-*.xml' | wc -l | tr -d ' ')"
echo "===== JUNIT REPORTS BEGIN ====="
find . -path '*/target/surefire-reports/TEST-*.xml' | sort | while read -r f; do
    cat "$f"
    echo
done
echo "===== JUNIT REPORTS END ====="
"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

export CI=true
export TZ=UTC
export MAVEN_OPTS="__MAVEN_OPTS__"

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

cd /home/__REPO__/__MODULE_DIR__

timeout 1800 mvn -B dependency:go-offline __MVN_FLAGS__ > /tmp/go-offline.log 2>&1 || true

attempt=1
until timeout 7200 mvn -B test __MVN_FLAGS__ > /tmp/warm-mvn.log 2>&1; do
    if [ "$attempt" -ge 3 ]; then
        break
    fi
    sleep "$((attempt * 15))"
    attempt=$((attempt + 1))
done

sed -i '/<proxies>/,/<\/proxies>/d' /root/.m2/settings.xml

find . -path '*/target/surefire-reports/TEST-*.xml' -delete
set +e
timeout 7200 mvn -o -B test __MVN_FLAGS__ > /tmp/gate-mvn.log 2>&1
GATE_STATUS=$?
set -e
GATE_REPORTS="$(find . -path '*/target/surefire-reports/TEST-*.xml' | wc -l | tr -d ' ')"
GATE_CLASSES="$(grep -cE '^([0-9]{2}:[0-9]{2}:[0-9]{2}[[:space:]]+)?([0-9]+:)?[0-9]{2}:[0-9]{2}\.[0-9]{3}[[:space:]]+org\.h2\.' /tmp/gate-mvn.log || true)"
echo "GATE_MVN_EXIT=$GATE_STATUS"
echo "GATE_JUNIT_REPORTS=$GATE_REPORTS"
echo "GATE_H2_TEST_CLASSES=$GATE_CLASSES"

cd /home/__REPO__
git reset --hard
git clean -fdx

! grep -qE 'Could not resolve dependencies|Could not transfer artifact|Cannot access .* in offline mode' /tmp/gate-mvn.log
test "$GATE_CLASSES" -gt 0
echo "DEPS_OK"
"""

_RUN_BODY_SH = r"""
cd /home/__REPO__/__MODULE_DIR__

find . -path '*/target/surefire-reports/TEST-*.xml' -delete

set +e
timeout 7200 mvn -o -B test __MVN_FLAGS__ 2>&1 | tee /tmp/mvn-stage.log
MVN_STATUS=${PIPESTATUS[0]}
set -e

__COLLECT_REPORTS__
exit $MVN_STATUS
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export MAVEN_OPTS="__MAVEN_OPTS__"
cd /home/__REPO__
__RUN_BODY__"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export MAVEN_OPTS="__MAVEN_OPTS__"
cd /home/__REPO__

git apply --3way --whitespace=nowarn __PATCH_EXCLUDES__ /home/test.patch
__RUN_BODY__"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export MAVEN_OPTS="__MAVEN_OPTS__"
cd /home/__REPO__

git apply --3way --whitespace=nowarn __PATCH_EXCLUDES__ /home/test.patch /home/fix.patch
__RUN_BODY__"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_H2_CLASS_RE = re.compile(
    r"^(?:\d{2}:\d{2}:\d{2}\s+)?"
    r"(?:\d+:)?\d{2}:\d{2}\.\d{3}\s+"
    r"(org\.h2\.\S+?)"
    r"(?:\s+\d+/\d+)?"
    r"\s*(FAIL\b.*)?$"
)


def h2database_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

    re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
    re_attr = re.compile(r'(\w+)="([^"]*)"')

    for match in re_case.finditer(clean_log):
        attrs = dict(re_attr.findall(match.group(1)))
        method = attrs.get("name", "")
        classname = attrs.get("classname", "")
        if not method or not classname:
            continue
        name = f"{classname}#{method}"
        body = match.group(3) or ""
        if "<failure" in body or "<error" in body:
            failed_tests.add(name)
        elif "<skipped" in body:
            skipped_tests.add(name)
        else:
            passed_tests.add(name)

    for line in clean_log.splitlines():
        match = _H2_CLASS_RE.match(line)
        if not match:
            continue
        name = match.group(1).strip()
        if not name or name.endswith("."):
            continue
        if match.group(2):
            failed_tests.add(name)
        else:
            passed_tests.add(name)

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


class H2databaseImageBase(Image):
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
            clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", _BASE_IMAGE)
            .replace("__MAVEN_OPTS__", _MAVEN_OPTS)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CLONE__", clone)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class H2databaseImageDefault(Image):
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
        return H2databaseImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        run_body = _RUN_BODY_SH.replace(
            "__COLLECT_REPORTS__", _COLLECT_REPORTS_SH
        )
        return (
            template.replace("__RUN_BODY__", run_body)
            .replace("__MVN_FLAGS__", _MVN_TEST_FLAGS)
            .replace("__PATCH_EXCLUDES__", _PATCH_EXCLUDES)
            .replace("__MAVEN_OPTS__", _MAVEN_OPTS)
            .replace("__MODULE_DIR__", _MODULE_DIR)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__REPO__", self.pr.repo)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
            _DEFAULT_DOCKERFILE.replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__REPO__", self.pr.repo)
        )


@Instance.register("h2database", "h2database_4431_to_4359")
class H2DATABASE_4431_TO_4359(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return H2databaseImageDefault(self.pr, self._config)

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
        return h2database_parse_log(test_log)


Instance.register("h2database", "h2database")(H2DATABASE_4431_TO_4359)
