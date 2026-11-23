from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "pinpoint-apm"
_REPO = "pinpoint"
_INTERVAL = "13944_to_14389"

_MAVEN_VERSION = "3.9.16"
_MAVEN_HOME = f"/opt/apache-maven-{_MAVEN_VERSION}"
_MAVEN_SHA512 = (
    "831a8591fe20c8243b1dbe7d71e3244f31d1665b0804b2e825e38cbbe5ce0caf"
    "b8338851f90780735568773e0a6cd07bbec107cda0b896b008b861075358b6f6"
)

_JDK_PACKAGES = (
    "openjdk-17-jdk-headless",
    "openjdk-11-jdk-headless",
    "openjdk-8-jdk-headless",
)
_JDK_VERSIONS = ("8", "11", "17")

_MVN_FLAGS = (
    "-Dbuild.frontend.skip=true "
    "-Dcheckstyle.skip=true "
    "-Dspotbugs.skip=true "
    "-Dpmd.skip=true "
    "-Dcpd.skip=true "
    "-Djacoco.skip=true "
    "-Dplugin.jacoco.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.source.skip=true "
    "-Dlicense.skip=true "
    "-Drat.skip=true "
    "-Dgpg.skip=true "
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120 "
    "-Dmaven.wagon.rto=180000"
)

_MVN_TEST_FLAGS = (
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-Dsurefire.useFile=false"
)

_XML_DUMP = (
    r"find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} \;"
)

_PATCH_APPLY_OPTS = (
    "--whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' "
    "--exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' "
    "--exclude='*.jar'"
)

_JAVA_ENV = """export JAVA_8_HOME=/usr/lib/jvm/jdk-8
export JAVA_11_HOME=/usr/lib/jvm/jdk-11
export JAVA_17_HOME=/usr/lib/jvm/jdk-17
export JAVA_HOME="${JAVA_17_HOME}"
export PATH="${JAVA_HOME}/bin:@@MAVEN_HOME@@/bin:${PATH}"
export MAVEN_OPTS="-Xmx4g -XX:MaxMetaspaceSize=1g"
export CI=true
export TZ=UTC"""

_DERIVE_TARGETS = """derive_targets() {
    PATCH_PATHS="$( { grep -h '^diff --git ' /home/test.patch /home/fix.patch 2>/dev/null || true; } \\
        | sed -e 's|^diff --git ||' | tr ' ' '\\n' | sed -e 's|^[ab]/||' \\
        | grep -v '^/dev/null$' | sort -u )"

    MODULES=""
    for patch_path in $PATCH_PATHS; do
        module_dir="$(dirname "$patch_path")"
        while [ "$module_dir" != "." ] && [ "$module_dir" != "/" ] && [ ! -f "$module_dir/pom.xml" ]; do
            module_dir="$(dirname "$module_dir")"
        done
        [ "$module_dir" != "." ] || continue
        [ "$module_dir" != "/" ] || continue
        [ -f "$module_dir/pom.xml" ] || continue
        case " $MODULES " in
            *" $module_dir "*) ;;
            *) MODULES="${MODULES:+$MODULES }$module_dir" ;;
        esac
    done

    SEL_LIST=""
    SEL=""
    for module_dir in $MODULES; do
        parent="."
        in_reactor=1
        for part in $(printf '%s' "$module_dir" | tr '/' ' '); do
            if ! grep -q "<module>${part}</module>" "${parent}/pom.xml" 2>/dev/null; then
                in_reactor=0
                break
            fi
            parent="${parent}/${part}"
        done
        [ "$in_reactor" -eq 1 ] || continue
        SEL_LIST="${SEL_LIST:+$SEL_LIST }$module_dir"
        SEL="${SEL:+${SEL},}${module_dir}"
    done

    if [ -n "$SEL" ]; then
        PL_ARGS="-pl $SEL"
    else
        PL_ARGS=""
    fi
}

reset_targets() {
    for module_dir in $MODULES; do
        rm -rf "$module_dir/target"
    done
    find . -path '*/target/surefire-reports' -prune -exec rm -rf {} + 2>/dev/null || true
}

build_main() {
    mvn -B -fn install $PL_ARGS -am -Dmaven.test.skip=true @@MVN_FLAGS@@
}

prune_broken_tests() {
    module_dir="$1"
    TC_LOG=/tmp/test-compile.log
    attempt=0
    while [ "$attempt" -lt 5 ]; do
        attempt=$((attempt + 1))
        if mvn -B test-compile -pl "$module_dir" @@MVN_FLAGS@@ > "$TC_LOG" 2>&1; then
            printf 'prune_broken_tests: %s test-compiles (attempt %s)\\n' "$module_dir" "$attempt"
            return 0
        fi
        broken="$( { grep -oE '^\\[ERROR\\] /[^ :]*/src/test/[^ :]*\\.java' "$TC_LOG" || true; } \\
            | sed -e 's|^\\[ERROR\\] ||' | sort -u )"
        if [ -z "$broken" ]; then
            printf 'prune_broken_tests: %s fails to test-compile outside src/test\\n' "$module_dir"
            tail -n 100 "$TC_LOG"
            return 1
        fi
        for broken_src in $broken; do
            printf 'prune_broken_tests: dropping non-compiling test source %s\\n' "$broken_src"
            rm -f "$broken_src"
        done
    done
    printf 'prune_broken_tests: %s still fails after %s attempts\\n' "$module_dir" "$attempt"
    tail -n 100 "$TC_LOG"
    return 1
}

prune_tests() {
    for module_dir in $SEL_LIST; do
        prune_broken_tests "$module_dir" \\
            || printf 'prune_tests: surefire may emit nothing for %s\\n' "$module_dir"
    done
}

run_tests() {
    TEST_STATUS=0
    for module_dir in $SEL_LIST; do
        mvn -B -fn test -pl "$module_dir" @@MVN_FLAGS@@ @@MVN_TEST_FLAGS@@ || TEST_STATUS=1
    done
    return $TEST_STATUS
}"""

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
    ca-certificates curl git unzip zip procps tzdata locales \\
    && apt-get install -y --no-install-recommends ca-certificates-java \\
    && apt-get install -y --no-install-recommends @@JDK_PKG_17@@ \\
    && apt-get install -y --no-install-recommends @@JDK_PKG_11@@ \\
    && apt-get install -y --no-install-recommends @@JDK_PKG_8@@ \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    for jver in @@JDK_VERSIONS@@; do \\
      jdir="$(ls -d /usr/lib/jvm/java-${jver}-openjdk-* | head -1)"; \\
      test -d "$jdir"; \\
      ln -sfn "$jdir" "/usr/lib/jvm/jdk-${jver}"; \\
      "/usr/lib/jvm/jdk-${jver}/bin/javac" -version; \\
    done

ENV JAVA_8_HOME=/usr/lib/jvm/jdk-8 \\
    JAVA_11_HOME=/usr/lib/jvm/jdk-11 \\
    JAVA_17_HOME=/usr/lib/jvm/jdk-17 \\
    JAVA_HOME=/usr/lib/jvm/jdk-17 \\
    MAVEN_HOME=@@MAVEN_HOME@@
ENV PATH=${JAVA_HOME}/bin:${MAVEN_HOME}/bin:${PATH}

RUN set -eux; \\
    curl -fsSL --retry 5 --retry-delay 5 -o /tmp/maven.tar.gz \\
      "https://archive.apache.org/dist/maven/maven-3/@@MAVEN_VERSION@@/binaries/apache-maven-@@MAVEN_VERSION@@-bin.tar.gz"; \\
    printf '%s  %s\\n' "@@MAVEN_SHA512@@" /tmp/maven.tar.gz | sha512sum -c -; \\
    tar -xzf /tmp/maven.tar.gz -C /opt; \\
    rm -f /tmp/maven.tar.gz; \\
    mvn -v

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/@@REPO@@ && \\
    cd /home/@@REPO@@ && git rev-parse HEAD >/dev/null

WORKDIR /home/@@REPO@@

CMD ["/bin/bash"]
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

@@JAVA_ENV@@

@@DERIVE_TARGETS@@

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

derive_targets
test -n "$SEL"

for attempt in 1 2 3; do
    build_main || true
    if [ -n "$(find . -path '*/target/classes' -type d -print -quit)" ]; then
        break
    fi
    sleep 20
done

run_tests || true

test -n "$(find . -path '*/target/classes' -type d -print -quit)"
test -n "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)"

derive_targets
test -n "$SEL"
test "$(git rev-parse HEAD)" = "@@SHA@@"
"$JAVA_8_HOME/bin/javac" -version
"$JAVA_11_HOME/bin/javac" -version
"$JAVA_17_HOME/bin/javac" -version
mvn -v > /dev/null
test -n "$(find /root/.m2/repository -name '*.jar' -print -quit)"
test -d /root/.m2/repository/com/navercorp/pinpoint
test -n "$(find /root/.m2/repository/org/apache/maven/plugins/maven-surefire-plugin -name '*.jar' -print -quit)"
"""

_RUN = """#!/bin/bash
set -eo pipefail

@@JAVA_ENV@@

@@DERIVE_TARGETS@@

cd /home/@@REPO@@

derive_targets
reset_targets

set +e
build_main
prune_tests
run_tests
MVN_STATUS=$?
set -e

@@XML_DUMP@@

exit $MVN_STATUS
"""

_TEST_RUN = """#!/bin/bash
set -eo pipefail

@@JAVA_ENV@@

@@DERIVE_TARGETS@@

cd /home/@@REPO@@

git apply @@PATCH_OPTS@@ /home/test.patch

derive_targets
reset_targets

set +e
build_main
prune_tests
run_tests
MVN_STATUS=$?
set -e

@@XML_DUMP@@

exit $MVN_STATUS
"""

_FIX_RUN = """#!/bin/bash
set -eo pipefail

@@JAVA_ENV@@

@@DERIVE_TARGETS@@

cd /home/@@REPO@@

git apply @@PATCH_OPTS@@ /home/test.patch /home/fix.patch

derive_targets
reset_targets

set +e
build_main
prune_tests
run_tests
MVN_STATUS=$?
set -e

@@XML_DUMP@@

exit $MVN_STATUS
"""


def _render(template: str, pr: PullRequest) -> str:
    java_env = _JAVA_ENV.replace("@@MAVEN_HOME@@", _MAVEN_HOME)
    return (
        template.replace("@@JAVA_ENV@@", java_env)
        .replace("@@DERIVE_TARGETS@@", _DERIVE_TARGETS)
        .replace("@@MVN_FLAGS@@", _MVN_FLAGS)
        .replace("@@MVN_TEST_FLAGS@@", _MVN_TEST_FLAGS)
        .replace("@@XML_DUMP@@", _XML_DUMP)
        .replace("@@PATCH_OPTS@@", _PATCH_APPLY_OPTS)
        .replace("@@JDK_PKG_17@@", _JDK_PACKAGES[0])
        .replace("@@JDK_PKG_11@@", _JDK_PACKAGES[1])
        .replace("@@JDK_PKG_8@@", _JDK_PACKAGES[2])
        .replace("@@JDK_VERSIONS@@", " ".join(_JDK_VERSIONS))
        .replace("@@MAVEN_VERSION@@", _MAVEN_VERSION)
        .replace("@@MAVEN_SHA512@@", _MAVEN_SHA512)
        .replace("@@MAVEN_HOME@@", _MAVEN_HOME)
        .replace("@@SHA@@", pr.base.sha)
        .replace("@@ORG@@", pr.org)
        .replace("@@REPO@@", pr.repo)
    )


class Pinpoint13944To14389ImageBase(Image):
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


class Pinpoint13944To14389ImageDefault(Image):
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
        return Pinpoint13944To14389ImageBase(self.pr, self._config)

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
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        sections = [
            f"FROM {name}:{tag}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.extend(
            [
                copy_commands,
                f"WORKDIR /home/{self.pr.repo}",
                "RUN bash /home/prepare.sh",
                Image._HARDENING_BLOCK.rstrip("\n"),
            ]
        )

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register(_ORG, f"{_REPO}_{_INTERVAL}")
class PINPOINT_13944_TO_14389(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Pinpoint13944To14389ImageDefault(self.pr, self._config)

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
        ansi_re = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
        test_log = ansi_re.sub("", test_log)

        attr_re = re.compile(r'(\w+)="([^"]*)"')
        index_re = re.compile(r"^\[\d+\]\s*")

        statuses: dict[str, str] = {}
        rank = {"PASS": 0, "SKIP": 1, "FAIL": 2}

        def record(name: str, status: str) -> None:
            if not name:
                return
            current = statuses.get(name)
            if current is None or rank[status] > rank[current]:
                statuses[name] = status

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
            if "<failure" in body or "<error" in body:
                record(name, "FAIL")
            elif "<skipped" in body:
                record(name, "SKIP")
            else:
                record(name, "PASS")

        if not saw_xml:
            running_re = re.compile(r"Running\s+([\w.$]+)\s*$")
            summary_class_re = re.compile(
                r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
                r"\s*Skipped:\s*(\d+),\s*Time elapsed:.*?(?:--|-)\s+in\s+([\w.$]+)"
            )
            summary_plain_re = re.compile(
                r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
                r"\s*Skipped:\s*(\d+),\s*Time elapsed:"
            )

            def record_summary(
                name: str, total: int, failures: int, errors: int, skipped: int
            ) -> None:
                if failures > 0 or errors > 0:
                    record(name, "FAIL")
                elif total > 0 and skipped == total:
                    record(name, "SKIP")
                elif total > 0:
                    record(name, "PASS")

            current_class = None
            for line in test_log.splitlines():
                match = summary_class_re.search(line)
                if match:
                    current_class = None
                    record_summary(
                        match.group(5),
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        int(match.group(4)),
                    )
                    continue
                match = summary_plain_re.search(line)
                if match:
                    if current_class:
                        record_summary(
                            current_class,
                            int(match.group(1)),
                            int(match.group(2)),
                            int(match.group(3)),
                            int(match.group(4)),
                        )
                        current_class = None
                    continue
                match = running_re.search(line)
                if match:
                    current_class = match.group(1)

        passed_tests = {n for n, s in statuses.items() if s == "PASS"}
        failed_tests = {n for n, s in statuses.items() if s == "FAIL"}
        skipped_tests = {n for n, s in statuses.items() if s == "SKIP"}

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
