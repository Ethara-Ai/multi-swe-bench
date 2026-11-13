import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class Hudi18816To19903ImageBase(Image):
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
        return "base-18816_to_19903"

    def workdir(self) -> str:
        return "base-18816_to_19903"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        git \\
        ca-certificates \\
        curl \\
        unzip \\
        procps \\
        openjdk-11-jdk \\
        openjdk-17-jdk && \\
    rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-11-openjdk && \\
    ln -sf /usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-17-openjdk

RUN case "$(dpkg --print-architecture)" in \\
        arm64) jdk_arch=aarch64 ;; \\
        amd64) jdk_arch=x64 ;; \\
        *) echo "unsupported architecture" >&2; exit 1 ;; \\
    esac && \\
    curl -fsSL "https://api.adoptium.net/v3/binary/latest/25/ga/linux/${{jdk_arch}}/jdk/hotspot/normal/eclipse" -o /tmp/jdk25.tar.gz && \\
    mkdir -p /usr/lib/jvm/jdk-25 && \\
    tar xzf /tmp/jdk25.tar.gz -C /usr/lib/jvm/jdk-25 --strip-components=1 && \\
    rm /tmp/jdk25.tar.gz && \\
    /usr/lib/jvm/jdk-25/bin/java -version

ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk
ENV PATH=/usr/lib/jvm/java-11-openjdk/bin:$PATH

RUN curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.tar.gz -o /tmp/maven.tar.gz && \\
    tar xzf /tmp/maven.tar.gz -C /opt && \\
    ln -sf /opt/apache-maven-3.9.9/bin/mvn /usr/local/bin/mvn && \\
    rm /tmp/maven.tar.gz

WORKDIR /home/

{code}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Hudi18816To19903ImageDefault(Image):
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
        return Hudi18816To19903ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        header = re.compile(r"^diff --git a/.+? b/(.+)$", re.M)
        fix_paths = header.findall(self.pr.fix_patch)
        test_paths = header.findall(self.pr.test_patch)

        modules = sorted(
            {p.split("/src/", 1)[0] for p in fix_paths + test_paths if "/src/" in p}
        )
        test_sources = sorted(
            p
            for p in test_paths
            if re.search(r"/src/test/(java|scala)/.+\.(java|scala)$", p)
        )

        binary_added = re.compile(r"^Binary files (?:a/\S+|/dev/null) and b/(\S+) differ$", re.M)
        binary_deleted = re.compile(r"^Binary files a/(\S+) and /dev/null differ$", re.M)
        test_bins = sorted(set(binary_added.findall(self.pr.test_patch)))
        fix_bins = sorted(set(binary_added.findall(self.pr.fix_patch)))
        test_bins_deleted = sorted(set(binary_deleted.findall(self.pr.test_patch)))
        fix_bins_deleted = sorted(set(binary_deleted.findall(self.pr.fix_patch)))
        test_excludes = " ".join(
            f"--exclude='{p}'" for p in sorted(set(test_bins + test_bins_deleted))
        )
        fix_excludes = " ".join(
            f"--exclude='{p}'"
            for p in sorted(
                set(test_bins + test_bins_deleted + fix_bins + fix_bins_deleted)
            )
        )

        fetch_binaries = ""
        if test_bins or fix_bins:
            fetch_binaries = """rm -rf /home/pr-binaries /tmp/pr-head
git init -q /tmp/pr-head
git -C /tmp/pr-head fetch --quiet --depth 1 https://github.com/{org}/{repo}.git refs/pull/{number}/head
for kind_path in {pairs}; do
    kind="${{kind_path%%:*}}"
    p="${{kind_path#*:}}"
    mkdir -p "/home/pr-binaries/$kind/$(dirname "$p")"
    git -C /tmp/pr-head show "FETCH_HEAD:$p" > "/home/pr-binaries/$kind/$p"
    test -s "/home/pr-binaries/$kind/$p"
done
rm -rf /tmp/pr-head
""".format(
                org=self.pr.org,
                repo=self.pr.repo,
                number=self.pr.number,
                pairs=" ".join(
                    [f"test:{p}" for p in test_bins] + [f"fix:{p}" for p in fix_bins]
                ),
            )

        def apply_binaries(kinds: list[str]) -> str:
            lines = []
            for kind in kinds:
                lines.append(
                    f"if [ -d /home/pr-binaries/{kind} ]; then cp -a /home/pr-binaries/{kind}/. /home/{self.pr.repo}/; fi"
                )
            deleted = test_bins_deleted + (fix_bins_deleted if "fix" in kinds else [])
            for p in deleted:
                lines.append(f"rm -f '{p}'")
            return "\n".join(lines)

        common_env = """export CI=true
export MAVEN_OPTS="-Xmx8g -XX:+UseG1GC"
JDK11=/usr/lib/jvm/java-11-openjdk
JDK17=/usr/lib/jvm/java-17-openjdk
JDK25=/usr/lib/jvm/jdk-25
FLINK_PROFILE=$(sed -n 's|.*<flink.version>\\${\\(flink[0-9.]*\\)\\.version}</flink.version>.*|\\1|p' /home/hudi/pom.xml | head -n 1)
test -n "$FLINK_PROFILE"
MVN_FLAGS="-B -ntp -Dscala-2.12 -Dspark3.5 -D$FLINK_PROFILE -Dcheckstyle.skip=true -Drat.skip=true -Djacoco.skip=true -Dscalastyle.skip=true -Dmaven.javadoc.skip=true -Denforcer.skip=true -Dgpg.skip=true -Dmaven.source.skip=true"
TRINO_UPSTREAM=":hudi-common,:hudi-hive-sync,:hudi-io,:hudi-sync-common,:hudi-client-common,:hudi-java-client"
"""

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
if ! git cat-file -e "{sha}^{{commit}}" 2>/dev/null; then
    git fetch --quiet origin {sha}
fi
git checkout --detach {sha}
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"

{common_env}
: > /home/hudi_modules.txt
trino=0
for m in {modules}; do
    [ -f "$m/pom.xml" ] || continue
    if [ "$m" = "hudi-trino" ]; then
        trino=1
        echo "$m" >> /home/hudi_modules.txt
        continue
    fi
    if JAVA_HOME=$JDK11 mvn $MVN_FLAGS -q validate -pl "$m" > /dev/null 2>&1; then
        echo "$m" >> /home/hudi_modules.txt
    else
        echo "prepare: $m is not part of the default reactor, skipping"
    fi
done
cat /home/hudi_modules.txt

normal=$(grep -vx hudi-trino /home/hudi_modules.txt | paste -sd, - || true)

if [ "$trino" = "1" ]; then
    JAVA_HOME=$JDK17 mvn $MVN_FLAGS install -Dmaven.test.skip=true -pl "$TRINO_UPSTREAM" -am
    if [ -n "$normal" ]; then
        JAVA_HOME=$JDK17 mvn $MVN_FLAGS install -DskipTests -T 2 -pl "$normal" -am
    fi
    trino_sha=$(sed -n 's|.*<trino.sha>\\(.*\\)</trino.sha>.*|\\1|p' pom.xml)
    test -n "$trino_sha"
    rm -rf /home/trino-src
    git init -q /home/trino-src
    git -C /home/trino-src fetch --quiet --depth 1 https://github.com/trinodb/trino.git "$trino_sha"
    git -C /home/trino-src -c advice.detachedHead=false checkout --quiet --detach FETCH_HEAD
    JAVA_HOME=$JDK25 PATH=$JDK25/bin:$PATH bash scripts/trino/bootstrap_trino.sh /home/trino-src --skip-checkout
    rm -rf /home/trino-src
    JAVA_HOME=$JDK25 mvn $MVN_FLAGS -Phudi-trino -pl hudi-trino install -Dmaven.test.skip=true
    JAVA_HOME=$JDK25 mvn $MVN_FLAGS -Phudi-trino,hudi-trino-tests -pl hudi-trino test-compile
else
    test -n "$normal"
    JAVA_HOME=$JDK11 mvn $MVN_FLAGS install -DskipTests -T 2 -pl "$normal" -am
fi

{fetch_binaries}
test -d ~/.m2/repository/org/apache/maven/surefire/surefire-junit-platform
while read -r m; do
    if [ ! -d "$m/target/classes" ] && [ ! -d "$m/target/test-classes" ]; then
        echo "prepare: module $m was not built" >&2
        exit 1
    fi
done < /home/hudi_modules.txt
echo "prepare: DEPS_OK"
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    modules=" ".join(modules),
                    common_env=common_env,
                    fetch_binaries=fetch_binaries,
                ),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
set -eo pipefail

{common_env}
cd /home/{repo}

trino=0
grep -qx hudi-trino /home/hudi_modules.txt && trino=1
normal=$(grep -vx hudi-trino /home/hudi_modules.txt | paste -sd, - || true)

if [ -n "$normal" ]; then
    if [ "$trino" = "1" ]; then
        build_jdk=$JDK17
    else
        build_jdk=$JDK11
    fi
    JAVA_HOME=$build_jdk mvn $MVN_FLAGS install -DskipTests -fae -T 2 -pl "$normal" -am
fi
if [ "$trino" = "1" ]; then
    JAVA_HOME=$JDK25 mvn $MVN_FLAGS -Phudi-trino -pl hudi-trino install -Dmaven.test.skip=true
fi

while read -r m; do
    junit=""
    suites=""
    for f in {test_sources}; do
        case "$f" in
            "$m"/src/test/*) ;;
            *) continue ;;
        esac
        [ -f "$f" ] || continue
        cls=$(basename "$f")
        cls="${{cls%.*}}"
        case "$cls" in
            Test*|*Test|*Tests|*TestCase) ;;
            *) continue ;;
        esac
        rel="${{f#"$m"/src/test/*/}}"
        rel="${{rel%.*}}"
        fqn="${{rel//\\//.}}"
        if [ "${{f##*.}}" = "scala" ] && ! grep -q "org\\.junit" "$f"; then
            suites="${{suites:+$suites,}}$fqn"
        else
            junit="${{junit:+$junit,}}$fqn"
        fi
    done

    echo "run-tests: module=$m"
    echo "run-tests: junit=$junit"
    echo "run-tests: scalatest=$suites"
    if [ -z "$junit" ] && [ -z "$suites" ]; then
        continue
    fi

    args="-Dsurefire.useFile=false -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false"
    if [ -n "$junit" ]; then
        args="$args -Dtest=$junit"
    else
        args="$args -Dtest=NoSuchHudiTest"
    fi
    if [ -n "$suites" ]; then
        args="$args -Dsuites=$suites"
    else
        args="$args -DskipUTs=true"
    fi

    if [ "$m" = "hudi-trino" ]; then
        JAVA_HOME=$JDK25 mvn $MVN_FLAGS test -fn -Phudi-trino,hudi-trino-tests -pl "$m" $args
    else
        JAVA_HOME=$JDK11 mvn $MVN_FLAGS test -fn -pl "$m" $args
    fi
done < /home/hudi_modules.txt
""".format(
                    repo=self.pr.repo,
                    test_sources=" ".join(test_sources),
                    common_env=common_env,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
bash /home/run-tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn {excludes} /home/test.patch
{apply_binaries}
bash /home/run-tests.sh

""".format(
                    repo=self.pr.repo,
                    excludes=test_excludes,
                    apply_binaries=apply_binaries(["test"]),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn {excludes} /home/test.patch /home/fix.patch
{apply_binaries}
bash /home/run-tests.sh

""".format(
                    repo=self.pr.repo,
                    excludes=fix_excludes,
                    apply_binaries=apply_binaries(["test", "fix"]),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"
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
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{proxy_setup}

{copy_commands}
{prepare_commands}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
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

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("apache", "hudi_18816_to_19903")
@Instance.register("apache", "hudi")
class HUDI_18816_TO_19903(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Hudi18816To19903ImageDefault(self.pr, self._config)

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

        ansi_escape_pattern = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape_pattern.sub("", test_log)

        surefire_pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)(?:, Flakes: \d+)?, Time elapsed: [\d.]+ .+? in (\S+)"
        )
        suites_pattern = re.compile(r"^run-tests: scalatest=(.*)$")
        suite_header_pattern = re.compile(r"^([A-Za-z_$][\w$]*):\s*$")
        scala_case_pattern = re.compile(r"^- (.*)$")

        suite_names = {}
        current_suite = None
        suite_passed = set()
        suite_failed = set()
        suite_skipped = set()

        for line in test_log.splitlines():
            match = surefire_pattern.search(line)
            if match:
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5).strip()

                if (
                    tests_run > 0
                    and failures == 0
                    and errors == 0
                    and skipped != tests_run
                ):
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)
                continue

            stripped = line.strip()

            match = suites_pattern.match(stripped)
            if match:
                current_suite = None
                for fqn in filter(None, match.group(1).split(",")):
                    suite_names[fqn.rsplit(".", 1)[-1]] = fqn
                continue

            match = suite_header_pattern.match(stripped)
            if match and match.group(1) in suite_names:
                current_suite = suite_names[match.group(1)]
                continue

            if current_suite is None:
                continue

            if "*** ABORTED ***" in stripped:
                suite_failed.add(current_suite)
                continue

            match = scala_case_pattern.match(stripped)
            if match:
                case = match.group(1)
                if "*** FAILED ***" in case:
                    suite_failed.add(current_suite)
                elif "!!! IGNORED !!!" in case or "!!! CANCELED !!!" in case:
                    suite_skipped.add(current_suite)
                else:
                    suite_passed.add(current_suite)

        failed_tests |= suite_failed
        passed_tests |= suite_passed - suite_failed
        skipped_tests |= suite_skipped - suite_passed - suite_failed

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
