from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

SCAFFOLD = r"""
scaffold_amqp() {
  if ! grep -q '<module>amqp</module>' pom.xml; then
    sed -i 's|<module>core</module>|<module>core</module>\n        <module>amqp</module>|' pom.xml
  fi

  mkdir -p amqp/src/main/java amqp/src/test/java

  if [ ! -f amqp/pom.xml ]; then
    cat > amqp/pom.xml <<'MSWB_SCAFFOLD_POM'
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>io.cloudevents</groupId>
        <artifactId>cloudevents-parent</artifactId>
        <version>2.0.0-SNAPSHOT</version>
    </parent>
    <artifactId>cloudevents-amqp-proton</artifactId>
    <name>CloudEvents - Proton AMQP Binding</name>
    <packaging>jar</packaging>
    <properties>
        <protonj.version>0.33.7</protonj.version>
        <jsr305.version>3.0.2</jsr305.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.apache.qpid</groupId>
            <artifactId>proton-j</artifactId>
            <version>${protonj.version}</version>
        </dependency>
        <dependency>
            <groupId>io.cloudevents</groupId>
            <artifactId>cloudevents-core</artifactId>
            <version>${project.version}</version>
        </dependency>
        <dependency>
            <groupId>com.google.code.findbugs</groupId>
            <artifactId>jsr305</artifactId>
            <version>${jsr305.version}</version>
            <scope>provided</scope>
            <optional>true</optional>
        </dependency>
        <dependency>
            <groupId>org.junit.jupiter</groupId>
            <artifactId>junit-jupiter</artifactId>
            <version>${junit-jupiter.version}</version>
        </dependency>
        <dependency>
            <groupId>io.cloudevents</groupId>
            <artifactId>cloudevents-core</artifactId>
            <classifier>tests</classifier>
            <type>test-jar</type>
            <version>${project.version}</version>
            <scope>test</scope>
        </dependency>
        <dependency>
            <groupId>org.assertj</groupId>
            <artifactId>assertj-core</artifactId>
            <version>${assertj-core.version}</version>
            <scope>test</scope>
        </dependency>
    </dependencies>
</project>
MSWB_SCAFFOLD_POM
  fi
}
"""

EMIT_TESTS = r"""
emit_tests() {
  find . -path '*/target/surefire-reports/TEST-*.xml' 2>/dev/null | sort | while read -r f; do
    tr '\n' ' ' < "$f" | sed 's/<testcase/\n<testcase/g' | sed '1d' | while IFS= read -r el; do
      cls=$(printf '%s' "$el" | sed -n 's/.*classname="\([^"]*\)".*/\1/p')
      meth=$(printf '%s' "$el" | sed -n 's/.*[^s]name="\([^"]*\)".*/\1/p')
      [ -z "$cls" ] && continue
      [ -z "$meth" ] && continue
      meth=$(printf '%s' "$meth" | sed -e 's/{[^}]*}//g' -e 's/\[[^][]*\]$//' -e 's/[[:space:]]*$//')
      status=PASS
      case "$el" in
        *"<failure"*|*"<error"*) status=FAIL ;;
        *"<skipped"*)            status=SKIP ;;
      esac
      printf '%s %s#%s\n' "$status" "$cls" "$meth"
    done
  done | sort -u | awk '
    { st=$1; name=$2;
      if (!(name in best)) best[name]=st;
      else {
        if (st=="FAIL" || best[name]=="FAIL") best[name]="FAIL";
        else if (st=="PASS" || best[name]=="PASS") best[name]="PASS";
      }
    }
    END { for (n in best) printf "MSWB_TEST: %s %s\n", best[n], n }
  ' | sort -k3
}
"""

MVN_TEST = (
    "mvn -B -o -fae clean test"
    " -Dmaven.test.failure.ignore=true"
    " -Dsurefire.useFile=false"
    " -DfailIfNoTests=false"
)


class ImageBase(Image):

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
        return "maven:3.8-eclipse-temurin-8"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-270-to-270"

    def workdir(self) -> str:
        return "base-270-to-270"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    MAVEN_OPTS="-Xmx3g" \\
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    sudo \\
    unzip \\
    wget \\
    && rm -rf /var/lib/apt/lists/*


WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):

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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euxo pipefail
{SCAFFOLD}
cd /home/{repo}

git config core.autocrlf input
git config core.filemode false
git reset --hard
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

scaffold_amqp

mvn -B -fae clean install \\
    -DskipTests \\
    -Dmaven.test.failure.ignore=true \\
    -DfailIfNoTests=false || true

mvn -B -fae test \\
    -Dmaven.test.failure.ignore=true \\
    -Dsurefire.useFile=false \\
    -DfailIfNoTests=false || true

git checkout -- .
git clean -fdx
test "$(git rev-parse HEAD)" = "{sha}"
test ! -e amqp
bash /home/check_git_changes.sh
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{SCAFFOLD}{EMIT_TESTS}
cd /home/{repo}

scaffold_amqp

mvn_status=0
{MVN_TEST} || mvn_status=$?

emit_tests

if ! find . -path '*/target/surefire-reports/TEST-*.xml' | grep -q .; then
  echo "run.sh: no surefire reports produced (mvn exit ${{mvn_status}})" >&2
  exit 1
fi
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{SCAFFOLD}{EMIT_TESTS}
cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

scaffold_amqp

mvn_status=0
{MVN_TEST} || mvn_status=$?

emit_tests

if ! find . -path '*/target/surefire-reports/TEST-*.xml' | grep -q .; then
  echo "test-run.sh: no surefire reports produced (mvn exit ${{mvn_status}})" >&2
  exit 1
fi
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{SCAFFOLD}{EMIT_TESTS}
cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

scaffold_amqp

mvn_status=0
{MVN_TEST} || mvn_status=$?

emit_tests

if ! find . -path '*/target/surefire-reports/TEST-*.xml' | grep -q .; then
  echo "fix-run.sh: no surefire reports produced (mvn exit ${{mvn_status}})" >&2
  exit 1
fi
""",
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

        global_env = f"\n{self.global_env}\n" if self.global_env else ""

        return f"""FROM {name}:{tag}
{global_env}
{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN set -eux; \\
    git checkout --detach {self.pr.base.sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse {self.pr.base.sha})"; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{prepare_commands}
{self.clear_env}
"""


@Instance.register("cloudevents", "sdk_java_270_to_270")
class SDK_JAVA_270_TO_270(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        emitted = re.compile(r"^MSWB_TEST:\s+(PASS|FAIL|SKIP)\s+(\S+)\s*$")

        for line in clean_log.splitlines():
            match = emitted.match(line.strip())
            if not match:
                continue

            status = match.group(1)
            test_name = match.group(2)

            if status == "FAIL":
                failed_tests.add(test_name)
            elif status == "SKIP":
                skipped_tests.add(test_name)
            else:
                passed_tests.add(test_name)

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


Instance.register("cloudevents", "sdk-java")(SDK_JAVA_270_TO_270)
