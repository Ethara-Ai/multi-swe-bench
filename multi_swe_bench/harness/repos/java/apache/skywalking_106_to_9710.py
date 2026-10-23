import re
import textwrap

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_MVN_SKIPS = (
    "-Dcheckstyle.skip=true "
    "-Drat.skip=true "
    "-Dlicense.skip=true "
    "-Denforcer.skip=true "
    "-Danimal.sniffer.skip=true "
    "-Dmdep.analyze.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dgpg.skip=true "
    "-Dfbs.compiler=/usr/local/bin/flatc"
)

_MVN_NET = (
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120 "
    "-Dmaven.wagon.rto=120000"
)

_MVN_TEST = (
    "clean test -B -fn "
    f"{_MVN_SKIPS} {_MVN_NET} "
    "-Dsurefire.useFile=false -Dsurefire.skipAfterFailureCount=0 "
    "-DfailIfNoTests=false"
)

_MVN_WARMUP = f"clean test-compile -T 4 -B -fn -DskipTests {_MVN_SKIPS} {_MVN_NET}"

_MVN_WARMUP_TIMEOUT = "2700"

_JDK_PACKAGES = ("openjdk-8-jdk",)

_JAVA_ENV_TEMPLATE = """select_java_home() {
    local pom="/home/@@REPO@@/pom.xml"
    local want=""

    if [ -f "$pom" ]; then
        want="$(grep -A5 '<requireJavaVersion>' "$pom" \\
            | sed -n 's:.*<version>\\([^<]*\\)</version>.*:\\1:p' | head -1)"
        [ -n "$want" ] || want="$(sed -n \\
            's:.*<maven\\.compiler\\.source>\\([^<]*\\)<.*:\\1:p' "$pom" | head -1)"
        [ -n "$want" ] || want="$(sed -n \\
            's:.*<compiler\\.version>\\([^<]*\\)<.*:\\1:p' "$pom" | head -1)"
    fi

    want="${want#1.}"
    case "$want" in
        8) ;;
        *) want=8 ;;
    esac

    for candidate in /usr/lib/jvm/java-"$want"-openjdk-*; do
        if [ -d "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done

    dirname "$(dirname "$(readlink -f "$(command -v javac)")")"
}

JAVA_HOME="$(select_java_home)"
export JAVA_HOME
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS='-Xmx2g -XX:+UseParallelGC -XX:MaxMetaspaceSize=512m'
echo "selected JAVA_HOME=$JAVA_HOME"
"$JAVA_HOME/bin/javac" -version 2>&1 || true"""


def _java_env(repo: str) -> str:
    return _JAVA_ENV_TEMPLATE.replace("@@REPO@@", repo)

_MAVEN_PROVISION = r"""if [ ! -x /opt/apache-maven-3.8.4/bin/mvn ]; then
    curl -fsSL -o /tmp/maven.tar.gz \
        https://archive.apache.org/dist/maven/maven-3/3.8.4/binaries/apache-maven-3.8.4-bin.tar.gz
    echo "a9b2d825eacf2e771ed5d6b0e01398589ac1bfa4171f36154d1b5787879605507802f699da6f7cfc80732a5282fd31b28e4cd6052338cbef0fa1358b48a5e3c8  /tmp/maven.tar.gz" \
        | sha512sum -c -
    tar -xzf /tmp/maven.tar.gz -C /opt
    rm -f /tmp/maven.tar.gz
    /opt/apache-maven-3.8.4/bin/mvn -v >/dev/null
fi

mkdir -p /root/.m2 && printf '%s\n' \
    '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' \
    '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' \
    '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' \
    '    <mirrors>' \
    '        <mirror>' \
    '            <id>central-gcs</id>' \
    '            <mirrorOf>central</mirrorOf>' \
    '            <name>Google mirror of Maven Central</name>' \
    '            <url>https://maven-central.storage-download.googleapis.com/maven2</url>' \
    '        </mirror>' \
    '    </mirrors>' \
    '</settings>' > /root/.m2/settings.xml
grep -q central-gcs /root/.m2/settings.xml
"""


_MVN_SETUP_TEMPLATE = r"""select_maven() {
    local props="/home/@@REPO@@/.mvn/wrapper/maven-wrapper.properties"
    local want=""

    if [ -f "$props" ]; then
        want="$(sed -n 's:.*apache-maven-\([0-9][0-9.]*\)-bin\.zip.*:\1:p' \
            "$props" | head -1)"
    fi

    case "$want" in
        3.8*|3.9*|4.*)
            if [ -x /opt/apache-maven-3.8.4/bin/mvn ]; then
                echo /opt/apache-maven-3.8.4/bin/mvn
                return 0
            fi
            ;;
    esac

    command -v mvn
}

MVN="$(select_maven)"
if [ -z "$MVN" ] || ! "$MVN" -v >/dev/null 2>&1; then
    echo "FATAL: no usable maven found" >&2
    exit 1
fi
echo "selected MVN=$MVN"
"$MVN" -v 2>&1 | head -1
"""


def _mvn_setup(repo: str) -> str:
    return _MVN_SETUP_TEMPLATE.replace("@@REPO@@", repo).rstrip("\n")


_SUBMODULE_SETUP = """if [ -f .gitmodules ]; then
  sed -i 's|git@github.com:|https://github.com/|g' .gitmodules
  git submodule update --init --recursive || true
fi"""

_SUREFIRE_DUMP = """echo "===== REACTOR SUMMARY ====="
echo "modules built ok: $(grep -cE '^\\[INFO\\] .+ SUCCESS \\[' /tmp/mvn-stage.log 2>/dev/null || echo 0)"
echo "modules failed:   $(grep -cE '^\\[INFO\\] .+ FAILURE \\[' /tmp/mvn-stage.log 2>/dev/null || echo 0)"
echo "test classes run: $(find . -path '*/target/surefire-reports/TEST-*.xml' 2>/dev/null | wc -l)"
echo "===== SUREFIRE REPORTS BEGIN ====="
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} \\; 2>/dev/null
echo "===== SUREFIRE REPORTS END =====\""""

class Skywalking106To9710ImageBase(Image):
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
        return "base-106_to_9710"

    def workdir(self) -> str:
        return "base-106_to_9710"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} \\\n'
                f"    && cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        proxy_args = (
            'ARG http_proxy=""\n'
            'ARG https_proxy=""\n'
            'ARG HTTP_PROXY=""\n'
            'ARG HTTPS_PROXY=""\n'
            'ARG no_proxy="localhost,127.0.0.1,::1"\n'
            'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
            'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
        )

        env_block = (
            "ENV DEBIAN_FRONTEND=noninteractive \\\n"
            "    LANG=C.UTF-8 \\\n"
            "    TZ=UTC \\\n"
            "    http_proxy=${http_proxy} \\\n"
            "    https_proxy=${https_proxy} \\\n"
            "    HTTP_PROXY=${HTTP_PROXY} \\\n"
            "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
            "    no_proxy=${no_proxy} \\\n"
            "    NO_PROXY=${NO_PROXY} \\\n"
            "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
            "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
            "    CURL_CA_BUNDLE=${CA_CERT_PATH}"
        )

        jdk_installs = "".join(
            f"    && apt-get install -y --no-install-recommends {pkg} \\\n"
            for pkg in _JDK_PACKAGES
        )

        apt_install = (
            "RUN apt-get update \\\n"
            f"{jdk_installs}"
            "    && apt-get install -y --no-install-recommends \\\n"
            "    git ca-certificates curl unzip make maven \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )

        flatc_build = (
            "RUN apt-get update \\\n"
            "    && apt-get install -y --no-install-recommends cmake g++ \\\n"
            "    && rm -rf /var/lib/apt/lists/* \\\n"
            "    && curl -fsSL -o /tmp/flatbuffers.tgz https://github.com/google/flatbuffers/archive/refs/tags/v1.12.0.tar.gz \\\n"
            "    && echo '62f2223fb9181d1d6338451375628975775f7522185266cd5296571ac152bc45  /tmp/flatbuffers.tgz' | sha256sum -c - \\\n"
            "    && tar -xzf /tmp/flatbuffers.tgz -C /tmp \\\n"
            "    && cd /tmp/flatbuffers-1.12.0 \\\n"
            "    && cmake -G 'Unix Makefiles' -DCMAKE_BUILD_TYPE=Release -DFLATBUFFERS_BUILD_TESTS=OFF \\\n"
            "       -DFLATBUFFERS_BUILD_FLATLIB=OFF -DFLATBUFFERS_BUILD_FLATHASH=OFF -DCMAKE_CXX_FLAGS=-Wno-error . \\\n"
            "    && make -j\"$(nproc)\" flatc \\\n"
            "    && install -m 0755 flatc /usr/local/bin/flatc \\\n"
            "    && cd / && rm -rf /tmp/flatbuffers-1.12.0 /tmp/flatbuffers.tgz \\\n"
            "    && /usr/local/bin/flatc --version"
        )

        cert_symlinks = (
            "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
            "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
            "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
            "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
            "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
            "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
            "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
        )

        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "ARG BASE_COMMIT"
            ),
            proxy_args,
            env_block,
            label,
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.extend(
            [
                cert_symlinks,
                apt_install,
                flatc_build,
                "RUN git config --global --add safe.directory '*'",
                "WORKDIR /home/",
                code,
            ]
        )

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class Skywalking106To9710ImageDefault(Image):
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
        return Skywalking106To9710ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

if ! git cat-file -e {sha}^{{commit}} 2>/dev/null; then
    git fetch --no-tags --depth 1 https://github.com/{org}/{repo}.git {sha}
    git checkout --detach FETCH_HEAD
else
    git checkout --detach {sha}
fi
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "{sha}"

{java_env}

{submodule_setup}

{maven_provision}

{mvn_setup}

# Best-effort warmup: -fn and the timeout keep one bad module from aborting
# dependency resolution for the rest of the reactor. Retried (up to 3x) when
# nothing compiled, e.g. a transient TLS drop while resolving the parent POM;
# the final gate below re-checks the outcome.
for attempt in 1 2 3; do
    timeout {mvn_warmup_timeout} $MVN {mvn_warmup} || true
    if [ -n "$(find . -path '*/target/classes' -type d -print -quit)" ]; then
        break
    fi
    echo "prepare.sh: warmup attempt $attempt compiled nothing; retrying" >&2
    sleep 15
done

# Final hard gate (non-tolerant): toolchain usable, commit still pinned,
# dependencies resolved into ~/.m2 and at least one module compiled.
"$JAVA_HOME/bin/javac" -version
"$MVN" -v >/dev/null
test "$(git rev-parse HEAD)" = "{sha}"
test -n "$(find /root/.m2/repository -name '*.jar' -print -quit)"
test -n "$(find . -path '*/target/classes' -type d -print -quit)"
/usr/local/bin/flatc --version
if [ -d oap-server/server-receiver-plugin/receiver-proto ]; then
    test -d oap-server/server-receiver-plugin/receiver-proto/target/classes
fi
echo "prepare.sh: gate passed"
""".format(
                    org=self.pr.org,
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    java_env=_java_env(self.pr.repo),
                    submodule_setup=_SUBMODULE_SETUP,
                    maven_provision=_MAVEN_PROVISION,
                    mvn_setup=_mvn_setup(self.pr.repo),
                    mvn_warmup=_MVN_WARMUP,
                    mvn_warmup_timeout=_MVN_WARMUP_TIMEOUT,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{java_env}

cd /home/{pr.repo}
{mvn_setup}

set +e
$MVN {mvn_test} 2>&1 | tee /tmp/mvn-stage.log
MVN_STATUS=${{PIPESTATUS[0]}}
set -e

{surefire_dump}

exit $MVN_STATUS
""".format(
                    pr=self.pr,
                    java_env=_java_env(self.pr.repo),
                    mvn_setup=_mvn_setup(self.pr.repo),
                    mvn_test=_MVN_TEST,
                    surefire_dump=_SUREFIRE_DUMP,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{java_env}

cd /home/{pr.repo}
{mvn_setup}

git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' /home/test.patch

set +e
$MVN {mvn_test} 2>&1 | tee /tmp/mvn-stage.log
MVN_STATUS=${{PIPESTATUS[0]}}
set -e

{surefire_dump}

exit $MVN_STATUS
""".format(
                    pr=self.pr,
                    java_env=_java_env(self.pr.repo),
                    mvn_setup=_mvn_setup(self.pr.repo),
                    mvn_test=_MVN_TEST,
                    surefire_dump=_SUREFIRE_DUMP,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{java_env}

cd /home/{pr.repo}
{mvn_setup}

git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' /home/test.patch /home/fix.patch

set +e
$MVN {mvn_test} 2>&1 | tee /tmp/mvn-stage.log
MVN_STATUS=${{PIPESTATUS[0]}}
set -e

{surefire_dump}

exit $MVN_STATUS
""".format(
                    pr=self.pr,
                    java_env=_java_env(self.pr.repo),
                    mvn_setup=_mvn_setup(self.pr.repo),
                    mvn_test=_MVN_TEST,
                    surefire_dump=_SUREFIRE_DUMP,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        proxy_setup = ""
        proxy_cleanup = ""
        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r'^ENV\s*(http[s]?_proxy)="?http[s]?://([^:"]+):(\d+)', line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""\
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml"""
                )

                proxy_cleanup = (
                    "RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml"
                )

        sha = self.pr.base.sha
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        sections = [f"FROM {name}:{tag}", f'ARG BASE_COMMIT="{sha}"']

        if self.global_env:
            sections.append(self.global_env)

        if proxy_setup:
            sections.append(proxy_setup)

        sections.extend(
            [
                f"WORKDIR /home/{self.pr.repo}",
                "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
                copy_commands,
                "RUN bash /home/prepare.sh",
                hardening,
            ]
        )

        if proxy_cleanup:
            sections.append(proxy_cleanup)

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("apache", "skywalking_106_to_9710")
class SKYWALKING_106_TO_9710(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Skywalking106To9710ImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        def record(test_name, tests_run, failures, errors, skipped):
            if failures > 0 or errors > 0:
                failed_tests.add(test_name)
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(test_name)
            elif tests_run > 0:
                passed_tests.add(test_name)

        def finish():
            passed_tests.difference_update(failed_tests)
            passed_tests.difference_update(skipped_tests)
            skipped_tests.difference_update(failed_tests)
            return TestResult(
                passed_count=len(passed_tests),
                failed_count=len(failed_tests),
                skipped_count=len(skipped_tests),
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )

        re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
        re_attr = re.compile(r'(\w+)="([^"]*)"')
        saw_xml = False
        for match in re_case.finditer(test_log):
            attrs = dict(re_attr.findall(match.group(1)))
            method = attrs.get("name", "")
            if not method:
                continue
            saw_xml = True
            classname = attrs.get("classname", "")
            name = f"{classname}#{method}" if classname else method
            body = match.group(3) or ""
            if "<failure" in body or "<error" in body:
                failed_tests.add(name)
            elif "<skipped" in body:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        if saw_xml:
            return finish()

        re_running = re.compile(r"Running\s+([\w.$]+)\s*$")
        re_summary_with_class = re.compile(
            r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
            r"\s*Skipped:\s*(\d+),\s*Time elapsed:.*?(?:--|-)\s+in\s+([\w.$]+)"
        )
        re_summary_plain = re.compile(
            r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
            r"\s*Skipped:\s*(\d+),\s*Time elapsed:"
        )

        current_class = None
        for line in test_log.splitlines():
            match = re_summary_with_class.search(line)
            if match:
                current_class = None
                record(
                    match.group(5),
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                    int(match.group(4)),
                )
                continue

            match = re_summary_plain.search(line)
            if match:
                if current_class:
                    record(
                        current_class,
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        int(match.group(4)),
                    )
                    current_class = None
                continue

            match = re_running.search(line)
            if match:
                current_class = match.group(1)

        return finish()
