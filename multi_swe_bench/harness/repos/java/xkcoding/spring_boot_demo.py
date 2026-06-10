import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# xkcoding/spring-boot-demo is a learning monorepo: the root pom.xml aggregates
# 55-62 independent Spring Boot demo modules (each with <parent> ->
# com.xkcoding:spring-boot-demo resolved via ../pom.xml). Many modules require
# external services (Redis/MySQL/Elasticsearch/LDAP/mail) so a whole-reactor
# `mvn clean test` is impractical and fails fast. Each PR in the dataset touches
# one (or a few) module(s), so the run scripts derive the target module(s)
# dynamically from the patch file paths and run `mvn clean test` only there.
# This also transparently handles the `spring-boot-demo-*` -> `demo-*` module
# rename (single config, no era split): Java 1.8 + Maven + Spring Boot
# 2.1.0.RELEASE is consistent across every base commit.

_DETECT_MODULES = r"""
# Derive the target module dir(s) from the new-path side of a patch:
# `+++ b/<path>` (creations/modifications) and `rename to <path>` (the PR #28
# restructure). First path segment is the module; root-level files (README,
# pom.xml, TODO, LICENSE) have no '/' so awk NF>1 drops them, and the pom.xml
# guard in run_module_tests drops non-module dirs such as assets/.
_modules_from() {
  cat "$@" 2>/dev/null \
    | grep -E '^\+\+\+ b/|^rename to ' \
    | sed -E 's@^\+\+\+ b/@@; s@^rename to @@' \
    | grep -v '^/dev/null' \
    | awk -F/ 'NF>1{print $1}' \
    | sort -u
}

# Target the module(s) under test (test.patch). Every demo module is
# self-contained (its only Maven parent is the repo root pom resolved via
# ../pom.xml), so the fix never lives outside the tested module; modules that
# the fix patch only touches via README/pom version bumps (e.g. demo-nacos,
# demo-ureport2 in PR #170) are intentionally NOT built. Fall back to fix.patch
# only if test.patch yields nothing.
detect_modules() {
  local m
  m=$(_modules_from /home/test.patch)
  if [ -z "$m" ]; then
    m=$(_modules_from /home/test.patch /home/fix.patch)
  fi
  echo "$m"
}

run_module_tests() {
  local found=0
  for m in $(detect_modules); do
    if [ -f "/home/__REPO__/$m/pom.xml" ]; then
      found=1
      echo "===== mvn clean test in module: $m ====="
      ( cd "/home/__REPO__/$m" && \
        mvn clean test -Dsurefire.useFile=false -Dmaven.test.skip=false \
          -DskipTests=false -DfailIfNoTests=false -B ) || true
    else
      echo "SKIP module $m (no pom.xml at this stage)"
    fi
  done
  if [ "$found" -eq 0 ]; then
    echo "No buildable target modules detected for this stage."
  fi
}
"""

# git apply must ignore binary hunks. The dataset patches are generated without
# `git diff --binary`, so binary files appear as content-less
# "Binary files ... differ" markers; including them aborts the whole apply.
_BIN_EXCLUDES = (
    "--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.JPG' "
    "--exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.pptx' "
    "--exclude='*.otf' --exclude='*.eot' --exclude='*.ttf' --exclude='*.woff' "
    "--exclude='*.woff2' --exclude='*.jar' --exclude='*.keystore'"
)


class SpringBootDemoImageBase(Image):
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
        org, repo = self.pr.org, self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return (
            '# syntax=docker/dockerfile:1.6\n'
            '\n'
            'FROM ubuntu:22.04\n'
            '\n'
            'ARG TARGETARCH\n'
            f'ARG REPO_URL="{repo_url}"\n'
            'ARG BASE_COMMIT\n'
            '\n'
            'ARG http_proxy=""\n'
            'ARG https_proxy=""\n'
            'ARG HTTP_PROXY=""\n'
            'ARG HTTPS_PROXY=""\n'
            'ARG no_proxy="localhost,127.0.0.1,::1"\n'
            'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
            'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"\n'
            '\n'
            'ENV DEBIAN_FRONTEND=noninteractive \\\n'
            '    LANG=C.UTF-8 \\\n'
            '    TZ=UTC \\\n'
            '    http_proxy=${http_proxy} \\\n'
            '    https_proxy=${https_proxy} \\\n'
            '    HTTP_PROXY=${HTTP_PROXY} \\\n'
            '    HTTPS_PROXY=${HTTPS_PROXY} \\\n'
            '    no_proxy=${no_proxy} \\\n'
            '    NO_PROXY=${NO_PROXY} \\\n'
            '    SSL_CERT_FILE=${CA_CERT_PATH} \\\n'
            '    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n'
            '    CURL_CA_BUNDLE=${CA_CERT_PATH}\n'
            '\n'
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} (JDK 8 / Spring Boot 2.1) Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"\n'
            '\n'
            'RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt\n'
            '\n'
            'RUN --mount=type=secret,id=mitm_ca,required=0 \\\n'
            '    if [ -f /run/secrets/mitm_ca ]; then \\\n'
            '        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \\\n'
            '    fi\n'
            '\n'
            'ARG JDK_PKG="openjdk-8-jdk-headless"\n'
            'ARG MVN_URL="https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.tar.gz"\n'
            'ARG MVN_HOME="/opt/apache-maven-3.9.9"\n'
            '\n'
            'WORKDIR /home/\n'
            '\n'
            'RUN apt-get update && apt-get install -y --no-install-recommends \\\n'
            '    ca-certificates curl git gnupg make sudo wget \\\n'
            '    ${JDK_PKG} \\\n'
            '    && rm -rf /var/lib/apt/lists/*\n'
            '\n'
            'RUN curl -fsSL "${MVN_URL}" | tar xz -C /opt && \\\n'
            '    ln -s "${MVN_HOME}/bin/mvn" /usr/bin/mvn\n'
            '\n'
            f'{code}\n'
            '\n'
            f'WORKDIR /home/{repo}\n'
            '\n'
            'RUN git reset --hard\n'
            'RUN git checkout ${BASE_COMMIT}\n'
            '\n'
            'CMD ["/bin/bash"]\n'
        )


class SpringBootDemoImageDefault(Image):
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
        return SpringBootDemoImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        detect = _DETECT_MODULES.replace("__REPO__", self.pr.repo)
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
                "enable_tests.sh",
                """#!/bin/bash
# Override any hardcoded <skip>true</skip> in maven-surefire-plugin config
# and <maven.test.skip>true</maven.test.skip> property so tests actually run.
set -e
find . -name pom.xml -exec sed -i '/<artifactId>maven-surefire-plugin/,/<\\/plugin>/{ s|<skip>true</skip>|<skip>false</skip>|g }' {} +
find . -name pom.xml -exec sed -i 's|<maven.test.skip>true</maven.test.skip>|<maven.test.skip>false</maven.test.skip>|g' {} +
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

bash /home/enable_tests.sh
{detect}
# Warm the local ~/.m2 cache for the target module(s) so the graded runs are
# offline-fast; failures here are non-fatal (services may be unavailable).
run_module_tests || true
""".format(pr=self.pr, detect=detect),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
git clean -fdq || true
git checkout {pr.base.sha}
bash /home/enable_tests.sh
{detect}
run_module_tests
""".format(pr=self.pr, detect=detect),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
git clean -fdq || true
git checkout {pr.base.sha}
git apply --whitespace=nowarn {excludes} \\
  /home/test.patch 2>/dev/null \\
  || git apply --whitespace=nowarn --3way {excludes} \\
       /home/test.patch \\
  || true
bash /home/enable_tests.sh
{detect}
run_module_tests
""".format(pr=self.pr, detect=detect, excludes=_BIN_EXCLUDES),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
git clean -fdq || true
git checkout {pr.base.sha}
git apply --whitespace=nowarn {excludes} \\
  /home/test.patch 2>/dev/null \\
  || git apply --whitespace=nowarn --3way {excludes} \\
       /home/test.patch \\
  || true
git apply --whitespace=nowarn {excludes} \\
  /home/fix.patch 2>/dev/null \\
  || git apply --whitespace=nowarn --3way {excludes} \\
       /home/fix.patch \\
  || true
bash /home/enable_tests.sh
{detect}
run_module_tests
""".format(pr=self.pr, detect=detect, excludes=_BIN_EXCLUDES),
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

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("xkcoding", "spring-boot-demo")
class SPRING_BOOT_DEMO(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpringBootDemoImageDefault(self.pr, self._config)

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

        # Maven Surefire per-test-class summary line, e.g.:
        #   Tests run: 1, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1.534 s - in com.xkcoding.log.aop.SpringBootDemoLogAopApplicationTests
        #   Tests run: 3, Failures: 0, Errors: 3, Skipped: 0, Time elapsed: 0.019 s <<< FAILURE! - in com.xkcoding.springbootdemohttps.SpringBootDemoHttpsApplicationTests
        pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d.]+ .+? in (.+)"
        )

        for line in test_log.splitlines():
            match = pattern.search(line)
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

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
