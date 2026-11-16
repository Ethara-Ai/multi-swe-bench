import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_IMAGE = "eclipse-temurin:25-jdk"

_MAVEN_OPTS = "-Xmx2g -XX:MaxMetaspaceSize=512m"

_NON_MODULE_DIRS = frozenset(
    {
        ".github",
        "config",
        "src",
    }
)

_MVN_COMMON_FLAGS = (
    "-B "
    "-ntp "
    "-Dmaven.test.skip=false "
    "-Dcheckstyle.skip=true "
    "-Dspotless.check.skip=true "
    "-Dspotless.apply.skip=true "
    "-Dforbiddenapis.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dgpg.skip=true "
    "-Denforcer.skip=true "
    "-Djacoco.skip=true "
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120 "
    "-Dmaven.wagon.rto=120000"
)

_MVN_TEST_FLAGS = "-fn " + _MVN_COMMON_FLAGS

_MVN_GATE_FLAGS = _MVN_COMMON_FLAGS

_REPORT_GLOB = "*/target/junit-platform/*.xml"

_RESOLUTION_FAILURE_RE = (
    "Could not resolve dependencies"
    "|Could not transfer artifact"
    "|Cannot access .* in offline mode"
    "|Non-resolvable parent POM"
    "|Could not find the selected project"
)

_UNSTABLE_TESTS = frozenset(
    {
        "oshi.comparison.NativeComparisonTest.processListStructure()",
        "oshi.comparison.NativeFreeComparisonTest.diskStores()",
        "oshi.software.os.shared.OperatingSystemTest.testGetServices()",
        "oshi.util.driver.linux.SysfsTest.testAtLeastOneSysfsQueryReturnsValue()",
        "oshi.util.driver.linux.proc.ProcessStatTest.testQuerySocketToPidMap()",
    }
)

_PATCH_PATH_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)


def _era_bounds(module_name: str) -> tuple[int, int]:
    parts = module_name.rsplit(".", 1)[-1].split("_")
    numbers = [int(p) for p in parts if p.isdigit()]
    return numbers[0], numbers[1]


_ERA_MIN, _ERA_MAX = _era_bounds(__name__)


def _patch_paths(patch_text: str) -> set[str]:
    paths = set()
    for match in _PATCH_PATH_RE.finditer(patch_text or ""):
        paths.add(match.group(1))
        paths.add(match.group(2))
    return paths


def _module_of(path: str) -> str:
    segments = path.split("/")
    if len(segments) < 2:
        return ""
    top = segments[0]
    if top in _NON_MODULE_DIRS or top.endswith(".md") or top.endswith(".xml"):
        return ""
    return top


def _build_pl_flag(pr: PullRequest) -> str:
    modules = set()
    for path in _patch_paths(pr.fix_patch) | _patch_paths(pr.test_patch):
        module = _module_of(path)
        if module:
            modules.add(module)
    modules.discard("")
    if not modules:
        return ""
    return "-pl " + ",".join(sorted(modules)) + " -am"


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

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
test "$(git rev-parse HEAD)" = "$(git rev-parse __BASE_SHA__)"
bash /home/check_git_changes.sh

chmod +x ./mvnw

java -version
./mvnw --version

find . -path '*/target/junit-platform' -type d -prune -exec rm -rf {} +

attempt=1
WARM_REPORTS=0
while [ "$attempt" -le 3 ]; do
    set +e
    timeout 10800 ./mvnw -U clean test __MVN_FLAGS__ __PL_FLAG__ > /tmp/warm-mvn.log 2>&1
    set -e
    WARM_REPORTS="$(find . -path '__REPORT_GLOB__' | wc -l | tr -d ' ')"
    if [ "$WARM_REPORTS" -gt 0 ]; then
        break
    fi
    sleep "$((attempt * 15))"
    attempt=$((attempt + 1))
done

echo "WARM_JUNIT_REPORTS=$WARM_REPORTS"

set +e
timeout 10800 ./mvnw -o -q test-compile __MVN_GATE_FLAGS__ __PL_FLAG__ > /tmp/gate-mvn.log 2>&1
GATE_STATUS=$?
set -e
echo "GATE_MVN_EXIT=$GATE_STATUS"

if [ "$WARM_REPORTS" -eq 0 ]; then
    tail -120 /tmp/warm-mvn.log
fi
if [ "$GATE_STATUS" -ne 0 ]; then
    tail -120 /tmp/gate-mvn.log
fi

! grep -qE '__RESOLUTION_FAILURE_RE__' /tmp/gate-mvn.log
test "$GATE_STATUS" -eq 0
test "$WARM_REPORTS" -gt 0
echo "DEPS_OK"
"""

_RUN_BODY_SH = r"""
find . -path '*/target/junit-platform' -type d -prune -exec rm -rf {} +

QUARANTINED_SOURCES=0
attempt=1
while [ "$attempt" -le 5 ]; do
    set +e
    timeout 10800 ./mvnw -o test-compile __MVN_GATE_FLAGS__ __PL_FLAG__ > /tmp/precompile.log 2>&1
    PRECOMPILE_STATUS=$?
    grep '^\[ERROR\]' /tmp/precompile.log | grep -oE '[^ :]+/src/test/java/[^ :]+\.java' | sort -u > /tmp/broken-tests.txt
    set -e
    if [ "$PRECOMPILE_STATUS" -eq 0 ]; then
        break
    fi
    if [ ! -s /tmp/broken-tests.txt ]; then
        break
    fi
    while read -r source; do
        git checkout HEAD -- "$source" 2>/dev/null || rm -f "$source"
        QUARANTINED_SOURCES=$((QUARANTINED_SOURCES + 1))
        echo "QUARANTINED_TEST_SOURCE=$source"
    done < /tmp/broken-tests.txt
    find . -path '*/target/test-classes' -type d -prune -exec rm -rf {} +
    attempt=$((attempt + 1))
done

echo "QUARANTINED_TEST_SOURCES=$QUARANTINED_SOURCES"

set +e
timeout 10800 ./mvnw -o test __MVN_FLAGS__ __PL_FLAG__ 2>&1 | tee /tmp/mvn-stage.log
MVN_STATUS=${PIPESTATUS[0]}
set -e

test -n "$(find . -path '__REPORT_GLOB__' -print -quit)"
find . -path '__REPORT_GLOB__' -exec cat {} +

exit ${MVN_STATUS}
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__
__RUN_BODY__
"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
__RUN_BODY__
"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
__RUN_BODY__
"""

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
    MAVEN_OPTS="__MAVEN_OPTS__" \
    TEST_ENV_PROVIDER=oss-source \
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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gnupg \
    procps \
    unzip \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

__CLONE__

WORKDIR /home/__REPO__
RUN git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""
WORKDIR /home/

__CLEAR_ENV__

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM __BASE_FULL_NAME__

__GLOBAL_ENV__

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout __BASE_SHA__

__COPY_COMMANDS__
__PROXY_SETUP__

RUN bash /home/prepare.sh

RUN set -eux; \
    git checkout --detach "__BASE_SHA__"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now; \
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
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
__PROXY_CLEANUP__

__CLEAR_ENV__
"""


class OshiBaseImage(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_ERA_MIN}_to_{_ERA_MAX}"

    def workdir(self) -> str:
        return f"base-{_ERA_MIN}_to_{_ERA_MAX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", _BASE_IMAGE)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__MAVEN_OPTS__", _MAVEN_OPTS)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLONE__", clone)
            .replace("__CLEAR_ENV__", self.clear_env)
        )


class OshiPRImage(Image):
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
        return OshiBaseImage(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render_prepare(self) -> str:
        return (
            _PREPARE_SH.replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__MVN_FLAGS__", _MVN_TEST_FLAGS)
            .replace("__MVN_GATE_FLAGS__", _MVN_GATE_FLAGS)
            .replace("__REPORT_GLOB__", _REPORT_GLOB)
            .replace("__RESOLUTION_FAILURE_RE__", _RESOLUTION_FAILURE_RE)
            .replace("__PL_FLAG__", _build_pl_flag(self.pr))
        )

    def _render_run_body(self) -> str:
        return (
            _RUN_BODY_SH.replace("__MVN_FLAGS__", _MVN_TEST_FLAGS)
            .replace("__MVN_GATE_FLAGS__", _MVN_GATE_FLAGS)
            .replace("__REPORT_GLOB__", _REPORT_GLOB)
            .replace("__PL_FLAG__", _build_pl_flag(self.pr))
        )

    def _render_stage(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__RUN_BODY__", self._render_run_body())
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render_prepare()),
            File(".", "run.sh", self._render_stage(_RUN_SH)),
            File(".", "test-run.sh", self._render_stage(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render_stage(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        base_full_name = f"{image.image_name()}:{image.image_tag()}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None
            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p /root/.m2 && \\
                        if [ ! -f /root/.m2/settings.xml ]; then \\
                            printf '%s\\n' '<settings>' '</settings>' > /root/.m2/settings.xml; \\
                        fi && \\
                        sed -i '$d' /root/.m2/settings.xml && \\
                        printf '%s\\n' \\
                            '    <proxies>' \\
                            '        <proxy>' \\
                            '            <id>build-proxy</id>' \\
                            '            <active>true</active>' \\
                            '            <protocol>http</protocol>' \\
                            '            <host>{proxy_host}</host>' \\
                            '            <port>{proxy_port}</port>' \\
                            '            <nonProxyHosts>localhost,127.0.0.1,::1</nonProxyHosts>' \\
                            '        </proxy>' \\
                            '    </proxies>' \\
                            '</settings>' >> /root/.m2/settings.xml
                    """
                )
                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' /root/.m2/settings.xml
                    """
                )

        return (
            _PR_DOCKERFILE.replace("__BASE_FULL_NAME__", base_full_name)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__PROXY_SETUP__", proxy_setup)
            .replace("__PROXY_CLEANUP__", proxy_cleanup)
            .replace("__CLEAR_ENV__", self.clear_env)
        )


class OshiInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OshiPRImage(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        clean_log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
        re_attr = re.compile(r'(\w+)="([^"]*)"')
        saw_xml = False

        for match in re_case.finditer(clean_log):
            attrs = dict(re_attr.findall(match.group(1)))
            cls = attrs.get("classname", "")
            meth = attrs.get("name", "")
            if not meth:
                continue
            saw_xml = True
            name = f"{cls}.{meth}" if cls else meth
            if name in _UNSTABLE_TESTS:
                continue
            body = match.group(3) or ""
            if "<failure" in body or "<error" in body:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif "<skipped" in body:
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
            else:
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)

        if saw_xml:
            return TestResult(
                passed_count=len(passed_tests),
                failed_count=len(failed_tests),
                skipped_count=len(skipped_tests),
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )

        re_class_result = re.compile(
            r"Running\s+(\S+)\s*\n"
            r"(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*"
            r".*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),"
            r"\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
        )

        for match in re_class_result.finditer(clean_log):
            name = match.group(1)
            if name in _UNSTABLE_TESTS:
                continue
            total = int(match.group(2))
            failures = int(match.group(3))
            errors = int(match.group(4))
            skipped = int(match.group(5))
            if failures > 0 or errors > 0:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif total > 0 and skipped == total:
                if name not in failed_tests:
                    skipped_tests.add(name)
            elif total > 0:
                if name not in failed_tests:
                    passed_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("oshi", f"oshi_{_ERA_MIN}_to_{_ERA_MAX}")(OshiInstance)
Instance.register("oshi", "oshi")(OshiInstance)
