import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_JDK_IMAGE = "maven:3.8.8-eclipse-temurin-8"
_BASE_TAG = "base-10386_to_10386"

_NON_MODULE_DIRS = frozenset(
    {
        ".github",
        ".git",
        ".gitignore",
        ".gitattributes",
        ".mvn",
        "docs",
        "style",
        "bin",
        "conf",
    }
)

_NO_TEST_MODULES = frozenset({"distribution", "example", "test"})

_SKIP_FLAGS = (
    "-Drat.skip=true -Dcheckstyle.skip=true -Dcheckstyle_unix.skip=true "
    "-Djacoco.skip=true -Denforcer.skip=true -Dmaven.javadoc.skip=true "
    "-Dclirr.skip=true -Dversions.skip=true -Dspotbugs.skip=true "
    "-Dlicense.skip=true -Dgpg.skip=true"
)

_SUREFIRE_FLAGS = (
    "-Dsurefire.skipAfterFailureCount=0 -Dsurefire.useFile=false "
    "-Dmaven.test.skip=false -DfailIfNoTests=false"
)

_M2_SETTINGS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">
  <mirrors>
    <mirror>
      <id>mswb-central-primary</id>
      <name>Maven Central via GCS mirror</name>
      <url>https://maven-central.storage-download.googleapis.com/maven2</url>
      <mirrorOf>central</mirrorOf>
    </mirror>
  </mirrors>
  <profiles>
    <profile>
      <id>mswb-retry</id>
      <properties>
        <maven.wagon.http.retryHandler.count>10</maven.wagon.http.retryHandler.count>
        <maven.wagon.http.retryHandler.requestSentEnabled>true</maven.wagon.http.retryHandler.requestSentEnabled>
        <maven.wagon.httpconnectionManager.ttlSeconds>120</maven.wagon.httpconnectionManager.ttlSeconds>
        <maven.wagon.http.pool>false</maven.wagon.http.pool>
      </properties>
    </profile>
  </profiles>
  <activeProfiles>
    <activeProfile>mswb-retry</activeProfile>
  </activeProfiles>
</settings>
"""

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
    repo + "/**/target/surefire-reports/TEST-*.xml",
    repo + "/**/target/failsafe-reports/TEST-*.xml",
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

_RESOLVE_MODULES_SH = """export PL_FLAG=""
_MODULES="__MODULES__"
if [ -n "$_MODULES" ]; then
    _present=""
    for _m in $(echo "$_MODULES" | tr ',' ' '); do
        if [ -f "/home/__REPO__/$_m/pom.xml" ]; then
            if [ -z "$_present" ]; then
                _present="$_m"
            else
                _present="$_present,$_m"
            fi
        fi
    done
    if [ -n "$_present" ]; then
        PL_FLAG="-pl $_present -am"
    fi
fi
echo "module selector: $PL_FLAG"
"""

_PREPARE_SH = """#!/bin/bash
set -e

export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

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

mkdir -p "$HOME/.m2"
cp /home/settings.xml "$HOME/.m2/settings.xml"

source /home/resolve_modules.sh

mvn -B -ntp dependency:go-offline -DskipTests __SKIP_FLAGS__ $PL_FLAG || true
find "$HOME/.m2/repository" -name "*.lastUpdated" -delete 2>/dev/null || true
mvn -B -ntp install -DskipTests -U __SKIP_FLAGS__ $PL_FLAG || true
find "$HOME/.m2/repository" -name "*.lastUpdated" -delete 2>/dev/null || true
mvn -B -ntp test -fn -U __SUREFIRE_FLAGS__ __SKIP_FLAGS__ $PL_FLAG || true
python3 /home/emit_results.py /home/__REPO__ >/dev/null || true
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/__REPO__
find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null || true

source /home/resolve_modules.sh

find "$HOME/.m2/repository" -name "*.lastUpdated" -delete 2>/dev/null || true

mvn -B -ntp test -fn -U __SUREFIRE_FLAGS__ __SKIP_FLAGS__ $PL_FLAG || true

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__

WORKDIR /home/__REPO__

CMD ["/bin/bash"]
"""

_PRUNE_BLOCK = r"""RUN git reset --hard
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
"""

_TESTCASE_RE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (\S.*)$")


def _diff_paths(patch: str):
    for line in (patch or "").split("\n"):
        if not line.startswith("diff --git "):
            continue
        match = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if not match:
            continue
        yield match.group(1)


def _extract_modules(patch: str) -> set[str]:
    modules: set[str] = set()
    for path in _diff_paths(patch):
        segments = path.split("/")
        if len(segments) < 2 or segments[0] in _NON_MODULE_DIRS:
            continue
        modules.add(segments[0])
    return modules


def _build_modules(pr: PullRequest) -> str:
    modules = _extract_modules(pr.fix_patch) | _extract_modules(pr.test_patch)
    modules.discard("")
    modules = (modules - _NO_TEST_MODULES) or modules
    return ",".join(sorted(modules))


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__SUREFIRE_FLAGS__", _SUREFIRE_FLAGS)
        .replace("__SKIP_FLAGS__", _SKIP_FLAGS)
        .replace("__MODULES__", _build_modules(pr))
    )


class Rocketmq10386To10386ImageBase(Image):
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
        return template.replace("__ORG__", self.pr.org).replace(
            "__REPO__", self.pr.repo
        )


class Rocketmq10386To10386ImageDefault(Image):
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
        return Rocketmq10386To10386ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "emit_results.py", _EMIT_RESULTS_PY),
            File(".", "settings.xml", _M2_SETTINGS_XML),
            File(".", "resolve_modules.sh", _render(_RESOLVE_MODULES_SH, self.pr)),
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
            self.global_env,
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            f"WORKDIR /home/{self.pr.repo}",
            prepare_copy,
            "RUN bash /home/prepare.sh",
            graded_copy,
            _render(_PRUNE_BLOCK, self.pr),
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("apache", "rocketmq_10386_to_10386")
class ROCKETMQ_10386_TO_10386(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Rocketmq10386To10386ImageDefault(self.pr, self._config)

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


Instance.register("apache", "rocketmq")(ROCKETMQ_10386_TO_10386)
