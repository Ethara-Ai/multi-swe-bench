import re
import textwrap
from typing import Optional, Union

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
    "-Dgpg.skip=true"
)

# Maven Central returns HTTP 429 to this host, for plain single requests, not
# just under parallel load. The base image therefore mirrors central (see
# maven_mirror below) and these flags absorb the transient failures that remain.
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

# Warm up to test-compile, NOT install.
#
# maven-shade-plugin binds to the package phase, and on the 8.7-era commits its
# createDependencyReducedPom rewrite spins for hours inside
# MavenJDOMWriter.insertAtPreferredLocation while shading the spring-cloud
# gateway plugins. Observed on PR 7377: two builder threads RUNNABLE at 200% CPU
# for 90 minutes with zero new modules and zero new downloads.
#
# test-compile stops before package, so shade never runs, and it still does the
# thing the warmup exists for: resolve every third-party dependency into ~/.m2
# and compile main plus test sources. install was never needed either, because
# the stage scripts run `mvn clean test` at the reactor root, which also stops
# short of package and resolves siblings from the reactor rather than ~/.m2.
#
# -q is deliberately absent: with it, a wedged warmup and a slow one look
# identical in the build log for hours.
_MVN_WARMUP = f"clean test-compile -T 4 -B -fn -DskipTests {_MVN_SKIPS} {_MVN_NET}"

# Hard ceiling so a future pathological module cannot burn the build forever.
# The warmup is best-effort already (`|| true`), so a kill here is survivable.
_MVN_WARMUP_TIMEOUT = "2700"

# The JDKs the base image carries. SkyWalking spans three compiler eras and the
# repo config is not split by PR number, so the image holds every JDK a commit
# in range can ask for and each script picks one at run time.
_JDK_PACKAGES = ("openjdk-8-jdk", "openjdk-11-jdk", "openjdk-17-jdk")

# Choose the JDK from the checked-out commit rather than from the PR number, so
# a PR outside the current dataset still lands on the right compiler.
#
# The root pom is the source of truth. maven-enforcer's requireJavaVersion reads
# 1.8 on every commit up to the v9 line, carrying the comment "Build has not yet
# been updated for Java 9+", and 11 from the v10/MQE line onwards. Verified
# against upstream CI: the 2022-era workflow builds and unit-tests on java 8,
# the 2025-era one on java 11. The two compiler properties are the fallback for
# commits that carry no enforcer block.
#
# The pom is read by absolute path because the callers set this up before they
# cd into the checkout.
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
        8|11|17) ;;
        *) want=11 ;;
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

# NEVER ./mvnw.
#
# The Maven wrapper ships only maven-wrapper.properties and a downloader; the
# jar holding org.apache.maven.wrapper.MavenWrapperMain is fetched over the
# network on first use, and that fetch returns HTTP 429 here, so every mvnw
# invocation dies with
#     curl: (22) The requested URL returned error: 429
#     Error: Could not find or load main class ...MavenWrapperMain
# The warmup swallowed it via `|| true` and the stage scripts produced empty
# logs, which is why all ten PRs reported (0, 0, 0) and came back unresolved.
#
# The base image installs Maven itself, so pick from what is already on disk.
# Which one comes from the repo's own wrapper pin: the 8.x-era commits ask for
# 3.6.x and the v9/v10-era commits for 3.8.4, mirroring how select_java_home()
# reads the pom. Read by absolute path because callers set this up before they
# cd into the checkout, and the run now fails loudly rather than silently.
# Maven provisioning lives here, not in the base image.
#
# The base stops at the clone by design, and select_maven() below is what
# consumes both of these, so they belong beside it. apt supplies 3.6.3 for the
# 8.x-era wrapper pins; 3.8.4 covers the v9/v10-era pins.
#
# The mirror is required because repo.maven.apache.org answers HTTP 429 to this
# host even for single requests. printf rather than a heredoc: the harness builds
# through the classic Docker builder, which drops heredoc bodies and would leave
# a 0-byte settings.xml that makes Maven refuse to start.
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

class SkywalkingImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # DockerfileEnhancer.enhance() hands back a Dockerfile untouched once the
        # "# syntax=" directive is present, and that directive is here on purpose.
        # So the proxy ARGs, the ENV block and the CA symlinks it would otherwise
        # inject are spelled out below. Every proxy default is empty, so no proxy
        # host is ever baked into the image.
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

        # One base image carrying every JDK, not one image per JDK: the tag stays
        # "base", so all PRs still share a single build and a single layer cache.
        # select_java_home() in the stage scripts picks between them per commit.
        #
        # One apt-get per JDK, not one apt-get for all of them. openjdk-11-jre-headless
        # and openjdk-17-jre-headless both ship /usr/share/binfmts, and when they
        # unpack in the same dpkg transaction that directory is missing by the time
        # their configure steps run, so update-alternatives dies with
        #     error creating symbolic link '/usr/share/binfmts/jar.dpkg-tmp'
        # and apt exits 100. Installing binfmt-support first does not help; only
        # separate transactions do, because each package then configures before the
        # next one unpacks. Verified on arm64 against ubuntu:22.04.
        jdk_installs = "".join(
            f"    && apt-get install -y --no-install-recommends {pkg} \\\n"
            for pkg in _JDK_PACKAGES
        )

        # ubuntu:22.04 ships no ca-certificates bundle, so the symlink farm has to
        # follow the apt install rather than precede it as it does in images whose
        # base already carries one.
        apt_install = (
            "RUN apt-get update \\\n"
            f"{jdk_installs}"
            "    && apt-get install -y --no-install-recommends \\\n"
            "    git ca-certificates curl unzip make maven \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
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

        # The base stops at the clone. No BASE_COMMIT checkout and no history
        # rewrite happen here, so this one image serves every PR and each PR image
        # pins and strips its own history on top of it.
        # Pin linux/amd64, do not inherit the builder's architecture.
        #
        # SkyWalking 6.x/7.x pins protobuf 3.3.0, and protoc only began shipping
        # a linux-aarch_64 binary at 3.11.4. On an Apple Silicon host the arm64
        # build therefore dies in apm-network with
        #     Missing: com.google.protobuf:protoc:exe:linux-aarch_64:3.3.0
        # which is module 6 of 171, so 121 modules fail behind it and only the
        # 57 protobuf-free tests ever run. Identical counts in all three stages
        # mean no f2p/p2p transitions and an invalid instance. linux-x86_64
        # protoc exists for every version this repo pins, so amd64 builds all
        # eras. It emulates on arm64 hosts, which is slower but correct.
        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM --platform=linux/amd64 {image_name}",
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
                apt_install,
                cert_symlinks,
                "WORKDIR /home/",
                code,
            ]
        )

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class SkywalkingImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return SkywalkingImageBase(self.pr, self._config)

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

timeout {mvn_warmup_timeout} $MVN {mvn_warmup} || true
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
                # global_env emits quoted values (ENV http_proxy="http://h:p"),
                # so the quote has to be optional or this branch never fires and
                # Maven silently ignores the proxy.
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

        # The SHA is inlined literally rather than carried as ARG/ENV BASE_COMMIT,
        # so nothing about which commit this image pins survives into the running
        # container's environment.
        sha = self.pr.base.sha
        hardening = Image._HARDENING_BLOCK.rstrip("\n").replace(
            "${BASE_COMMIT}", sha
        )

        # No "# syntax=" directive and no CMD here: the PR image inherits the
        # base image's CMD, and DockerfileEnhancer skips any image whose
        # dependency() is an Image rather than a string, so nothing is injected.
        sections = [f"FROM {name}:{tag}"]

        if self.global_env:
            sections.append(self.global_env)

        if proxy_setup:
            sections.append(proxy_setup)

        sections.extend(
            [
                f"WORKDIR /home/{self.pr.repo}",
                f"RUN git reset --hard\nRUN git checkout {sha}",
                copy_commands,
                # History stripping belongs to the PR image, not to the base image
                # and not to prepare.sh, and it runs before prepare.sh so the
                # checkout prepare.sh builds on is already pinned and stripped.
                hardening,
                "RUN bash /home/prepare.sh",
            ]
        )

        if proxy_cleanup:
            sections.append(proxy_cleanup)

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("apache", "skywalking")
class Skywalking(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SkywalkingImageDefault(self.pr, self._config)

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
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
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
            skipped_tests.difference_update(failed_tests)
            skipped_tests.difference_update(passed_tests)
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
