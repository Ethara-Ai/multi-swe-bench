import html
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "apple"
_REPO = "pkl"
_INTERVAL = "pkl_1698_to_1698"

# Toolchain pins, read at base sha a90eb74c2de63f5cb1a16f847f91c228eb9a9141:
#   .java-version                                   -> 21 (IDE hint only)
#   build-logic/src/main/kotlin/BuildInfo.kt        -> PKL_JDK_VERSION_MIN = 25,
#                                                      jdkVendor = ADOPTIUM
#   .github/jobs/GradleJob.pkl                      -> actions/setup-java java-version "25"
#   gradle/wrapper/gradle-wrapper.properties        -> gradle-9.6.1 (sha256 pinned)
# Temurin 25.0.4+7 is the Adoptium JDK 25 GA build current at the base commit
# (2026-08-25); the official image is multi-arch (amd64 + arm64). Pulled through
# AWS's public mirror of the Docker official images (same manifests/digests as
# docker.io/library/eclipse-temurin) to avoid anonymous Docker Hub rate limits.
_JDK_IMAGE = "public.ecr.aws/docker/library/eclipse-temurin:25.0.4_7-jdk-noble"
_BASE_APT = "git ca-certificates curl unzip p11-kit"

# Tests credited by this PR live in pkl-core: the LanguageSnippetTests engine
# (driven by the .pkl/.pcf/.err files the test patch adds under
# pkl-core/src/test/files/LanguageSnippetTests) and EvaluatorTest.kt.
_GRADLE_TARGETS = {
    1698: (
        ":pkl-core:test --rerun "
        "--tests org.pkl.core.LanguageSnippetTests "
        "--tests org.pkl.core.EvaluatorTest"
    ),
}
_GRADLE_WARM = ":pkl-core:testClasses :pkl-core:test --rerun --tests org.pkl.core.NoSuchWarmupTest"

_GRADLE = (
    "./gradlew --no-daemon --no-build-cache --console=plain --max-workers=2 "
    "-I /home/gradle-init.gradle"
)
_TIMEOUT = "timeout -k 60 10800"
_TIMEOUT_PREPARE = "timeout -k 60 14400"

_CHECK_GIT_CHANGES = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

# Applied with `-I` to every graded Gradle invocation. Test failures must not abort
# the build (we parse the JUnit XML instead of the exit code), a filter that matches
# nothing in a given stage (a test class that only exists after test.patch) must not
# fail the task, and test JVMs are capped to keep the shared host responsive.
_GRADLE_INIT = r"""allprojects {
    tasks.withType(Test).configureEach {
        ignoreFailures = true
        filter.failOnNoMatchingTests = false
        maxParallelForks = 1
        reports.html.required = false
    }
}
"""

_APPLY_FN = r"""_apply_patches() {
    local _ex
    _ex="$(awk '/^diff --git /{p=$NF; sub(/^b\//, "", p)} /^Binary files .* differ$/{print "--exclude=" p}' "$@" | sort -u)"
    git apply --whitespace=nowarn $_ex "$@"
}
"""

_RUN_TARGETS = r"""find . -path '*/build/test-results' -type d -prune -exec rm -rf {} +

GRADLE_RC=0
@@TIMEOUT@@ @@GRADLE@@ --offline @@TARGETS@@ || GRADLE_RC=$?

for _x in $(find . -path '*/build/test-results/*' -name 'TEST-*.xml' | sort); do
    echo "=== JUNIT_XML $_x"
    cat "$_x"
    echo
done

if [ "$GRADLE_RC" -ne 0 ]; then
    echo "GRADLE_EXIT_CODE=$GRADLE_RC"
    exit "$GRADLE_RC"
fi
"""

_PREPARE = r"""#!/bin/bash
set -euo pipefail
export CI=true

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd @@REPO_DIR@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION (build time, era-pinned) ----------
# The checked-in wrapper downloads exactly the sha256-pinned Gradle distribution;
# dependency versions come from the commit's own version catalog / lockfiles.
# Compiling the test sources and launching the test task once with a filter that
# matches nothing resolves the full test runtime classpath and builds the test
# fixtures, so the graded runs can execute with --offline.
@@TIMEOUT_PREPARE@@ @@GRADLE@@ @@WARM@@
find . -path '*/build/test-results' -type d -prune -exec rm -rf {} +

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# Offline re-resolution of the test task graph proves every artifact the graded
# runs need is cached and the test sources compile at this commit.
@@TIMEOUT_PREPARE@@ @@GRADLE@@ --offline :pkl-core:testClasses
test "$(git rev-parse HEAD)" = "@@SHA@@"
echo "DEPS_OK"
"""

_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd @@REPO_DIR@@

@@RUN_TARGETS@@
"""

_TEST_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

@@APPLY_FN@@

cd @@REPO_DIR@@
_apply_patches /home/test.patch

@@RUN_TARGETS@@
"""

_FIX_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

@@APPLY_FN@@

cd @@REPO_DIR@@
_apply_patches /home/test.patch /home/fix.patch

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

# The Temurin JDK ships its own cacerts and ignores the system bundle. Rebuild
# it from /etc/ssl/certs/ca-certificates.crt (CA_CERT_PATH, where a build-time
# proxy CA lands) via p11-kit's trust module, so Gradle trusts exactly what
# curl/git trust. Fast and arch-independent (no per-cert keytool loop).
RUN p11-kit extract --format=java-cacerts --purpose=server-auth --overwrite \
        "$JAVA_HOME/lib/security/cacerts" && \
    n="$(keytool -list -cacerts -storepass changeit | grep -c trustedCertEntry)" && \
    echo "JDK cacerts trusted entries: $n" && test "$n" -gt 100

# Gradle daemon / Kotlin daemon heap and worker caps for a shared 8-core host.
# GRADLE_USER_HOME/gradle.properties overrides the project's org.gradle.jvmargs,
# so the project's jdk.compiler exports are carried over verbatim.
ENV GRADLE_USER_HOME=/root/.gradle
RUN mkdir -p /root/.gradle && printf '%s\n' \
    'org.gradle.daemon=false' \
    'org.gradle.workers.max=2' \
    'org.gradle.parallel=false' \
    'org.gradle.jvmargs=-Xmx3g -XX:+UseParallelGC -Dfile.encoding=UTF-8 --add-exports jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.file=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.parser=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED' \
    'kotlin.daemon.jvmargs=-Xmx2g -XX:+UseParallelGC' \
    > /root/.gradle/gradle.properties && \
    java -version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

@@CODE@@
@@CLEAR_ENV@@
CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM @@BASE_IMAGE@@
@@GLOBAL_ENV@@
@@COPY@@

RUN bash /home/prepare.sh

RUN set -eux; \
    cd @@REPO_DIR@@; \
    git checkout --detach "@@SHA@@"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "@@SHA@@"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test -z "$(git reflog)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f @@REPO_DIR@@/.gitmodules ]; then \
        cd @@REPO_DIR@@ && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
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


class Pkl1698To1698ImageBase(Image):
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
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        if self.config.need_clone:
            code = (
                'RUN git clone "${REPO_URL}" /home/%s && \\\n'
                "    cd /home/%s && git rev-parse HEAD >/dev/null"
            ) % (repo, repo)
        else:
            code = "COPY %s /home/%s" % (repo, repo)

        return _render(
            _BASE_DOCKERFILE,
            {
                "IMAGE": self.dependency(),
                "ORG": self.pr.org,
                "REPO": repo,
                "APT": _BASE_APT,
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class Pkl1698To1698ImageDefault(Image):
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
        return Pkl1698To1698ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "GRADLE": _GRADLE,
            "TARGETS": _GRADLE_TARGETS[self.pr.number],
            "WARM": _GRADLE_WARM,
            "TIMEOUT": _TIMEOUT,
            "TIMEOUT_PREPARE": _TIMEOUT_PREPARE,
        }

    def _script(self, body: str) -> str:
        body = _render(body, {"APPLY_FN": _APPLY_FN, "RUN_TARGETS": _RUN_TARGETS})
        return _render(body, self._values())

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "gradle-init.gradle", _GRADLE_INIT),
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


_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TESTCASE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
_ATTR_NAME = re.compile(r'\bname="([^"]*)"')
_ATTR_CLASS = re.compile(r'\bclassname="([^"]*)"')


def parse_gradle_junit_xml(test_log: str) -> TestResult:
    clean = _ANSI.sub("", test_log)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    for m in _TESTCASE.finditer(clean):
        attrs = m.group(1)
        nm = _ATTR_NAME.search(attrs)
        cn = _ATTR_CLASS.search(attrs)
        if not nm or not cn:
            continue
        test_id = "%s#%s" % (html.unescape(cn.group(1)), html.unescape(nm.group(1)))
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
class PKL_1698_TO_1698(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Pkl1698To1698ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_gradle_junit_xml(test_log)
