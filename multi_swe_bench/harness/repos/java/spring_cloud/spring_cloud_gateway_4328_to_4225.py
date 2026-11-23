import re
import shlex

from multi_swe_bench.harness import pull_request as _pull_request
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "spring-cloud"
_REPO = "spring-cloud-gateway"
_INTERVAL = "spring_cloud_gateway_4328_to_4225"
_BASE_REFS = ("main",)

_BOUNDS = re.search(r"_(\d+)_to_(\d+)$", _INTERVAL)
_BASE_TAG = "base-%s_to_%s" % (_BOUNDS.group(1), _BOUNDS.group(2))
_PR_MIN = min(int(_BOUNDS.group(1)), int(_BOUNDS.group(2)))
_PR_MAX = max(int(_BOUNDS.group(1)), int(_BOUNDS.group(2)))

_JDK_IMAGE = "eclipse-temurin:17-jdk"
_MAVEN_VERSION = "3.9.11"
_MAVEN_SHA512 = (
    "bcfe4fe305c962ace56ac7b5fc7a08b87d5abd8b7e89027ab251069faebee516"
    "b0ded8961445d6d91ec1985dfe30f8153268843c89aa392733d1a3ec956c9978"
)
_BASE_APT = "git ca-certificates curl"

_MVN_FLAGS = (
    "-B -ntp "
    "-P spring,withoutDockerTests "
    "-Ddisable.checks=true "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-Dmaven.javadoc.skip=true "
    "-Djacoco.skip=true"
)

_MVN_TIMEOUT = "timeout -k 60 2700"
_MVN_TIMEOUT_PREPARE = "timeout -k 60 3600"

_ROUTING_FLAG = "_spring_cloud_gateway_4328_to_4225_routed"

if not getattr(_pull_request.PullRequest, _ROUTING_FLAG, False):
    _orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _routed_from_json(cls, json_str):
        pr = _orig_from_json(cls, json_str)
        if (
            pr.org == _ORG
            and pr.repo == _REPO
            and not pr.number_interval
            and pr.base.ref in _BASE_REFS
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return pr

    _pull_request.PullRequest.from_json = classmethod(_routed_from_json)
    setattr(_pull_request.PullRequest, _ROUTING_FLAG, True)

_CHECK_GIT_CHANGES = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  git status --porcelain
  exit 1
fi

exit 0
"""

_TARGETS = r"""CLASSES=@@CLASSES@@
MODULES=@@MODULES@@
SEL=""
for _m in $MODULES; do
    if [ -f "$_m/pom.xml" ]; then SEL="$SEL,$_m"; fi
done
SEL="${SEL#,}"

if [ -z "$SEL" ] || [ -z "$CLASSES" ]; then
    exit 1
fi
"""

_RUN_TARGETS = r"""find . -type d -name surefire-reports -prune -exec rm -rf {} +

for _attempt in 1 2 3 4 5; do
    if _out="$(@@MVN_TIMEOUT@@ mvn @@MVN_FLAGS@@ package -DskipTests -pl "$SEL" -am 2>&1)"; then
        break
    fi
    _bad="$(printf '%s\n' "$_out" \
        | sed -nE 's#^\[ERROR\] (@@REPO_DIR@@/[^:]*/src/test/java/[^:]*\.java):\[[0-9]+,[0-9]+\].*#\1#p' \
        | sort -u)"
    if [ -z "$_bad" ]; then
        printf 'MSB_TEST_COMPILE_GAVE_UP: attempt %s, no test source to drop\n' "$_attempt"
        printf '%s\n' "$_out"
        break
    fi
    printf '%s\n' "$_bad" \
        | sed "s#^#MSB_DROPPED_TEST_SOURCE: attempt $_attempt: #"
    printf '%s\n' "$_bad" | xargs -r rm -f
done

MVN_RC=0
@@MVN_TIMEOUT@@ mvn @@MVN_FLAGS@@ package -pl "$SEL" -am -Dtest="$CLASSES" || MVN_RC=$?

find . -path '*/surefire-reports/TEST-*.xml' -exec cat {} +

exit "$MVN_RC"
"""

_PREPARE = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd @@REPO_DIR@@

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "@@SHA@@"
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "@@SHA@@"

@@TARGETS@@

@@MVN_TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ package -DskipTests -pl "$SEL" -am || true
@@MVN_TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ package -pl "$SEL" -am -Dtest="$CLASSES" || true

git clean -fdx

@@MVN_TIMEOUT_PREPARE@@ mvn -o @@MVN_FLAGS@@ test-compile -pl "$SEL" -am

git clean -fdx
test "$(git rev-parse HEAD)" = "@@SHA@@"
"""

_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd @@REPO_DIR@@

@@TARGETS@@

@@RUN_TARGETS@@
"""

_TEST_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd @@REPO_DIR@@
git apply --whitespace=nowarn /home/test.patch

@@TARGETS@@

@@RUN_TARGETS@@
"""

_FIX_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd @@REPO_DIR@@
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

@@TARGETS@@

@@RUN_TARGETS@@
"""

_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM @@IMAGE@@

ARG TARGETARCH
ARG REPO_URL="https://github.com/@@ORG@@/@@REPO@@.git"
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

RUN apt-get update && apt-get install -y --no-install-recommends \
    @@APT@@ \
    && rm -rf /var/lib/apt/lists/*

ENV MAVEN_HOME=/opt/apache-maven-@@MAVEN_VERSION@@ \
    MAVEN_OPTS="-Xmx3g"

RUN set -eux; \
    curl -fsSL -o /tmp/maven.tar.gz \
        "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/@@MAVEN_VERSION@@/apache-maven-@@MAVEN_VERSION@@-bin.tar.gz"; \
    echo "@@MAVEN_SHA512@@  /tmp/maven.tar.gz" | sha512sum -c -; \
    tar -xzf /tmp/maven.tar.gz -C /opt; \
    ln -s /opt/apache-maven-@@MAVEN_VERSION@@/bin/mvn /usr/local/bin/mvn; \
    rm -f /tmp/maven.tar.gz; \
    java -version; \
    mvn -v

RUN git config --global --add safe.directory '*'

WORKDIR /home/

@@CODE@@

WORKDIR @@REPO_DIR@@
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


class SpringCloudGateway4328To4225ImageBase(Image):
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
        return _JDK_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        if self.config.need_clone:
            code = 'RUN git clone "${REPO_URL}" /home/%s' % repo
        else:
            code = "COPY %s /home/%s" % (repo, repo)

        return _render(
            _BASE_DOCKERFILE,
            {
                "IMAGE": self.dependency(),
                "ORG": self.pr.org,
                "REPO": repo,
                "REPO_DIR": "/home/%s" % repo,
                "APT": _BASE_APT,
                "MAVEN_VERSION": _MAVEN_VERSION,
                "MAVEN_SHA512": _MAVEN_SHA512,
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class SpringCloudGateway4328To4225ImageDefault(Image):
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
        return SpringCloudGateway4328To4225ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-%d" % self.pr.number

    def workdir(self) -> str:
        return "pr-%d" % self.pr.number

    def _targets(self) -> str:
        paths = re.findall(r"^diff --git a/\S+ b/(\S+)$", self.pr.test_patch, re.M)
        classes = sorted(
            {
                p.rsplit("/", 1)[-1][: -len(".java")]
                for p in paths
                if re.search(r"/src/test/java/.*(Test|Tests|TestCase|IT)\.java$", p)
            }
        )
        modules = sorted(
            {
                m.group(1)
                for p in paths
                for m in [re.match(r"^(.*)/src/test/(java|resources)/", p)]
                if m
            }
        )
        return _render(
            _TARGETS,
            {
                "CLASSES": shlex.quote(",".join(classes)),
                "MODULES": shlex.quote(" ".join(modules)),
            },
        )

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "MVN_FLAGS": _MVN_FLAGS,
            "MVN_TIMEOUT_PREPARE": _MVN_TIMEOUT_PREPARE,
            "MVN_TIMEOUT": _MVN_TIMEOUT,
        }

    def _script(self, body: str) -> str:
        body = _render(
            body,
            {
                "TARGETS": self._targets(),
                "RUN_TARGETS": _RUN_TARGETS,
            },
        )
        return _render(body, self._values())

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._script(_PREPARE)),
            File(".", "run.sh", self._script(_RUN)),
            File(".", "test-run.sh", self._script(_TEST_RUN)),
            File(".", "fix-run.sh", self._script(_FIX_RUN)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "\n".join("COPY %s /home/" % f.name for f in self.files())
        values = self._values()
        values.update(
            {
                "BASE_IMAGE": "%s:%s" % (base.image_name(), base.image_tag()),
                "COPY": copy_commands,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            }
        )
        return _render(_PR_DOCKERFILE, values)


def parse_surefire_xml(test_log: str) -> TestResult:
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    clean = re.sub(r"<!\[CDATA\[.*?\]\]>", "", clean, flags=re.DOTALL)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    # Attribute values are tokenized as quoted strings so a raw `>` inside a
    # name/classname (JUnit 5 display names) cannot truncate the attribute run
    # and silently drop the testcase.
    testcase_re = re.compile(
        r"<testcase\b((?:\"[^\"]*\"|'[^']*'|[^>\"'])*?)(/>|>(.*?)</testcase>)",
        re.DOTALL,
    )
    name_re = re.compile(r"\bname=(\"[^\"]*\"|'[^']*')")
    classname_re = re.compile(r"\bclassname=(\"[^\"]*\"|'[^']*')")

    for m in testcase_re.finditer(clean):
        attrs = m.group(1)
        nm = name_re.search(attrs)
        cn = classname_re.search(attrs)
        if not nm or not cn:
            continue
        test_id = "%s#%s" % (cn.group(1)[1:-1], nm.group(1)[1:-1])
        inner = m.group(3) or ""
        if m.group(2) == "/>":
            passed.add(test_id)
        elif "<failure" in inner or "<error" in inner:
            failed.add(test_id)
        elif "<skipped" in inner:
            skipped.add(test_id)
        else:
            passed.add(test_id)

    passed -= failed
    skipped -= failed
    passed -= skipped

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register(_ORG, _INTERVAL)
class SPRING_CLOUD_GATEWAY_4328_TO_4225(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SpringCloudGateway4328To4225ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_surefire_xml(test_log)


Instance.register(_ORG, _REPO)(SPRING_CLOUD_GATEWAY_4328_TO_4225)
