"""bumptech/glide, PRs 120-1875 - Gradle/Android, one shared base image."""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Carried over from glide_0_to_4999 and REQUIRED for this dataset: the test patches
    for PR 1813 and PR 1875 each carry six .gif fixtures under
    third_party/gif_decoder/src/*/resources/. Collect emits diffs without --binary, so
    those sections have no index line and `git apply` refuses the ENTIRE patch with
    "cannot apply binary patch without full index line" - losing every real test in the
    same file. Stripping the binary sections keeps the .java test sources applying.
    """
    if not patch_content:
        return patch_content

    lines = patch_content.split("\n")
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git"):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith("diff --git"):
                if lines[i].startswith("GIT binary patch") or lines[i].startswith(
                    "Binary files"
                ):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1

    out = "\n".join(result)
    if out and not out.endswith("\n"):
        out += "\n"
    return out


# The five PRs span three build eras and the test task is NOT the same in all of them.
# Measured from each base commit's own build files (2026-09-03):
#
#   pr-120, pr-125   gradle 1.12-all  AGP 0.12.+  compileSdk 19  buildTools 19.1.0
#   pr-311, pr-1813  gradle 2.2-bin   AGP 1.0.0   compileSdk 19  buildTools 19.1.0
#   pr-1875          gradle 3.3-all   AGP 2.3.0   compileSdk 25  buildTools 25.0.2
#
# The first four apply the `robolectric` Gradle plugin and declare their tests with
# `androidTestCompile`, so the plugin's own `test` task is what runs them; they have no
# `testDebugUnitTest` at all. Only 1875 uses AGP's built-in unit tests (`testCompile` +
# `testDebugUnitTest`). Naming a task that does not exist is a CONFIGURATION error, not a
# task failure, so `--continue` does not save it - Gradle aborts before executing
# anything and every stage reports (0, 0, 0). Hence the split.
_AGP_UNIT_TEST_FROM = 1875


def _gradle_bin(pr) -> str:
    """The pre-installed Gradle matching this PR's gradle-wrapper.properties.

    Read from each base commit on 2026-09-03:
        pr-120, pr-125   gradle-1.12-all
        pr-311, pr-1813  gradle-2.2-bin
        pr-1875          gradle-3.3-all

    Invoking these instead of ./gradlew is what keeps the JVM's HTTPS client off the
    critical path - see the install block in _BASE_DOCKERFILE for the failure this
    works around. Same versions, so the builds behave identically.
    """
    n = int(pr.number)
    if n >= 1875:
        return "/opt/gradle/gradle-3.3/bin/gradle"
    if n >= 311:
        return "/opt/gradle/gradle-2.2/bin/gradle"
    return "/opt/gradle/gradle-1.12/bin/gradle"


# Test classes EXCLUDED per PR - identically in all three stages - via the
# mswebExcludeTests property that init.gradle feeds into every Test task. An exclusion
# applied equally to run/test/fix makes the class NONE in every stage: it can neither
# create nor destroy a transition, it only stops broken tests from tripping Rule 2.
#
# GifHeaderParserTest (pr-1813 AND pr-1875): every one of its added tests reads a .gif
# fixture that the dataset's test_patch NAMES but carries ZERO bytes for (collect ran
# without --binary: 0 "GIT binary patch" markers, 0 literal/delta lines). At run time
# TestUtil.resourceToBytes() returns null and the test NPEs - in every stage, forever.
# These tests can never pass and can never carry signal; all they can do is fail in
# ways that differ between stages. Jatin's decision, 2026-09-04: "ignore and avoid
# .gif if it'll result in failure... we can work with 2 n2p if the PR resolves." The
# signal-bearing added tests for BOTH PRs live in GifDrawableTest (mock-based, no
# fixture needed) and are untouched by this.
#
# GifDecoderTest (pr-1875 only): poisons its own JVM through the Robolectric ProxyMaker
# LinkageError on android.content.res.Configuration, and WHICH of its tests absorbs the
# poison moves when the test patch adds methods to the class - that produced the single
# run=PASS -> fix=FAIL (testFirstFrameMustClearBeforeDrawingWhenLastFrameIsDisposal
# Background) that voided the instance on 2026-09-03. forkEvery=1 cannot isolate WITHIN
# a class, and method-level `filter { excludeTestsMatching }` needs Gradle 4.6+ while
# this PR pins 3.3, so class level is the finest granularity available. Cost, stated
# plainly: its three added testTotalIterationCount* tests PASSED at fix and would have
# been n2p - this trades a potential n2p=5 down to n2p=2 to remove the Rule 2
# violation. To revert either entry, delete it here; nothing else depends on this dict.
_EXCLUDED_TESTS = {
    1813: ["**/GifHeaderParserTest*"],
    1875: ["**/GifDecoderTest*", "**/GifHeaderParserTest*"],
}

# Pre-existing test FILES excluded from COMPILATION per PR (mswebExcludeCompile ->
# tasks.withType(JavaCompile) in init.gradle), identically in all three stages.
#
# pr-120/pr-125 (June 2014) compiled against a MOVING 2.4-SNAPSHOT whose API differs
# from the 2.4 final we substitute (the snapshot is unobtainable - OSSRH is dead).
# Measured 2026-09-04: GlideTest.java:732 uses `@Implements(resetStaticState = true)`,
# an attribute the November 2.4 release does not have, and that single uncompilable
# file failed :library:compileTestDebugJava and silenced EVERY :library test - the
# 56/56/56 signature again, one cause deeper. GlideTest is untouched by either PR's
# patches and compiles in NO stage, so excluding it changes no comparable outcome; it
# only unblocks the rest of the suite (both PRs' own signal tests live elsewhere).
# 311/1813 pin the 2.4 RELEASE from the repo's own gradle.properties (their commits
# postdate upstream's swap, commit 705fe5a9), so they are not exposed.
# If more snapshot-era mismatches surface, they get added here file-by-file, on
# evidence - never wholesale.
_EXCLUDED_COMPILE = {
    120: ["**/GlideTest.java"],
    125: ["**/GlideTest.java"],
}


def _test_cmd(pr) -> str:
    n = int(pr.number)
    if n >= _AGP_UNIT_TEST_FROM:
        # AGP 2.3 registers its own unit-test tasks. -PmswebForkEvery=1 gives each test
        # class a fresh JVM; only this PR needs it (Robolectric cascade: 570 failures
        # -> 8 when it was introduced) - see the init.gradle comment in _BASE_DOCKERFILE.
        cmd = f"{_gradle_bin(pr)} test testDebugUnitTest --continue -PmswebForkEvery=1"
    else:
        # robolectric-gradle-plugin era (0.11-0.14.x). Verified against RobolectricPlugin
        # .groovy: the plugin creates `test` as a **TestReport** - an HTML aggregator that
        # executes NOTHING - and creates the real `Test` task as `test<Flavor><BuildType>`,
        # i.e. `testDebug` for a module with no flavors. The release variant is explicitly
        # skipped by the plugin, so `testRelease` does not exist in this era.
        #
        # This is why pr-311 and pr-1813 reported an identical (56, 0, 0) in all three
        # stages on 2026-09-03: `./gradlew test` ran :glide:test and :testutil:test (plain
        # java modules, whose `test` IS a Test task) while :library - which holds every test
        # the patches touch, under src/androidTest/ - ran nothing at all. The test patch
        # therefore changed no observable outcome and no transition could be detected.
        #
        # `test` is KEPT alongside testDebug because the plain-java modules genuinely need
        # it; dropping it would narrow the collection.
        cmd = f"{_gradle_bin(pr)} test testDebug --continue"

    excludes = _EXCLUDED_TESTS.get(n)
    if excludes:
        # Single-quoted so bash never globs the ant patterns against the work tree.
        cmd += " -PmswebExcludeTests='" + ",".join(excludes) + "'"
    compile_excludes = _EXCLUDED_COMPILE.get(n)
    if compile_excludes:
        cmd += " -PmswebExcludeCompile='" + ",".join(compile_excludes) + "'"
    return cmd


_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6
FROM __FROM__
__GLOBAL_ENV__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
# Passed as a build arg by build_dataset for every str-dependency image. Declared so
# docker does not warn about an unconsumed arg, and deliberately NOT used: this base is
# shared by all five PRs and must not be pinned to any one commit.
ARG BASE_COMMIT

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk

WORKDIR /home/

# Port-80 HTTP from containers is broken on this network: the stock-mirror apt step
# failed identically twice, 30 minutes apart, across a full Docker/laptop restart -
# while host HTTP is fine, container tcp:80 CONNECTS fine, and container HTTPS moved
# hundreds of MB (gradle zips, maven deps) all day. That signature is a filtering
# middlebox eating HTTP transfers, not a flaky link, so apt is moved to the HTTPS
# aliyun mirror this network demonstrably serves. ubuntu:22.04 ships no CA store, so
# ca-certificates is bootstrapped with peer verification off FOR THAT ONE CALL - apt's
# GPG check on the signed indexes still authenticates every byte - then every later
# apt call, including the big JDK install below, runs fully verified over HTTPS.
RUN sed -i 's|http://archive.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g; s|http://security.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g; s|http://ports.ubuntu.com/ubuntu-ports|https://mirrors.aliyun.com/ubuntu-ports|g' /etc/apt/sources.list && \\
    apt-get -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false -o Acquire::Retries=5 update && \\
    apt-get -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false -o Acquire::Retries=5 install -y ca-certificates

# Acquire::Retries rides out the intermittent multi-minute network bursts this host
# has shown (Java HTTPS at 18:26, apt HTTP at 21:06 - healthy in between, host-level
# connectivity fine throughout). A failed RUN is still cheap to retry: every layer
# that completed is cached, so a re-run resumes at the failed step.
RUN apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y \\
    git \\
    ca-certificates \\
    openjdk-8-jdk \\
    openjdk-11-jdk \\
    wget \\
    unzip \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8 && \\
    ln -sf /usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-11
ENV JAVA_HOME=/usr/lib/jvm/java-8

RUN mkdir -p ${ANDROID_HOME}/cmdline-tools && \\
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip -O /tmp/cmdline-tools.zip && \\
    unzip -q /tmp/cmdline-tools.zip -d ${ANDROID_HOME}/cmdline-tools && \\
    mv ${ANDROID_HOME}/cmdline-tools/cmdline-tools ${ANDROID_HOME}/cmdline-tools/latest && \\
    rm /tmp/cmdline-tools.zip

ENV PATH=${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${PATH}

# On arm64 the emulator package is x86-only; a stub package.xml stops sdkmanager
# failing while trying to resolve it.
RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        mkdir -p ${ANDROID_HOME}/emulator && \\
        echo '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' > ${ANDROID_HOME}/emulator/package.xml && \\
        echo '<ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02" xmlns:ns3="http://schemas.android.com/repository/android/common/01">' >> ${ANDROID_HOME}/emulator/package.xml && \\
        echo '  <localPackage path="emulator"><type-details xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="ns2:genericDetailsType"/>' >> ${ANDROID_HOME}/emulator/package.xml && \\
        echo '  <revision><major>31</major><minor>3</minor><micro>14</micro></revision>' >> ${ANDROID_HOME}/emulator/package.xml && \\
        echo '  <display-name>Android Emulator (fake for arm64)</display-name></localPackage>' >> ${ANDROID_HOME}/emulator/package.xml && \\
        echo '</ns2:repository>' >> ${ANDROID_HOME}/emulator/package.xml; \\
    fi

ENV JAVA_HOME=/usr/lib/jvm/java-11

# `env -u` the proxy variables: the enhancer's ENV block defaults HTTP_PROXY/HTTPS_PROXY
# to "", and sdkmanager reads set-but-empty as "a proxy is configured", calls
# new URL("") and dies with MalformedURLException. Everything else treats empty as
# no-proxy, so this is specific to the Android tooling.
RUN unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy && \\
    mkdir -p ${ANDROID_HOME}/licenses && \\
    ( echo -e '\\n24333f8a63b6825ea9c5514f83c2829b004d1fee' > ${ANDROID_HOME}/licenses/android-sdk-license ) && \\
    ( echo -e '\\n84831b9409646a918e30573bab4c9c91346d8abd' > ${ANDROID_HOME}/licenses/android-sdk-preview-license ) && \\
    ( yes | sdkmanager "platforms;android-19" "platforms;android-25" "platforms;android-26" "platforms;android-27" "platforms;android-28" "platforms;android-29" "platforms;android-30" "build-tools;19.1.0" "build-tools;25.0.2" "build-tools;26.0.3" "build-tools;27.0.3" "build-tools;28.0.3" "build-tools;29.0.3" "build-tools;30.0.3" "platform-tools" "extras;android;m2repository" || true )

# android-19 / build-tools 19.1.0 are what pr-120, pr-125, pr-311 and pr-1813 compile
# against; without them those four abort during AGP configuration and every stage reports
# (0, 0, 0). They are also old enough that a current sdkmanager may refuse to serve them,
# so print what actually landed. NOT a hard gate: pr-1875 only needs android-25 and must
# still build if the ancient packages are unavailable. Read this list in the build log -
# if android-19 is missing here, the four older PRs are an environment rejection, not a
# config bug.
RUN echo "=== installed SDK platforms ===" && ls ${ANDROID_HOME}/platforms && \\
    echo "=== installed build-tools ===" && ls ${ANDROID_HOME}/build-tools

RUN set -ex; \\
    case "${TARGETARCH:-amd64}" in \\
      arm64) JDK_URL="https://cdn.azul.com/zulu-embedded/bin/zulu8.33.0.135-jdk1.8.0_192-linux_aarch64.tar.gz" ;; \\
      *)     JDK_URL="https://cdn.azul.com/zulu/bin/zulu8.33.0.1-jdk8.0.192-linux_x64.tar.gz" ;; \\
    esac; \\
    mkdir -p /opt/jdk8; \\
    curl -fsSL "$JDK_URL" -o /tmp/jdk8.tar.gz; \\
    tar -xzf /tmp/jdk8.tar.gz -C /opt/jdk8 --strip-components=1; \\
    rm /tmp/jdk8.tar.gz; \\
    /opt/jdk8/bin/java -version

ENV JAVA_HOME=/opt/jdk8
ENV PATH=/opt/jdk8/bin:${PATH}

# Install the Gradle distributions the wrappers pin, instead of letting ./gradlew fetch
# them at image-build time.
#
# WHY: on 2026-09-03 the wrapper download started failing for every PR with
#   Downloading https://services.gradle.org/distributions/gradle-1.12-all.zip
#   java.net.ConnectException: Connection refused
# while curl, openssl s_client and even a raw Java Socket to that same host:443 all
# succeeded from the same container at the same moment. The failure is specific to the
# JVM's HTTPS client on this network path. It killed five image builds that had built
# fine two hours earlier from identical config, so it is not something the config can be
# written around - the dependency itself has to go.
#
# Fetching with curl here removes the JVM's HTTP stack from the critical path for good,
# and saves every PR image an ~80MB download on every rebuild. The versions are exactly
# what each era's gradle-wrapper.properties pins (measured from the base commits), so
# invoking these binaries is equivalent to invoking the wrapper:
#   gradle-1.12  pr-120, pr-125     gradle-2.2  pr-311, pr-1813     gradle-3.3  pr-1875
# _gradle_bin() picks the right one per PR.
RUN set -eux; \\
    mkdir -p /opt/gradle; \\
    for SPEC in "1.12:all" "2.2:bin" "3.3:all"; do \\
        V="${SPEC%%:*}"; K="${SPEC##*:}"; \\
        curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors \\
            "https://services.gradle.org/distributions/gradle-${V}-${K}.zip" -o /tmp/g.zip; \\
        unzip -q /tmp/g.zip -d /opt/gradle; \\
        rm -f /tmp/g.zip; \\
        test -x "/opt/gradle/gradle-${V}/bin/gradle"; \\
    done; \\
    ls -1 /opt/gradle

# build-tools 19.1.0 ships 32-BIT binaries. Verified 2026-09-04: aapt's ELF header is
# class-01 (ELF32) and the container has no /lib/ld-linux.so.2, so the kernel cannot
# load it and exec fails with the infamous 'error=2, No such file or directory' - for a
# file that plainly exists. That killed :third_party:gif_decoder:processReleaseResources
# (and with --continue, every :library test task downstream) for all four SDK-19 PRs the
# moment testDebug finally reached real work, collapsing them back to the 56 java-module
# tests. The gate could never catch this: --dry-run resolves the task graph but executes
# nothing. amd64 only - on arm64 the swap block below replaces these tools with native
# aarch64 builds, and jammy carries no i386 packages for arm64 anyway.
RUN if [ "$TARGETARCH" != "arm64" ]; then \\
        dpkg --add-architecture i386 && \\
        apt-get -o Acquire::Retries=5 update && \\
        apt-get -o Acquire::Retries=5 install -y libc6:i386 libstdc++6:i386 zlib1g:i386 && \\
        rm -rf /var/lib/apt/lists/*; \\
    fi

# On arm64 the SDK ships x86_64 build-tools binaries; swap in aarch64 builds.
RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        for BT_PAIR in "25.0.2:25.0.3" "26.0.3:26.1.1" "27.0.3:27.0.11" "28.0.3:28.0.3" "29.0.3:29.0.3" "30.0.3:30.0.3"; do \\
            SDK_VER="${BT_PAIR%%:*}" && \\
            LZHIYONG_VER="${BT_PAIR##*:}" && \\
            ( curl -fsSL "https://github.com/lzhiyong/android-sdk-tools/releases/download/${LZHIYONG_VER}/android-sdk-tools-static-aarch64.zip" -o /tmp/arm64-build-tools.zip || true ) && \\
            ( unzip -q /tmp/arm64-build-tools.zip -d /tmp/arm64-bt || true ) && \\
            for BIN in aapt aapt2 zipalign dexdump split-select; do \\
                if [ -f "/tmp/arm64-bt/$BIN" ]; then \\
                    cp -f "/tmp/arm64-bt/$BIN" "${ANDROID_HOME}/build-tools/$SDK_VER/$BIN" && \\
                    chmod +x "${ANDROID_HOME}/build-tools/$SDK_VER/$BIN"; \\
                fi; \\
            done && \\
            rm -rf /tmp/arm64-build-tools.zip /tmp/arm64-bt; \\
        done; \\
    fi

# maven.google.com is where the Android Gradle plugin and support libraries live; the
# era's own build files predate it being a default repository.
RUN V="${ANDROID_HOME}/extras/android/m2repository/com/android/volley/volley/1.0.0" && \\
    M="https://maven.aliyun.com/repository/public/com/android/volley/volley/1.0.0" && \\
    mkdir -p "$V" && \\
    curl -fsSL "$M/volley-1.0.0.pom" -o "$V/volley-1.0.0.pom" && \\
    curl -fsSL "$M/volley-1.0.0.aar" -o "$V/volley-1.0.0.aar"

# The init script carries four environment repairs, all of them measured on 2026-09-03.
#
# 1. HTTP -> HTTPS for Maven Central. Gradle 1.12's built-in mavenCentral() points at
#    http://repo1.maven.org, which since 2020-01-15 answers 301 -> an HTTPS page that
#    returns 501. Gradle only falls through to the next repository on a 404, so a 501
#    ABORTS resolution outright - this is what killed pr-120 and pr-125. Rewriting the
#    URL in place (rather than removing the repo) was verified by running it on a real
#    Gradle 1.12 / JVM 8.
#
# 2. robolectric 2.4-SNAPSHOT -> 2.4. OSSRH was shut down entirely on 2025-06-30 and
#    both snapshot hosts now 404 at the root, so the coordinate pr-120/pr-125 pin cannot
#    be served by anything. Upstream glide made exactly this swap itself in commit
#    705fe5a9 (2014-11-10, closing issue #249 "Robolectric 2.4 is out"): it changed only
#    ROBOLECTRIC_VERSION and deleted the snapshot repos, touching NO source or test file,
#    and keeping the same plugin and AGP versions. 2.4 was released 2014-11-07, days
#    after these commits. This is a substitution of the released build of the very same
#    version, not a version bump.
#
# 3. Dead hosts the PROJECT declares are rewritten, not just our own list: glide's
#    2014-era build.gradle puts jcenter() and the oss.sonatype.org snapshots repo in
#    buildscript AND subprojects. Bintray/jcenter is shut down (and on Gradle 1.12
#    jcenter() is plain http), OSSRH died 2025-06-30; neither reliably returns the 404
#    that lets Gradle fall through to the next repository. jcenter ->
#    repo1.maven.org (Central absorbed the overlap), oss.sonatype.org -> the live
#    central.sonatype.com snapshots host.
#
# 5. mockwebserver 1.2.x -> 1.5.4, for pr-125 (guarded to 1.2-prefixed requests, so
#    311/1813's own 1.6.0 pin and everything else is untouched). pr-125's fix patch
#    pins `mockwebserver:1.2.+` while its test patch's own new tests call
#    MockResponse.throttleBody(...), which 1.2.1 - the version 1.2.+ resolves to,
#    deterministically, then and now - does not have. The PR's pinned dependency
#    cannot compile the PR's own tests, and all 20 of its added tests live in the two
#    mockwebserver files, so exclusion would leave no signal at all. 1.5.4 is the
#    same 1.x line, released BEFORE this PR merged, and its MockResponse was verified
#    against the actual Central jar to carry throttleBody(int, long, TimeUnit).
#    Upstream's own comment on the pin reads "TODO: increase this" - they later did.
#    DECISION Jatin 2026-09-04: "override and make 125 resolved" - this overrides a
#    version the dataset's fix patch pins, which is why it needed an explicit call.
#
# 4. forkEvery is driven by a project property instead of being hardcoded to 1. It is
#    only needed by pr-1875, whose Robolectric ProxyMaker LinkageError poisons
#    android.content.res.Configuration for the rest of the JVM: with one JVM per test
#    class the cascade fell from 570 failures to 8, and its two n2p candidates went from
#    failing to passing. The other PRs have no such cascade and would pay a 6-9x
#    slowdown for nothing, so they get Gradle's default of 0 (one JVM for all classes).
RUN mkdir -p /root/.gradle && \\
    printf '%s\\n' \\
        'allprojects {' \\
        '    buildscript {' \\
        '        repositories {' \\
        '            maven { url "https://maven.google.com" }' \\
        '            mavenCentral()' \\
        '            maven { url "https://maven.aliyun.com/repository/public" }' \\
        '            maven { url "https://central.sonatype.com/repository/maven-snapshots" }' \\
        '        }' \\
        '    }' \\
        '    repositories {' \\
        '        mavenLocal()' \\
        '        maven { url "https://maven.google.com" }' \\
        '        mavenCentral()' \\
        '        maven { url "https://maven.aliyun.com/repository/public" }' \\
        '        maven { url "https://central.sonatype.com/repository/maven-snapshots" }' \\
        '    }' \\
        '    def mswebPatchRepo = { r ->' \\
        '        if (r instanceof org.gradle.api.artifacts.repositories.MavenArtifactRepository) {' \\
        '            String u = r.url == null ? "" : r.url.toString()' \\
        '            if (u.startsWith("http://repo1.maven.org/") || u.startsWith("http://repo.maven.apache.org/") || u.contains("jcenter.bintray.com")) {' \\
        '                r.url = "https://repo1.maven.org/maven2/"' \\
        '            } else if (u.contains("oss.sonatype.org")) {' \\
        '                r.url = "https://central.sonatype.com/repository/maven-snapshots"' \\
        '            }' \\
        '        }' \\
        '    }' \\
        '    buildscript.repositories.all(mswebPatchRepo)' \\
        '    repositories.all(mswebPatchRepo)' \\
        '    configurations.all {' \\
        '        resolutionStrategy.eachDependency { d ->' \\
        '            if (d.requested.group == "org.robolectric" && d.requested.name == "robolectric" && d.requested.version == "2.4-SNAPSHOT") {' \\
        '                d.useVersion "2.4"' \\
        '            }' \\
        '            if (d.requested.group == "com.squareup.okhttp" && d.requested.name == "mockwebserver" && d.requested.version.startsWith("1.2")) {' \\
        '                d.useVersion "1.5.4"' \\
        '            }' \\
        '        }' \\
        '    }' \\
        '    tasks.withType(JavaCompile) {' \\
        '        if (project.hasProperty("mswebExcludeCompile")) {' \\
        '            project.mswebExcludeCompile.split(",").each { p -> exclude p }' \\
        '        }' \\
        '    }' \\
        '    tasks.withType(Test) {' \\
        '        forkEvery = (project.hasProperty("mswebForkEvery") ? project.mswebForkEvery.toInteger() : 0)' \\
        '        if (project.hasProperty("mswebExcludeTests")) {' \\
        '            project.mswebExcludeTests.split(",").each { p -> exclude p }' \\
        '        }' \\
        '        testLogging {' \\
        '            events "passed", "failed", "skipped"' \\
        '            exceptionFormat "full"' \\
        '            showExceptions true' \\
        '            showCauses true' \\
        '            showStackTraces true' \\
        '        }' \\
        '    }' \\
        '}' > /root/.gradle/init.gradle

# Rule 1: the base ends here. Unpinned clone, no checkout, no scrub - those belong to
# the PR layer, which is the only layer that knows which commit it is for.
RUN git clone "${REPO_URL}" /home/__REPO__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


class Glide120To1875ImageBase(Image):
    """The ONE base image for this dataset - tag "base" per Ayush's rule 4."""

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

        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        return (
            _BASE_DOCKERFILE.replace("__FROM__", image_name)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__ORG__", org)
            .replace("__REPO__", repo)
        )


class Glide120To1875ImageDefault(Image):
    """The per-PR layer: pin, harden, stage patches and scripts, warm the cache."""

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
        return Glide120To1875ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = _safe_path_component(self.pr.repo)

        return [
            File(".", "fix.patch", f"{_filter_binary_patches(self.pr.fix_patch)}"),
            File(".", "test.patch", f"{_filter_binary_patches(self.pr.test_patch)}"),
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
                """#!/bin/bash
set -e

# No checkout and no scrub here: Ayush's rule 2 puts those in the PR Dockerfile, which
# has already pinned and pruned the tree by the time this runs.
cd /home/{repo}
bash /home/check_git_changes.sh

# HARD GATE - checklist rules 1 and 8. --dry-run configures the whole build and resolves
# the task graph for the EXACT command the three graded stages will run, without
# executing a single test. It fails if the Gradle wrapper cannot download, if AGP or any
# dependency cannot resolve, if the compileSdk platform is missing from the image, or if
# the test task does not exist in this era's build files.
#
# The previous version of this line ended in `|| true`. A total failure therefore still
# produced a "built successfully" image, and only surfaced three stages later as
# (0, 0, 0) with no diagnostic at all - which cost five rebuilds on 2026-09-03. Fail here
# instead, where the error is readable. `tr -cd` strips control bytes so the failure path
# cannot itself die on a cp1252 decode.
if ! {test_cmd} --dry-run > /tmp/configure.log 2>&1; then
    echo "=== GRADLE CONFIGURATION FAILED for: {test_cmd} ==="
    tr -cd '\\11\\12\\15\\40-\\176' < /tmp/configure.log | tail -100
    exit 1
fi
echo "configuration OK: {test_cmd}"

# Warm the dependency and compile caches so the graded stages are not paying for
# resolution. Deliberately NOT fatal: test sources can legitimately fail to compile until
# the fix patch lands, which is exactly what this benchmark measures.
{gradle} testClasses --continue > /tmp/warm.log 2>&1 \\
    || tr -cd '\\11\\12\\15\\40-\\176' < /tmp/warm.log | tail -20
""".format(repo=repo, test_cmd=_test_cmd(self.pr), gradle=_gradle_bin(self.pr)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{test_cmd}

""".format(repo=repo, test_cmd=_test_cmd(self.pr)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}

""".format(repo=repo, test_cmd=_test_cmd(self.pr)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}

""".format(repo=repo, test_cmd=_test_cmd(self.pr)),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)
        sha = _safe_path_component(self.pr.base.sha, "base commit")

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach ${{BASE_COMMIT}}
RUN git submodule update --init --recursive || true

{hardening}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("bumptech", "glide_120_to_1875")
@Instance.register("bumptech", "glide")
class Glide120To1875(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Glide120To1875ImageDefault(self.pr, self._config)

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

        passed_res = [
            re.compile(r"^> Task :(\S+)$"),
            re.compile(r"^> Task :(\S+) UP-TO-DATE$"),
            re.compile(r"^> Task :(\S+) FROM-CACHE$"),
            re.compile(r"^(.+ > .+) PASSED$"),
        ]

        failed_res = [
            re.compile(r"^> Task :(\S+) FAILED$"),
            re.compile(r"^(.+ > .+) FAILED$"),
        ]

        skipped_res = [
            re.compile(r"^> Task :(\S+) SKIPPED$"),
            re.compile(r"^> Task :(\S+) NO-SOURCE$"),
            re.compile(r"^(.+ > .+) SKIPPED$"),
        ]

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for line in clean_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))
                    passed_tests.discard(m.group(1))
                    failed_tests.discard(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
