"""TeamNewPipe/NewPipe -- JDK 8 era (PRs 1339, 3278, 3294, 6319).

Era boundary, measured at each PR's own base commit rather than guessed:

    PR 1339   2018-04-21  Gradle 4.6      AGP 3.1.1  compileSdk 27  Java 1.8
    PR 3278   2020-03-28  Gradle 5.4.1    AGP 3.5.1  compileSdk 28  Java 1.8  Kotlin 1.3.50
    PR 3294   2020-04-08  Gradle 5.4.1    AGP 3.5.1  compileSdk 28  Java 1.8  Kotlin 1.3.50
    PR 6319   2021-05-17  Gradle 6.8.3    AGP 4.1.3  compileSdk 29  Java 1.8  Kotlin 1.4.10
    --------------------------------------------------------------------- split
    PR 12325  2025-05-20  Gradle 8.9      AGP 8.7.1  compileSdk 34  Java 17   Kotlin 1.9.25

Gradle 4.6, 5.4.1 and 6.8.3 cannot run on JDK 17 (their support caps at JDK 10,
12 and 15 respectively), and AGP 3.x/4.x requires JDK 8. So these four PRs need a
JDK 8 toolchain and PR 12325 needs JDK 17 -- they cannot share a base image.

JDK 17 is ALSO installed here, but only so the Android cmdline-tools sdkmanager
can run (it requires JDK 11+). JAVA_HOME points at JDK 8, which is what Gradle
and AGP see.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA_RANGE = "6319-to-1339"



class ImageBase(Image):
    """Toolchain + cloned sources. Nothing after the clone.

    Per the layout rule for this project the base Dockerfile ends at `git clone`
    followed by `CMD`. It carries no `git checkout`, no history strip and no
    hardening -- pinning the tree to the PR's base commit is prepare.sh's job in
    the PR layer, because the commit differs per PR while this image is shared.
    """

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

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_ERA_RANGE}"

    def workdir(self) -> str:
        return f"base-{_ERA_RANGE}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Environment and clone only.

        Emits the BuildKit syntax directive itself, which makes
        DockerfileEnhancer.enhance() return this file untouched (image.py:317).
        That is deliberate -- it is what keeps the injected checkout and history
        scrub out of the base -- and it is why the ARGs, ENV block, OCI labels
        and CA-certificate symlink farm are written here rather than inherited.

        The symlink farm precedes every network RUN, so the first HTTPS call
        already trusts the proxy CA. BASE_COMMIT is declared because the harness
        passes it, but deliberately unused: pinning happens in the PR layer.
        """
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    ANDROID_HOME=/opt/android-sdk \\
    ANDROID_SDK_ROOT=/opt/android-sdk \\
    JAVA_HOME=/usr/lib/jvm/java-8
ENV PATH=${{JAVA_HOME}}/bin:${{ANDROID_HOME}}/cmdline-tools/latest/bin:${{ANDROID_HOME}}/platform-tools:${{PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN APT="apt-get install -y --no-install-recommends git ca-certificates openjdk-8-jdk-headless unzip curl" \
    && apt-get update && ($APT || (sleep 5 && apt-get update && $APT)) \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) ${{JAVA_HOME}}

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


_CHECK_GIT_CHANGES = """#!/bin/bash
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
"""


# The test body is inlined into all THREE stage scripts from this ONE constant so
# run.sh, test-run.sh and fix-run.sh cannot drift apart. That equality is not
# cosmetic: if the stages ran different tests the f2p/p2p comparison between them
# would be meaningless, so it is generated rather than maintained by hand.
#
# `set +e` because from here a failing test is the EXPECTED outcome of at least
# one stage; the wrappers above run with `set -eo pipefail` so that a failed
# `git apply` still aborts.
#
# The NP-TEST-FILES block is what lets parse_log emit "<source file>::<test>".
# It scans EVERY module's src/test, not just app/, so a test class outside app/
# still resolves to a real path instead of falling back to a bare class name.
# Gradle reports tests as "<FQCN> > <method>" and never names the source file, so
# the runner ships the inventory and parse_log resolves the class to its path.
_RUN_TESTS_BODY = r"""set +e
set -uo pipefail

export CI=true
export JAVA_HOME=/usr/lib/jvm/java-8
export GRADLE_USER_HOME=/root/.gradle

# The wrapper cannot download a distribution here (the CDN redirect defeats its
# Java downloader), so use the pre-seeded binary of the exact version this
# commit pins. Same version the wrapper would have fetched, just already local.
GRADLE_VER=$(grep -oE "gradle-[0-9.]+-(all|bin)" gradle/wrapper/gradle-wrapper.properties | head -1 | sed -e "s/^gradle-//" -e "s/-\\(all\\|bin\\)$//")
GRADLE_BIN=/opt/gradle/gradle-$GRADLE_VER/bin/gradle
test -x "$GRADLE_BIN" || { echo "no pre-seeded gradle $GRADLE_VER in /opt/gradle"; ls /opt/gradle; exit 1; }

echo "##### NP-TEST-FILES-BEGIN"
find . -path '*/src/test/*' -type f \( -name '*.java' -o -name '*.kt' \) -not -path '*/build/*' 2>/dev/null | sed 's|^\./||' | sort
echo "##### NP-TEST-FILES-END"

NP_LOG=/tmp/np-gradle.log
"$GRADLE_BIN" --no-daemon testDebugUnitTest --continue 2>&1 | tee "$NP_LOG"
rc=${PIPESTATUS[0]}

# A test patch calls symbols the fix patch introduces, so it cannot compile until
# the fix lands -- and javac/kotlinc compile the whole test source set as a single
# unit, so one unresolved symbol aborts the task and NO test runs. That discards
# the pre-existing tests' results too, which is why the stage reported (0,0,0).
# Move aside only the test files this stage's patch touched and run again, so the
# base-commit tests still execute and are reported. The file list comes from
# git status, so this is inert in the run stage, where nothing is patched.
if [ "$rc" -ne 0 ] && grep -qE "compileDebugUnitTest(JavaWithJavac|Kotlin) FAILED|Execution failed for task '[^']*compileDebugUnitTest[^']*'" "$NP_LOG"; then
  NP_EXCL=""
  for F in $(git status --porcelain --untracked-files=all 2>/dev/null | cut -c4-); do
    case "$F" in
      */src/test/*)
        if [ -f "$F" ]; then mv "$F" "$F.np-excluded"; NP_EXCL="$NP_EXCL $F"; fi
        ;;
    esac
  done
  if [ -n "$NP_EXCL" ]; then
    echo "##### NP-TESTPATCH-UNCOMPILABLE:$NP_EXCL"
    "$GRADLE_BIN" --no-daemon testDebugUnitTest --continue 2>&1 | tee -a "$NP_LOG"
    rc=${PIPESTATUS[0]}
  fi
fi

echo "##### NP-GRADLE-EXIT: $rc"

exit 0
"""


_STAGE_HEADER = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git checkout -- . 2>/dev/null || true

"""


_APPLY_TEST = r"""git apply --whitespace=nowarn /home/test.patch

"""


_APPLY_FIX = r"""git apply --whitespace=nowarn /home/fix.patch

"""


def _prepare_sh(pr: PullRequest) -> str:
    """Dependencies only -- no checkout, no fetch, no hardening.

    By the time this runs the PR Dockerfile has already pinned the tree to the
    base commit and scrubbed the history, so this script only has to make the
    build usable: assert the tree is clean, install what the build needs, prove
    the result actually works, and leave the worktree as it found it.

    The NewPipeExtractor version is pinned per commit in app/build.gradle, so it
    can only be resolved once the tree is at this PR's commit -- which is why the
    dependency step lives here and not in the shared base.
    """
    return "\n".join([
        "#!/bin/bash",
        "set -e",
        "",
        "export JAVA_HOME=/usr/lib/jvm/java-8",
        "export GRADLE_USER_HOME=/root/.gradle",
        "",
        f"cd /home/{pr.repo}",
        "bash /home/check_git_changes.sh",
        "",
        "# The PR layer pinned the tree to this commit and scrubbed the history.",
        "# Re-assert it from here rather than from the Dockerfile: if the pin ever",
        "# slipped, every dependency resolved below would belong to the wrong tree",
        "# and the instance would grade against sources it was never built for.",
        f'test "$(git rev-parse HEAD)" = "{pr.base.sha}"',
        'test -z "$(git remote)"',
        'test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"',
        '',
        '# Android SDK. Gradle will not even configure an Android project without',
        '# it, so this has to exist before anything else runs. TARGETARCH is a build',
        '# ARG and is not available here, so architecture comes from uname.',
        'ARCH=$(uname -m)',
        'if [ ! -x "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ]; then',
        '  mkdir -p "$ANDROID_HOME/cmdline-tools" "$ANDROID_HOME/emulator"',
        '  curl -fsSLo /tmp/clt.zip https://dl.google.com/android/repository/commandlinetools-linux-6858069_latest.zip',
        '  unzip -q /tmp/clt.zip -d "$ANDROID_HOME/cmdline-tools"',
        '  mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"',
        '  rm /tmp/clt.zip',
        '  # sdkmanager refuses to run on arm64 while resolving the x86-only emulator',
        '  # package, so a stub package.xml satisfies it.',
        '  if [ "$ARCH" = "aarch64" ]; then',
        '    printf \'%s\' \'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02"><localPackage path="emulator"><type-details xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="ns2:genericDetailsType"/><revision><major>31</major></revision><display-name>Android Emulator</display-name></localPackage></ns2:repository>\' > "$ANDROID_HOME/emulator/package.xml"',
        '  fi',
        '  (unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; \\',
        '     yes | sdkmanager --licenses > /dev/null 2>&1; \\',
        '     sdkmanager "platform-tools" "platforms;android-27" "platforms;android-28" "platforms;android-29" "platforms;android-30" "build-tools;27.0.3" "build-tools;28.0.3" "build-tools;29.0.3" "build-tools;30.0.3") || true',
        '  # Google ships aapt/aapt2/zipalign/dexdump as x86_64-only in every',
        '  # build-tools release, so on arm64 they are replaced with aarch64 builds.',
        '  if [ "$ARCH" = "aarch64" ]; then',
        '    curl -fsSL "https://github.com/lzhiyong/android-sdk-tools/releases/download/33.0.3/android-sdk-tools-static-aarch64.zip" -o /tmp/bt.zip \\',
        '      && unzip -qo /tmp/bt.zip -d /tmp/bt \\',
        '      && for V in 27.0.3 28.0.3 29.0.3 30.0.3; do for B in aapt aapt2 aidl zipalign dexdump split-select; do \\',
        '           BT_SRC=$(find /tmp/bt -type f -name "$B" | head -1); \\\n           [ -n "$BT_SRC" ] && [ -d "$ANDROID_HOME/build-tools/$V" ] && install -m755 "$BT_SRC" "$ANDROID_HOME/build-tools/$V/$B"; \\',
        '         done; done; rm -rf /tmp/bt.zip /tmp/bt',
        '  fi',
        'fi',
        '',
        '# Gradle distributions. The wrapper cannot fetch these itself: services.gradle.org',
        '# redirects to a GitHub CDN that defeats its Java downloader (Connection refused on',
        '# Gradle 4.x, timeout on 8.x, every time). curl handles the same URL fine, so the',
        '# exact versions each commit pins are fetched here and invoked directly.',
        'for V in 4.2.1 4.6 5.4.1 5.5.1 6.8.2 6.8.3; do',
        '  [ -d "/opt/gradle/gradle-$V" ] && continue',
        '  mkdir -p /opt/gradle',
        '  curl -fsSL --retry 5 --retry-all-errors --connect-timeout 60 --max-time 900 \\',
        '       -o /tmp/g.zip "https://services.gradle.org/distributions/gradle-$V-bin.zip" \\',
        '    && unzip -q /tmp/g.zip -d /opt/gradle && rm /tmp/g.zip',
        'done',
        '',
        '# Gradle prints no per-test results by default, which leaves parse_log nothing',
        '# to read. upToDateWhen(false) matters too: the warm run below would otherwise',
        '# make the first graded stage UP-TO-DATE and silent.',
        '#',
        '# The Aliyun mirror is here because JCenter shut down in 2021 and its artifacts',
        '# were never migrated: com.xwray:groupie and com.google.android.exoplayer, which',
        '# the 2018-2020 PRs need, now 404 on JCenter, Maven Central, Google Maven and',
        '# JitPack alike. Aliyun still mirrors JCenter and serves them. Adding it here',
        '# keeps the projects under test unmodified -- editing their build.gradle would',
        '# dirty the worktree and trip the clean-tree assert.',
        'mkdir -p /root/.gradle',
        "cat > /root/.gradle/init.gradle <<'GRADLE'",
        'allprojects {',
        '  repositories {',
        '    mavenLocal()',
        '    mavenCentral()',
        '    google()',
        '    maven { url "https://maven.aliyun.com/repository/public" }',
        '    maven { url "https://maven.aliyun.com/repository/jcenter" }',
        '  }',
        '  tasks.withType(Test) {',
        '    outputs.upToDateWhen { false }',
        '    testLogging { events "passed", "failed", "skipped" }',
        '  }',
        '    tasks.matching { it.name.toLowerCase().contains("checkstyle") }.all { it.enabled = false }',
        '}',
        'GRADLE',
        '',
        '# Gradle 6+ forks a single-use daemon even under --no-daemon, and BuildKit',
        '# kills that forked JVM at startup. -XX:-UsePerfData avoids the hsperfdata',
        "# write that aborts it. The project's own jvmargs are carried over verbatim:",
        '# GRADLE_USER_HOME/gradle.properties outranks the project file, so omitting',
        "# them would silently drop upstream's heap setting.",
        "PROJ_JVMARGS=$(grep -h '^org\\.gradle\\.jvmargs' gradle.properties 2>/dev/null | head -1 | sed 's/^[^=]*=//')",
        'echo "org.gradle.jvmargs=${PROJ_JVMARGS} -XX:-UsePerfData" >> /root/.gradle/gradle.properties',
        '',
        '# AGP resolves its own x86_64-only aapt2 from Maven and prefers it over the',
        '# SDK\'s, so arm64 resource merging fails with "AAPT2 ... Daemon startup',
        '# failed". Point AGP at the aarch64 binary installed above instead.',
        'if [ "$ARCH" = "aarch64" ]; then',
        '  AAPT2_BIN=$(ls -d "$ANDROID_HOME"/build-tools/*/aapt2 2>/dev/null | tail -1)',
        '  if [ -n "$AAPT2_BIN" ]; then',
        '    echo "android.aapt2FromMavenOverride=$AAPT2_BIN" >> /root/.gradle/gradle.properties',
        '    echo "prepare: AGP aapt2 override -> $AAPT2_BIN"',
        '  fi',
        'fi',
        '',
        '',
        '',
        '# The wrapper cannot download a distribution here (the CDN redirect defeats its',
        '# Java downloader), so use the pre-seeded binary of the exact version this',
        '# commit pins. Same version the wrapper would have fetched, just already local.',
        'GRADLE_VER=$(grep -oE "gradle-[0-9.]+-(all|bin)" gradle/wrapper/gradle-wrapper.properties | head -1 | sed -e "s/^gradle-//" -e "s/-\\\\(all\\\\|bin\\\\)$//")',
        'GRADLE_BIN=/opt/gradle/gradle-$GRADLE_VER/bin/gradle',
        'test -x "$GRADLE_BIN" || { echo "no pre-seeded gradle $GRADLE_VER in /opt/gradle"; ls /opt/gradle; exit 1; }',
        '',
        "",
        '# JitPack-hosted dependencies that JitPack no longer serves are built from',
        '# source. The coordinate says where the source lives:',
        '#   com.github.<user>:<repo>:<ref>        -> github.com/<user>/<repo>',
        '#   com.github.<user>.<repo>:<repo>:<ref> -> same (multi-module form)',
        '# Only coordinates that actually 404 on JitPack are built, so this costs one',
        '# HEAD request per dependency when everything resolves normally.',
        'for COORD in $(grep -oE "com\\.github\\.[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9._-]+" app/build.gradle | sort -u); do',
        '  DEP_G=${COORD%%:*}; DEP_REST=${COORD#*:}',
        '  DEP_A=${DEP_REST%%:*}; DEP_V=${DEP_REST##*:}',
        '  DEP_USER=${DEP_G#com.github.}; DEP_USER=${DEP_USER%%.*}',
        '  DEP_DIR=/root/.m2/repository/$(printf %s "$DEP_G" | tr . /)/$DEP_A/$DEP_V',
        '  [ -f "$DEP_DIR/$DEP_A-$DEP_V.pom" ] && continue',
        '  if curl -fsI --max-time 60 "https://jitpack.io/$(printf %s "$DEP_G" | tr . /)/$DEP_A/$DEP_V/$DEP_A-$DEP_V.pom" > /dev/null 2>&1; then',
        '    echo "prepare: $COORD resolves from JitPack"',
        '    continue',
        '  fi',
        '  echo "prepare: $COORD not on JitPack -- building from github.com/$DEP_USER/$DEP_A"',
        '  # Some refs can never come from JitPack (their build errored years ago) yet',
        '  # the library is published on Maven Central under the conventional lowercase',
        '  # artifactId. Taking the release and aliasing it to the requested ref avoids',
        '  # reconstructing an obsolete Android toolchain just to rebuild it.',
        '  DEP_LC=$(printf %s "$DEP_A" | tr "[:upper:]" "[:lower:]")',
        '  DEP_GOT=""',
        '  for DEP_RV in $(curl -fsL --max-time 60 "https://repo1.maven.org/maven2/$(printf %s "$DEP_G" | tr . /)/$DEP_LC/maven-metadata.xml" 2>/dev/null | grep -oE "<version>[^<]+</version>" | sed -e "s/<[^>]*>//g" | tac); do',
        '    DEP_RB="https://repo1.maven.org/maven2/$(printf %s "$DEP_G" | tr . /)/$DEP_LC/$DEP_RV/$DEP_LC-$DEP_RV"',
        '    curl -fsL --max-time 120 -o /tmp/dep.pom "$DEP_RB.pom" 2>/dev/null || continue',
        '    for DEP_EXT in aar jar; do',
        '      curl -fsL --max-time 300 -o /tmp/dep.$DEP_EXT "$DEP_RB.$DEP_EXT" 2>/dev/null || continue',
        '      mkdir -p "$DEP_DIR"',
        '      cp /tmp/dep.pom "$DEP_DIR/$DEP_A-$DEP_V.pom"',
        '      cp /tmp/dep.$DEP_EXT "$DEP_DIR/$DEP_A-$DEP_V.$DEP_EXT"',
        '      sed -i "0,/<version>$DEP_RV<\\/version>/s//<version>$DEP_V<\\/version>/" "$DEP_DIR/$DEP_A-$DEP_V.pom"',
        '      sed -i "s|<artifactId>$DEP_LC</artifactId>|<artifactId>$DEP_A</artifactId>|g" "$DEP_DIR/$DEP_A-$DEP_V.pom"',
        '      sed -i \'s|<scope>runtime</scope>|<scope>compile</scope>|g\' "$DEP_DIR/$DEP_A-$DEP_V.pom"',
        '      echo "prepare: $COORD satisfied by Maven Central $DEP_LC:$DEP_RV ($DEP_EXT)"',
        '      DEP_GOT=1; break',
        '    done',
        '    [ -n "$DEP_GOT" ] && break',
        '  done',
        '  [ -n "$DEP_GOT" ] && continue',
        '',
        '  DEP_SRC=/home/src-$DEP_A',
        '  [ -d "$DEP_SRC/.git" ] || git clone --quiet "https://github.com/$DEP_USER/$DEP_A.git" "$DEP_SRC" || continue',
        '  git -C "$DEP_SRC" switch --detach --quiet "$DEP_V" 2>/dev/null || git -C "$DEP_SRC" checkout -q "$DEP_V" || continue',
        '  cat > /tmp/dep-publish.gradle <<GRADLE',
        'allprojects {',
        '  apply plugin: "maven"',
        '  group = "$DEP_G"',
        '  version = "$DEP_V"',
        '}',
        'GRADLE',
        '  DEP_GV=$(grep -oE "gradle-[0-9.]+-(all|bin)" "$DEP_SRC/gradle/wrapper/gradle-wrapper.properties" 2>/dev/null | head -1 | sed -e "s/^gradle-//" -e "s/-\\(all\\|bin\\)$//")',
        '  DEP_GB=/opt/gradle/gradle-$DEP_GV/bin/gradle',
        '  [ -x "$DEP_GB" ] || DEP_GB="$GRADLE_BIN"',
        '  (cd "$DEP_SRC" && "$DEP_GB" --no-daemon -I /tmp/dep-publish.gradle install) || true',
        "  # `install` publishes under the project's own hardcoded version, not the ref",
        '  # we need, and writes module deps at runtime scope (which hides the classes',
        '  # from the compiler). Alias to the requested coordinate and fix the scope.',
        '  if [ ! -f "$DEP_DIR/$DEP_A-$DEP_V.pom" ]; then',
        '    DEP_PUB=$(ls -d "/root/.m2/repository/$(printf %s "$DEP_G" | tr . /)/$DEP_A"/*/ 2>/dev/null | head -1)',
        '    if [ -n "$DEP_PUB" ]; then',
        '      DEP_SRCV=$(basename "$DEP_PUB")',
        '      echo "prepare: aliasing $DEP_A $DEP_SRCV -> $DEP_V"',
        '      mkdir -p "$DEP_DIR"',
        '      for F in "$DEP_PUB"*; do',
        '        B=$(basename "$F")',
        '        cp "$F" "$DEP_DIR/$(printf %s "$B" | sed "s/$DEP_SRCV/$DEP_V/")"',
        '      done',
        '      sed -i "0,/<version>$DEP_SRCV<\\/version>/s//<version>$DEP_V<\\/version>/" "$DEP_DIR/$DEP_A-$DEP_V.pom"',
        '      sed -i \'s|<scope>runtime</scope>|<scope>compile</scope>|g\' "$DEP_DIR/$DEP_A-$DEP_V.pom"',
        '    fi',
        '  fi',
        '  # A multi-module dependency whose root project does not depend on its own',
        '  # submodules publishes an empty root artifact -- the classes live in the',
        '  # submodule jars and nothing references them. JitPack synthesises an',
        '  # aggregator POM here, so do the same: list every sibling module published',
        '  # alongside it as a compile dependency.',
        '  if [ -f "$DEP_DIR/$DEP_A-$DEP_V.pom" ] && ! grep -q "<dependencies>" "$DEP_DIR/$DEP_A-$DEP_V.pom"; then',
        '    DEP_SIB=""',
        '    for SP in "/root/.m2/repository/$(printf %s "$DEP_G" | tr . /)"/*/; do',
        '      SA=$(basename "$SP")',
        '      [ "$SA" = "$DEP_A" ] && continue',
        '      SVD=$(ls -d "$SP"*/ 2>/dev/null | head -1) || continue',
        '      [ -n "$SVD" ] || continue',
        '      SV=$(basename "$SVD")',
        '      [ -f "$SVD/$SA-$SV.jar" ] || continue',
        '      DEP_SIB="$DEP_SIB<dependency><groupId>$DEP_G</groupId><artifactId>$SA</artifactId><version>$SV</version><scope>compile</scope></dependency>"',
        '    done',
        '    if [ -n "$DEP_SIB" ]; then',
        '      echo "prepare: synthesising aggregator POM for $DEP_A -> $DEP_SIB"',
        '      sed -i "s|</project>|<dependencies>$DEP_SIB</dependencies></project>|" "$DEP_DIR/$DEP_A-$DEP_V.pom"',
        '    fi',
        '  fi',
        'done',
        '',
        '',
        '"$GRADLE_BIN" --no-daemon testDebugUnitTest --continue || true',
        "",
        "# Hard gate. The installs above end in `|| true` so a partial failure does",
        "# not abort mid-way; these lines are what decide the environment is usable,",
        "# and a genuinely missing dependency fails the build loudly right here.",
        "java -version 2>&1 | head -1",
        'test -x "$GRADLE_BIN"',
        "test -d ${ANDROID_HOME}/platform-tools",
        '"$GRADLE_BIN" --no-daemon -q projects > /dev/null',
        '# Compiling main + unit-test sources at the base commit is the invariant that',
        '# matters: it must hold before any patch is applied. No `|| true` here -- a',
        '# missing dependency has to fail the build now, not surface as an empty',
        '# report after the instances run.',
        '"$GRADLE_BIN" --no-daemon :app:compileDebugUnitTestJavaWithJavac',
        "",
        "# Restore the worktree: the build writes into gitignored paths, but a",
        "# dependency step that dirtied a tracked file must fail rather than ship.",
        "git restore --worktree --staged .",
        "bash /home/check_git_changes.sh",
        "",
    ])


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

    def dependency(self) -> "ImageBase":
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_sh(self.pr)),
            File(
                ".",
                "run.sh",
                (_STAGE_HEADER + _RUN_TESTS_BODY).replace("__REPO__", self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                (_STAGE_HEADER + _APPLY_TEST + _RUN_TESTS_BODY).replace(
                    "__REPO__", self.pr.repo
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                (_STAGE_HEADER + _APPLY_TEST + _APPLY_FIX + _RUN_TESTS_BODY).replace(
                    "__REPO__", self.pr.repo
                ),
            ),
        ]

    def dockerfile(self) -> str:
        """COPY, pin to the base commit, scrub history, then install deps.

        This file is never touched by DockerfileEnhancer: enhance() returns the
        raw text whenever dependency() is not a string (image.py:315), and a PR
        image depends on the base Image. The same rule means the PR build gets no
        BASE_COMMIT build-arg, which is why the SHA below is a literal taken from
        self.pr.base.sha rather than ${{BASE_COMMIT}}.

        The four `test` lines are the point of the scrub: HEAD is the base
        commit, no refs survive, no remote survives, and no unreachable history
        survives. Without them a mispinned or leaky image ships silently.
        """
        image = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

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
            git reflog expire --expire=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("TeamNewPipe", "NewPipe_6319_to_1339")
class NewPipeJdk8Era(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)

        # 1. Source-file inventory the runner printed, so an identity can name the
        #    file. Gradle only ever reports "<FQCN> > <method>".
        files: list[str] = []
        capture = False
        for line in clean.splitlines():
            s = line.strip()
            if s == "##### NP-TEST-FILES-BEGIN":
                capture = True
                continue
            if s == "##### NP-TEST-FILES-END":
                capture = False
                continue
            if capture and s:
                files.append(s)

        # 2. FQCN -> path. A test source root is whatever precedes the package
        #    dirs, i.e. ".../java/" or ".../kotlin/"; strip it and the extension,
        #    then swap separators for dots.
        by_class: dict[str, str] = {}
        by_simple: dict[str, str] = {}
        ambiguous: set[str] = set()
        for path in files:
            rel = re.sub(r"^.*?/(?:java|kotlin)/", "", path)
            fqcn = re.sub(r"\.(?:java|kt)$", "", rel).replace("/", ".")
            by_class[fqcn] = path
            # JUnit4 under Gradle 6.x reports the fully-qualified class name, but
            # the JUnit5 platform under Gradle 8.x reports only the simple name
            # ("ExportPlaylistTest > foo"), so an FQCN-keyed index misses every
            # line and the identity degrades to "ClassName::method". Index the
            # simple name too, and refuse it when two packages share one class
            # name -- a wrong file is worse than an unqualified one.
            simple = fqcn.rsplit(".", 1)[-1]
            if simple in by_simple and by_simple[simple] != path:
                ambiguous.add(simple)
            by_simple[simple] = path
        for s in ambiguous:
            by_simple.pop(s, None)

        # 3. Gradle per-test lines, e.g.
        #      Gradle Test Run :app:testDebugUnitTest > Gradle Test Executor 1 \
        #        > org.schabi.newpipe.util.ListHelperTest > testFoo PASSED
        #    With our init.gradle testLogging config Gradle emits the BARE form
        #    "<FQCN> > <method> <STATUS>" with no prefix, so the prefix is optional
        #    here -- requiring it matched nothing and every stage reported (0,0,0).
        #    Only the trailing "<FQCN> > <method> <STATUS>" is taken. Deliberately
        #    NOT matching "> Task :app:foo" lines: those are Gradle build tasks,
        #    not tests, and folding them into these sets would report compilation
        #    steps as passing tests and flood p2p.
        test_re = re.compile(
            r"^(?:.*> )?([\w.$]+) > (.+?) (PASSED|FAILED|SKIPPED)\s*$"
        )

        for line in clean.splitlines():
            m = test_re.search(line.rstrip())
            if not m:
                continue
            fqcn, method, status = m.group(1), m.group(2).strip(), m.group(3)
            # Nested classes report as Outer$Inner; the file is Outer's.
            outer = fqcn.split("$", 1)[0]
            path = (
                by_class.get(fqcn)
                or by_class.get(outer)
                or by_simple.get(outer.rsplit(".", 1)[-1])
            )
            name = f"{path}::{method}" if path else f"{fqcn}::{method}"

            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # TestResult requires the three sets to be pairwise disjoint, and a retried
        # test can appear with two statuses. Failure wins: crediting a test that was
        # ever seen failing as passed is the unsafe direction.
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
