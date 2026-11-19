import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_JDK_IMAGE = "eclipse-temurin:11-jdk"
_BASE_TAG = "base-788_to_788"
_GRADLE_TASK = ":java-jwt:test"
_GRADLE_FLAGS = (
    "--no-daemon --console=plain --continue "
    "-x checkstyleMain -x checkstyleTest -x checkstyleJmh "
    "-x jacocoTestReport"
)

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
"""

_EMIT_RESULTS_PY = r'''import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

repo = sys.argv[1]
rank = {"PASSED": 0, "SKIPPED": 1, "FAILED": 2}
results = {}

patterns = [
    repo + "/**/build/test-results/test/TEST-*.xml",
]

seen = set()
for pattern in patterns:
    for path in sorted(glob.glob(pattern, recursive=True)):
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("testcase"):
            cls = (case.get("classname") or "").strip()
            raw = re.sub(r"\s+", " ", (case.get("name") or "")).strip()
            method = re.match(r"^([A-Za-z_$][\w$]*)\s*\(", raw)
            name = method.group(1) if method else raw
            if not cls or not name:
                continue
            status = "PASSED"
            for child in case:
                tag = child.tag.split("}")[-1].lower()
                if tag in ("failure", "error"):
                    status = "FAILED"
                    break
                if tag == "skipped":
                    status = "SKIPPED"
                    break
            key = cls + "#" + name
            prev = results.get(key)
            if prev is None or rank[status] > rank[prev]:
                results[key] = status

for key in sorted(results):
    print("TESTCASE " + results[key] + " " + key)
sys.stderr.write("emit_results: %d test cases\n" % len(results))
'''

_INIT_GRADLE = """allprojects {
    repositories {
        mavenCentral()
        gradlePluginPortal()
    }
}
"""

_PREPARE_SH = """#!/bin/bash
set -e

export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __BASE_SHA__^{commit} 2>/dev/null || git fetch --quiet --no-tags origin __BASE_SHA__
git checkout --detach __BASE_SHA__
if [ -f .gitmodules ]; then
    git submodule update --init --recursive
fi
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__BASE_SHA__"

test -x "$JAVA_HOME/bin/javac"

mkdir -p "$GRADLE_USER_HOME"
cp /home/init.gradle "$GRADLE_USER_HOME/init.gradle"

chmod +x gradlew
sed -i "s|^distributionUrl=.*|distributionUrl=file:/opt/gradle-dist/gradle-bin.zip|" \
    gradle/wrapper/gradle-wrapper.properties

./gradlew __GRADLE_TASK__ __GRADLE_FLAGS__ || true

# Hard gate: the warm-up above is tolerated with `|| true` only because this
# gate re-checks what it was supposed to accomplish. A warm-up that aborted
# before resolving the project's dependencies (e.g. an invalid `-x <task>`
# target) leaves no test XML and an empty module cache, which would otherwise
# surface later as three identical 0/0/0 stages and an invalid report.
test "$(python3 /home/emit_results.py /home/__REPO__ | wc -l)" -gt 0
for _coord in com.fasterxml.jackson.core junit org.mockito org.hamcrest org.bouncycastle; do
    test -d "$GRADLE_USER_HOME/caches/modules-2/files-2.1/$_coord"
done

git checkout -- gradle/wrapper/gradle-wrapper.properties
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"

cd /home/__REPO__
rm -rf lib/build/test-results

chmod +x gradlew
sed -i "s|^distributionUrl=.*|distributionUrl=file:/opt/gradle-dist/gradle-bin.zip|" \
    gradle/wrapper/gradle-wrapper.properties

./gradlew __GRADLE_TASK__ __GRADLE_FLAGS__ || true

git checkout -- gradle/wrapper/gradle-wrapper.properties 2>/dev/null || true

python3 /home/emit_results.py /home/__REPO__
exit 0
"""

_RUN_SH = """#!/bin/bash
set -e
bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -e
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -e
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT
ARG GRADLE_DIST_URL="https://services.gradle.org/distributions/gradle-6.9.2-bin.zip"

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

ENV GRADLE_USER_HOME=/home/gradle-home \\
    GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8"

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
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
    ca-certificates curl git python3 unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/gradle-dist && \\
    curl -fsSL --retry 8 --retry-all-errors --retry-delay 5 --connect-timeout 60 --max-time 1800 \\
        -o /opt/gradle-dist/gradle-bin.zip "${GRADLE_DIST_URL}" && \\
    test -s /opt/gradle-dist/gradle-bin.zip

RUN update-ca-certificates && \\
    csplit -z -f /tmp/sysca- -b "%03d.pem" "${CA_CERT_PATH}" "/-----BEGIN CERTIFICATE-----/" "{*}" >/dev/null && \\
    for f in /tmp/sysca-*.pem; do \\
        keytool -importcert -noprompt -trustcacerts -cacerts -storepass changeit \\
            -alias "$(basename "$f" .pem)" -file "$f" >/dev/null 2>&1 || true; \\
    done && \\
    rm -f /tmp/sysca-*.pem && \\
    keytool -list -cacerts -storepass changeit | grep -c trustedCertEntry

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""

_PRUNE_BLOCK = r"""RUN set -eux; \
    git checkout --detach "__BASE_SHA__"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --quiet; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
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
            git gc --prune=now --quiet; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""

_TESTCASE_RE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (\S.*)$")


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__GRADLE_TASK__", _GRADLE_TASK)
        .replace("__GRADLE_FLAGS__", _GRADLE_FLAGS)
    )


class JavaJwt788To788ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _JDK_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        template = _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency())
        return (
            template.replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class JavaJwt788To788ImageDefault(Image):
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
        return JavaJwt788To788ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "emit_results.py", _EMIT_RESULTS_PY),
            File(".", "init.gradle", _INIT_GRADLE),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
        ]

    def graded_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, self.pr)),
            File(".", "run.sh", _RUN_SH),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, self.pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, self.pr)),
        ]

    def files(self) -> list[File]:
        return self.prepare_files() + self.graded_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        prepare_copy = "".join(f"COPY {f.name} /home/\n" for f in self.prepare_files())
        graded_copy = "".join(f"COPY {f.name} /home/\n" for f in self.graded_files())

        sections = [f"FROM {image.image_name()}:{image.image_tag()}"]
        for part in (
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            prepare_copy,
            "RUN bash /home/prepare.sh",
            graded_copy,
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            _render(_PRUNE_BLOCK, self.pr),
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("auth0", "java_jwt_788_to_788")
class JAVA_JWT_788_TO_788(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JavaJwt788To788ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {"PASSED": passed, "FAILED": failed, "SKIPPED": skipped}

        for line in test_log.replace("\r", "").split("\n"):
            match = _TESTCASE_RE.match(line.rstrip())
            if match:
                buckets[match.group(1)].add(match.group(2))

        passed -= failed
        skipped -= failed | passed
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


Instance.register("auth0", "java-jwt")(JAVA_JWT_788_TO_788)
