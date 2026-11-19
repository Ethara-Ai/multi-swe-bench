
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_JDK_IMAGE = "eclipse-temurin:8-jdk"
_BASE_TAG = "base-229_to_229"

_GRADLE_PROJECT_DIR = "solopi-app"
_GRADLE_TASK = ":common:testDebugUnitTest"
_GRADLE_FLAGS = "--no-daemon --console=plain --continue"

_ANDROID_HOME = "/opt/android-sdk"
_GRADLE_HOME = "/home/gradle-home"
_GRADLE_DIST = "/opt/gradle-dist/gradle-bin.zip"

_COMPILE_SDK = "29"
_BUILD_TOOLS = "29.0.2"

_GRADLE_DIST_URL = "https://services.gradle.org/distributions/gradle-6.1.1-bin.zip"
_CMDLINE_TOOLS_URL = (
    "https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip"
)
_AARCH64_TOOLS_URL = (
    "https://github.com/lzhiyong/android-sdk-tools/releases/download/33.0.3/"
    "android-sdk-tools-static-aarch64.zip"
)

_EMULATOR_STUB = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02">'
    '<localPackage path="emulator">'
    '<type-details xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
    ' xsi:type="ns2:genericDetailsType"/>'
    "<revision><major>31</major></revision>"
    "<display-name>Android Emulator</display-name>"
    "</localPackage></ns2:repository>"
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
    repo + "/**/build/test-results/**/TEST-*.xml",
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
    buildscript {
        repositories {
            mavenCentral()
            google()
            gradlePluginPortal()
            maven { url 'https://repo1.maven.org/maven2' }
        }
    }
    repositories {
        mavenCentral()
        google()
        maven { url 'https://repo1.maven.org/maven2' }
        maven { url 'https://jitpack.io' }
    }
}

settingsEvaluated { settings ->
    settings.pluginManagement {
        repositories {
            gradlePluginPortal()
            google()
            mavenCentral()
        }
    }
}
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

# ---------------------------------------------------------------- section 1: pin
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export ANDROID_HOME=__ANDROID_HOME__
export ANDROID_SDK_ROOT=__ANDROID_HOME__
export GRADLE_USER_HOME=__GRADLE_HOME__

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

# ---------------------------------------------------- section 2: provision deps
# Architecture comes from uname, never from TARGETARCH: the harness uses the
# legacy docker builder, where TARGETARCH is empty and would select x86-64.
ARCH=$(uname -m)
DEB_ARCH=$(dpkg --print-architecture)

# sdkmanager needs Java 11+; AGP 4.0.2 needs Java 8. $JAVA_HOME stays on 8 (it is
# what gradle will use) and JDK11 is only ever used to run sdkmanager.
JDK11="/usr/lib/jvm/java-11-openjdk-${DEB_ARCH}"
test -x "$JDK11/bin/java"

if [ ! -f "$ANDROID_HOME/platforms/android-__COMPILE_SDK__/android.jar" ]; then
    mkdir -p "$ANDROID_HOME/cmdline-tools" "$ANDROID_HOME/emulator"
    if [ ! -x "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ]; then
        curl -fsSL --retry 8 --retry-all-errors --retry-delay 5 \\
            --connect-timeout 60 --max-time 1800 \\
            -o /tmp/cmdline-tools.zip "__CMDLINE_TOOLS_URL__"
        unzip -q /tmp/cmdline-tools.zip -d "$ANDROID_HOME/cmdline-tools"
        mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" \\
           "$ANDROID_HOME/cmdline-tools/latest"
        rm -f /tmp/cmdline-tools.zip
    fi

    # Without this stub sdkmanager fails resolving the x86-only emulator package.
    if [ "$ARCH" = "aarch64" ]; then
        printf '%s' '__EMULATOR_STUB__' > "$ANDROID_HOME/emulator/package.xml"
    fi

    # sdkmanager parses HTTP_PROXY/HTTPS_PROXY unconditionally and dies on an
    # EMPTY value with `MalformedURLException: no protocol`. The base image sets
    # all four to "" (the standard no-proxy default), so they are unset for the
    # duration of these two calls -- in a subshell, so the rest of the script and
    # every later stage keep the environment the image declares.
    (
        unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
        yes | env JAVA_HOME="$JDK11" PATH="$JDK11/bin:$PATH" \\
            "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses \\
            >/dev/null 2>&1 || true
        env JAVA_HOME="$JDK11" PATH="$JDK11/bin:$PATH" \\
            "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \\
            "platforms;android-__COMPILE_SDK__" \\
            "build-tools;__BUILD_TOOLS__" \\
            "platform-tools"
    )
fi

# Google's build-tools binaries are x86-64-only; swap in static aarch64 builds so
# resource processing works. `common` has res/, so this is load-bearing.
if [ "$ARCH" = "aarch64" ] && [ ! -f "$ANDROID_HOME/.aarch64-tools-done" ]; then
    curl -fsSL --retry 8 --retry-all-errors --retry-delay 5 \\
        --connect-timeout 60 --max-time 1800 \\
        -o /tmp/bt.zip "__AARCH64_TOOLS_URL__"
    unzip -qo /tmp/bt.zip -d /tmp/bt
    for B in aapt aapt2 aidl zipalign dexdump split-select; do
        # -print -quit rather than `| head -1`: under `set -o pipefail` a SIGPIPE
        # on find would fail the whole script.
        SRC=$(find /tmp/bt -type f -name "$B" -print -quit)
        if [ -n "$SRC" ] && [ -d "$ANDROID_HOME/build-tools/__BUILD_TOOLS__" ]; then
            install -m755 "$SRC" \\
                "$ANDROID_HOME/build-tools/__BUILD_TOOLS__/$B"
        fi
    done
    rm -rf /tmp/bt.zip /tmp/bt
    touch "$ANDROID_HOME/.aarch64-tools-done"
fi

mkdir -p "$GRADLE_USER_HOME"
cp /home/init.gradle "$GRADLE_USER_HOME/init.gradle"

# AGP pulls its own x86-64 aapt2 from Maven and prefers it over the SDK's, which
# fails on arm64 with "AAPT2 ... Daemon startup failed".
if [ "$ARCH" = "aarch64" ]; then
    AAPT2_BIN="$ANDROID_HOME/build-tools/__BUILD_TOOLS__/aapt2"
    if [ -x "$AAPT2_BIN" ]; then
        echo "android.aapt2FromMavenOverride=$AAPT2_BIN" \\
            >> "$GRADLE_USER_HOME/gradle.properties"
    fi
fi

cd /home/__REPO__/__GRADLE_PROJECT_DIR__
chmod +x gradlew
# The wrapper's own downloader is unreliable against the services.gradle.org CDN
# redirect, so it is pointed at the distribution already present in the image.
sed -i "s|^distributionUrl=.*|distributionUrl=file:__GRADLE_DIST__|" \\
    gradle/wrapper/gradle-wrapper.properties

# Warm the dependency cache and the Android build chain. Tolerated because the
# gate below re-checks everything this was supposed to accomplish.
./gradlew __GRADLE_TASK__ __GRADLE_FLAGS__ || true
python3 /home/emit_results.py /home/__REPO__ >/dev/null || true

# ------------------------------------------------------------- section 3: gate
# Restore the tree the PR layer's prune block will assert on. No clean-tree
# assertion belongs here -- provisioning is allowed to have dirtied the worktree.
cd /home/__REPO__
git checkout -- __GRADLE_PROJECT_DIR__/gradle/wrapper/gradle-wrapper.properties
git reset --hard
git clean -fdx

# Hard gate: every one of these is a thing provisioning had to achieve, and none
# of it is tolerated. A half-provisioned image fails here instead of producing
# three identically-empty stages later.
test -x "$JAVA_HOME/bin/javac"
test -f "$ANDROID_HOME/platforms/android-__COMPILE_SDK__/android.jar"
test -x "$ANDROID_HOME/build-tools/__BUILD_TOOLS__/aapt2"
test -s "__GRADLE_DIST__"
test -f "$GRADLE_USER_HOME/init.gradle"
"$ANDROID_HOME/build-tools/__BUILD_TOOLS__/aapt2" version >/dev/null

# The deepest gate: the warm-up above must have actually resolved the unit-test
# classpath, not merely configured the project. junit is what the gold test
# compiles against, so its absence from the cache means the graded stages would
# all fail identically for a provisioning reason rather than a code reason.
test -n "$(find "$GRADLE_USER_HOME/caches" -name 'junit-4.13.2.jar' -print -quit 2>/dev/null)"
test -n "$(find "$GRADLE_USER_HOME/caches" -name 'gradle-4.0.2.jar' -print -quit 2>/dev/null)"
echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export ANDROID_HOME=__ANDROID_HOME__
export ANDROID_SDK_ROOT=__ANDROID_HOME__
export GRADLE_USER_HOME=__GRADLE_HOME__

cd /home/__REPO__
find . -type d -name test-results -prune -exec rm -rf {} + 2>/dev/null || true

cd /home/__REPO__/__GRADLE_PROJECT_DIR__
chmod +x gradlew
sed -i "s|^distributionUrl=.*|distributionUrl=file:__GRADLE_DIST__|" \\
    gradle/wrapper/gradle-wrapper.properties

# Gradle exits non-zero whenever a test fails, and the test stage legitimately
# fails to COMPILE (the gold test references production code the fix adds), so
# the exit code is captured rather than allowed to abort the script. It is not
# discarded: a run that produced no results at all is reported on stderr below,
# and emit_results.py is the single source of truth for what actually ran.
set +e
./gradlew __GRADLE_TASK__ __GRADLE_FLAGS__
GRADLE_RC=$?
set -e

cd /home/__REPO__
git checkout -- __GRADLE_PROJECT_DIR__/gradle/wrapper/gradle-wrapper.properties

if [ "$GRADLE_RC" -ne 0 ] && \\
   [ -z "$(find . -type d -name test-results -print -quit)" ]; then
    echo "run_tests: gradle exited $GRADLE_RC with no test results produced" >&2
fi

python3 /home/emit_results.py /home/__REPO__
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT
ARG GRADLE_DIST_URL="__GRADLE_DIST_URL__"

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

ENV ANDROID_HOME=__ANDROID_HOME__ \\
    ANDROID_SDK_ROOT=__ANDROID_HOME__ \\
    GRADLE_USER_HOME=__GRADLE_HOME__ \\
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

RUN APT="apt-get install -y --no-install-recommends ca-certificates curl git python3 unzip openjdk-11-jdk-headless" && \\
    apt-get update && ($APT || (sleep 5 && apt-get update && $APT)) && \\
    rm -rf /var/lib/apt/lists/* && \\
    test -x "/usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture)/bin/java"

RUN mkdir -p /opt/gradle-dist && \\
    curl -fsSL --retry 8 --retry-all-errors --retry-delay 5 --connect-timeout 60 --max-time 1800 \\
        -o __GRADLE_DIST__ "${GRADLE_DIST_URL}" && \\
    test -s __GRADLE_DIST__

RUN update-ca-certificates && \\
    csplit -z -f /tmp/sysca- -b "%03d.pem" "${CA_CERT_PATH}" "/-----BEGIN CERTIFICATE-----/" "{*}" >/dev/null && \\
    for f in /tmp/sysca-*.pem; do \\
        keytool -importcert -noprompt -trustcacerts \\
            -keystore "${JAVA_HOME}/jre/lib/security/cacerts" -storepass changeit \\
            -alias "$(basename "$f" .pem)" -file "$f" >/dev/null 2>&1 || true; \\
    done && \\
    rm -f /tmp/sysca-*.pem

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""

_PRUNE_BLOCK = r"""RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
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
            git gc --prune=now --quiet; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""

_TESTCASE_RE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (\S.*)$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__GRADLE_PROJECT_DIR__", _GRADLE_PROJECT_DIR)
        .replace("__GRADLE_TASK__", _GRADLE_TASK)
        .replace("__GRADLE_FLAGS__", _GRADLE_FLAGS)
        .replace("__ANDROID_HOME__", _ANDROID_HOME)
        .replace("__GRADLE_HOME__", _GRADLE_HOME)
        .replace("__GRADLE_DIST_URL__", _GRADLE_DIST_URL)
        .replace("__GRADLE_DIST__", _GRADLE_DIST)
        .replace("__CMDLINE_TOOLS_URL__", _CMDLINE_TOOLS_URL)
        .replace("__AARCH64_TOOLS_URL__", _AARCH64_TOOLS_URL)
        .replace("__EMULATOR_STUB__", _EMULATOR_STUB)
        .replace("__COMPILE_SDK__", _COMPILE_SDK)
        .replace("__BUILD_TOOLS__", _BUILD_TOOLS)
    )


class SoloPi229To229ImageBase(Image):
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
        return _render(
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency()), self.pr
        )


class SoloPi229To229ImageDefault(Image):
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
        return SoloPi229To229ImageBase(self.pr, self._config)

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

        sections = [
            f"FROM {image.image_name()}:{image.image_tag()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
        ]
        for part in (
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            prepare_copy,
            "RUN bash /home/prepare.sh",
            graded_copy,
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            _PRUNE_BLOCK,
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("alipay", "SoloPi_229_to_229")
class SOLOPI_229_TO_229(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SoloPi229To229ImageDefault(self.pr, self._config)

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

        clean = _ANSI_RE.sub("", test_log.replace("\r", ""))
        for line in clean.split("\n"):
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


Instance.register("alipay", "SoloPi")(SOLOPI_229_TO_229)
