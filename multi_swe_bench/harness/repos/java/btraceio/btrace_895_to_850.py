from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "btraceio"
_REPO = "btrace"
_INTERVAL = "895_to_850"

_GRADLE_DIST_URL = "https://services.gradle.org/distributions/gradle-9.5.1-bin.zip"
_GRADLE_DIST_DIR = "gradle-9.5.1-bin"

_JDK_SPECS = "8:jdk8u512-b09 11:jdk-11.0.33%2B10 21:jdk-21.0.13%2B9 24:jdk-24.0.2%2B12"
_JDK_HOMES = (
    "/usr/lib/jvm/temurin-8",
    "/usr/lib/jvm/temurin-11",
    "/usr/lib/jvm/temurin-21",
    "/usr/lib/jvm/temurin-24",
)

_GRADLE_TEST_CMD = (
    "./gradlew --no-daemon --console=plain test --continue -x :integration-tests:test"
)
_GRADLE_WARMUP_CMD = (
    "./gradlew --no-daemon --console=plain --continue testClasses "
    ":btrace-agent:compileTestProbes "
    "-x :integration-tests:compileTestJava -x :integration-tests:testClasses"
)
_XML_DUMP = (
    r"find . -path '*/build/test-results/test/TEST-*.xml' -print -exec cat {} \;"
)

_PATCH_APPLY_OPTS = (
    "--whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' "
    "--exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' "
    "--exclude='*.jar'"
)

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM ubuntu:22.04

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

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
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
    ca-certificates curl git unzip zip procps \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \\
      amd64) JARCH=x64 ;; \\
      arm64) JARCH=aarch64 ;; \\
      *) JARCH=x64 ;; \\
    esac; \\
    mkdir -p /usr/lib/jvm; \\
    for spec in @@JDK_SPECS@@; do \\
      jver="${spec%%:*}"; \\
      jrel="${spec#*:}"; \\
      mkdir -p "/usr/lib/jvm/temurin-${jver}"; \\
      curl -fsSL --retry 5 --retry-delay 5 -o /tmp/jdk.tar.gz \\
        "https://api.adoptium.net/v3/binary/version/${jrel}/linux/${JARCH}/jdk/hotspot/normal/eclipse"; \\
      tar -xzf /tmp/jdk.tar.gz -C "/usr/lib/jvm/temurin-${jver}" --strip-components=1; \\
      rm -f /tmp/jdk.tar.gz; \\
      "/usr/lib/jvm/temurin-${jver}/bin/javac" -version; \\
    done

ENV JAVA_HOME=/usr/lib/jvm/temurin-21
ENV PATH=${JAVA_HOME}/bin:${PATH}
ENV GRADLE_USER_HOME=/root/.gradle

RUN mkdir -p /root/.gradle && printf '%s\\n' \\
    'org.gradle.java.installations.paths=@@JDK_PATHS@@' \\
    'org.gradle.java.installations.auto-detect=true' \\
    'org.gradle.java.installations.auto-download=false' \\
    'org.gradle.jvmargs=-Xmx6g -XX:MaxMetaspaceSize=1g' \\
    'org.gradle.daemon=false' \\
    'org.gradle.parallel=false' \\
    'org.gradle.console=plain' \\
    > /root/.gradle/gradle.properties && \\
    grep -q installations.paths /root/.gradle/gradle.properties

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/@@REPO@@

RUN set -eux; \\
    mkdir -p /tmp/gradle-dist/gradle/wrapper; \\
    cp /home/@@REPO@@/gradlew /tmp/gradle-dist/gradlew; \\
    cp /home/@@REPO@@/gradle/wrapper/gradle-wrapper.jar /tmp/gradle-dist/gradle/wrapper/gradle-wrapper.jar; \\
    printf '%s\\n' \\
      'distributionBase=GRADLE_USER_HOME' \\
      'distributionPath=wrapper/dists' \\
      'distributionUrl=@@GRADLE_PROPS_URL@@' \\
      'networkTimeout=10000' \\
      'validateDistributionUrl=true' \\
      'zipStoreBase=GRADLE_USER_HOME' \\
      'zipStorePath=wrapper/dists' \\
      > /tmp/gradle-dist/gradle/wrapper/gradle-wrapper.properties; \\
    chmod +x /tmp/gradle-dist/gradlew; \\
    cd /tmp/gradle-dist; \\
    ./gradlew --no-daemon --console=plain --version; \\
    cd /; \\
    rm -rf /tmp/gradle-dist; \\
    test -d /root/.gradle/wrapper/dists/@@GRADLE_DIST_DIR@@

WORKDIR /home/@@REPO@@

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = """FROM @@IMAGE_NAME@@:@@BASE_TAG@@

ARG BASE_COMMIT="@@SHA@@"
@@GLOBAL_ENV@@
@@COPY_COMMANDS@@

RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
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
@@CLEAR_ENV@@
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  printf '%s\\n' "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  printf '%s\\n' "check_git_changes: Uncommitted changes"
  exit 1
fi

printf '%s\\n' "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE = """#!/bin/bash
set -eo pipefail

cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

if ! git cat-file -e @@SHA@@^{commit} 2>/dev/null; then
    git fetch --no-tags --depth 1 "https://github.com/@@ORG@@/@@REPO@@.git" @@SHA@@
fi
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "@@SHA@@"

chmod +x gradlew

for attempt in 1 2 3; do
    if @@WARMUP@@; then
        break
    fi
    sleep 20
done

@@JDK8@@/bin/javac -version
@@JDK11@@/bin/javac -version
@@JDK21@@/bin/javac -version
@@JDK24@@/bin/javac -version
"$JAVA_HOME/bin/java" -version
./gradlew --no-daemon --console=plain --version > /dev/null
test "$(git rev-parse HEAD)" = "@@SHA@@"
test -n "$(find /root/.gradle/caches/modules-2 -name '*.jar' -print -quit)"
test -d /root/.gradle/wrapper/dists/@@GRADLE_DIST_DIR@@
test -d btrace-core/build/classes/java/test
test -d btrace-agent/build/classes/java/test
test -d btrace-agent/build/classes/traces
test -d btrace-runtime/build/classes/java/test
test -d btrace-client/build/classes/java/test
"""

_RUN = """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/@@REPO@@

set +e
@@TEST_CMD@@
GRADLE_STATUS=$?
set -e

@@XML_DUMP@@

exit $GRADLE_STATUS
"""

_TEST_RUN = """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/@@REPO@@

git apply @@PATCH_OPTS@@ /home/test.patch

set +e
@@TEST_CMD@@
GRADLE_STATUS=$?
set -e

@@XML_DUMP@@

exit $GRADLE_STATUS
"""

_FIX_RUN = """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/@@REPO@@

git apply @@PATCH_OPTS@@ /home/test.patch /home/fix.patch

set +e
@@TEST_CMD@@
GRADLE_STATUS=$?
set -e

@@XML_DUMP@@

exit $GRADLE_STATUS
"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("@@ORG@@", pr.org)
        .replace("@@REPO@@", pr.repo)
        .replace("@@SHA@@", pr.base.sha)
        .replace("@@JDK_SPECS@@", _JDK_SPECS)
        .replace("@@JDK_PATHS@@", ",".join(_JDK_HOMES))
        .replace("@@JDK8@@", _JDK_HOMES[0])
        .replace("@@JDK11@@", _JDK_HOMES[1])
        .replace("@@JDK21@@", _JDK_HOMES[2])
        .replace("@@JDK24@@", _JDK_HOMES[3])
        .replace("@@GRADLE_PROPS_URL@@", _GRADLE_DIST_URL.replace("://", "\\://", 1))
        .replace("@@GRADLE_DIST_DIR@@", _GRADLE_DIST_DIR)
        .replace("@@WARMUP@@", _GRADLE_WARMUP_CMD)
        .replace("@@TEST_CMD@@", _GRADLE_TEST_CMD)
        .replace("@@XML_DUMP@@", _XML_DUMP)
        .replace("@@PATCH_OPTS@@", _PATCH_APPLY_OPTS)
    )


class Btrace895To850ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return _render(_BASE_DOCKERFILE, self.pr)


class Btrace895To850ImageDefault(Image):
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
        return Btrace895To850ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", _render(_RUN, self.pr)),
            File(".", "test-run.sh", _render(_TEST_RUN, self.pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN, self.pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        return (
            _render(_PR_DOCKERFILE, self.pr)
            .replace("@@IMAGE_NAME@@", image.image_name())
            .replace("@@BASE_TAG@@", image.image_tag())
            .replace("@@GLOBAL_ENV@@", global_env)
            .replace("@@COPY_COMMANDS@@", copy_commands)
            .replace("@@CLEAR_ENV@@", clear_env)
        )


@Instance.register(_ORG, f"{_REPO}_{_INTERVAL}")
class BTRACE_895_TO_850(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Btrace895To850ImageDefault(self.pr, self._config)

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
        ansi_re = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        test_log = ansi_re.sub("", test_log)

        attr_re = re.compile(r'(\w+)="([^"]*)"')
        index_re = re.compile(r"^\[\d+\]\s*")
        module_re = re.compile(
            r"(?m)^\./(?P<module>[^\n]+?)/build/test-results/test/TEST-[^\n]*\.xml$"
        )

        markers = [(m.start(), m.group("module")) for m in module_re.finditer(test_log)]

        def module_for(offset: int) -> str:
            found = ""
            for start, module in markers:
                if start > offset:
                    break
                found = module
            return found

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        def record(name: str, status: str) -> None:
            if not name:
                return
            if status == "FAIL":
                failed_tests.add(name)
            elif status == "SKIP":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        pos = 0
        saw_xml = False
        while True:
            start = test_log.find("<testcase", pos)
            if start < 0:
                break
            cursor = start + len("<testcase")
            in_quote = False
            while cursor < len(test_log):
                ch = test_log[cursor]
                if ch == '"':
                    in_quote = not in_quote
                elif ch == ">" and not in_quote:
                    break
                cursor += 1
            if cursor >= len(test_log):
                break
            head = test_log[start + len("<testcase") : cursor]
            self_closed = head.rstrip().endswith("/")
            attrs = dict(attr_re.findall(head))
            if self_closed:
                body = ""
                pos = cursor + 1
            else:
                end = test_log.find("</testcase>", cursor)
                if end < 0:
                    body = test_log[cursor + 1 :]
                    pos = len(test_log)
                else:
                    body = test_log[cursor + 1 : end]
                    pos = end + len("</testcase>")

            method = index_re.sub("", attrs.get("name", "")).strip()
            if not method:
                continue
            saw_xml = True
            classname = attrs.get("classname", "").strip()
            name = f"{classname}#{method}" if classname else method
            module = module_for(start)
            if module:
                name = f"{module}:{name}"
            if "<failure" in body or "<error" in body:
                record(name, "FAIL")
            elif "<skipped" in body:
                record(name, "SKIP")
            else:
                record(name, "PASS")

        if not saw_xml:
            console_re = re.compile(r"^(\S.*?\s>\s.+?)\s(PASSED|FAILED|SKIPPED)$")
            task_re = re.compile(r"^> Task :(\S+) (FAILED|SKIPPED|NO-SOURCE)$")
            compile_re = re.compile(
                r"(compile\w*|processResources|processTestResources"
                r"|classes|testClasses|jar)$"
            )
            for line in test_log.splitlines():
                match = console_re.match(line.strip())
                if match:
                    label = index_re.sub("", match.group(1).strip())
                    if match.group(2) == "PASSED":
                        record(label, "PASS")
                    elif match.group(2) == "FAILED":
                        record(label, "FAIL")
                    else:
                        record(label, "SKIP")
                    continue
                match = task_re.match(line.strip())
                if match:
                    task = match.group(1)
                    if compile_re.search(task):
                        continue
                    if match.group(2) == "FAILED":
                        record(task, "FAIL")
                    else:
                        record(task, "SKIP")

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
