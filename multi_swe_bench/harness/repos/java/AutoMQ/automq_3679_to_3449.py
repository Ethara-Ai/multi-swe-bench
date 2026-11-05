import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-3679_to_3449"

_BASE_IMAGE = "eclipse-temurin:17-jdk-jammy"

_GRADLE_TASKS = "metadata:S3UnitTest core:S3UnitTest s3stream:test"

_GRADLE_FLAGS = (
    "--no-daemon "
    "--console=plain "
    "--continue "
    "--build-cache "
    "-PignoreFailures=true "
    "-PmaxParallelForks=2 "
    "-PmaxTestRetries=0 "
    "-Dorg.gradle.parallel=false "
    "-x checkstyleMain "
    "-x checkstyleTest "
    "-x spotbugsMain "
    "-x spotbugsTest "
    "-x rat "
    "-x spotlessJavaCheck"
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
    "--exclude='gradle/wrapper/gradle-wrapper.jar'"
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
    GRADLE_USER_HOME=/root/.gradle \
    GRADLE_OPTS="-Dorg.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8" \
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
    unzip \
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

_COLLECT_REPORTS_SH = r"""echo "test-result xml files: $(find . -path '*/build/test-results/*/TEST-*.xml' | wc -l | tr -d ' ')"
echo "===== JUNIT REPORTS BEGIN ====="
find . -path '*/build/test-results/*/TEST-*.xml' | sort | while read -r f; do
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
export GRADLE_USER_HOME=/root/.gradle

mkdir -p "$GRADLE_USER_HOME"

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
    printf '%s\n' \
        "systemProp.http.proxyHost=${proxy_host}" \
        "systemProp.http.proxyPort=${proxy_port}" \
        "systemProp.https.proxyHost=${proxy_host}" \
        "systemProp.https.proxyPort=${proxy_port}" \
        "systemProp.http.nonProxyHosts=${proxy_nonhosts}" \
        "systemProp.https.nonProxyHosts=${proxy_nonhosts}" \
        > "$GRADLE_USER_HOME/gradle.properties"
fi

attempt=1
until timeout 7200 ./gradlew __GRADLE_FLAGS__ __GRADLE_TASKS__ > /tmp/warm-gradle.log 2>&1; do
    if [ "$attempt" -ge 3 ]; then
        break
    fi
    sleep "$((attempt * 15))"
    attempt=$((attempt + 1))
done

rm -f "$GRADLE_USER_HOME/gradle.properties"

find . -path '*/build/test-results/*' -name 'TEST-*.xml' -delete
set +e
timeout 7200 ./gradlew --offline __GRADLE_FLAGS__ __GRADLE_TASKS__ > /tmp/gate-gradle.log 2>&1
GATE_STATUS=$?
set -e
GATE_REPORTS="$(find . -path '*/build/test-results/*/TEST-*.xml' | wc -l | tr -d ' ')"
echo "GATE_GRADLE_EXIT=$GATE_STATUS"
echo "GATE_JUNIT_REPORTS=$GATE_REPORTS"

git reset --hard
git clean -fdx -e build -e '**/build' -e .gradle

! grep -qE 'No cached version of|Could not resolve all (files|dependencies)|No such host is known' /tmp/gate-gradle.log
test "$GATE_REPORTS" -gt 0
echo "DEPS_OK"
"""

_RUN_BODY_SH = r"""
find . -path '*/build/test-results/*' -name 'TEST-*.xml' -delete

set +e
timeout 7200 ./gradlew --offline __GRADLE_FLAGS__ __GRADLE_TASKS__ 2>&1 | tee /tmp/gradle-stage.log
GRADLE_STATUS=${PIPESTATUS[0]}
set -e

__COLLECT_REPORTS__
exit $GRADLE_STATUS
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export GRADLE_USER_HOME=/root/.gradle
cd /home/__REPO__
__RUN_BODY__"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export GRADLE_USER_HOME=/root/.gradle
cd /home/__REPO__

git apply --3way --whitespace=nowarn __PATCH_EXCLUDES__ /home/test.patch
__RUN_BODY__"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export GRADLE_USER_HOME=/root/.gradle
cd /home/__REPO__

git apply --3way --whitespace=nowarn __PATCH_EXCLUDES__ /home/test.patch /home/fix.patch
__RUN_BODY__"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def automq_parse_log(test_log: str) -> TestResult:
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


class AutomqImageBase(Image):
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
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CLONE__", clone)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class AutomqImageDefault(Image):
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
        return AutomqImageBase(self.pr, self._config)

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
            .replace("__GRADLE_FLAGS__", _GRADLE_FLAGS)
            .replace("__GRADLE_TASKS__", _GRADLE_TASKS)
            .replace("__PATCH_EXCLUDES__", _PATCH_EXCLUDES)
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


@Instance.register("AutoMQ", "automq_3679_to_3449")
class AUTOMQ_3679_TO_3449(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return AutomqImageDefault(self.pr, self._config)

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
        return automq_parse_log(test_log)


Instance.register("AutoMQ", "automq")(AUTOMQ_3679_TO_3449)
