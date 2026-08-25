"""Repo config for hash-checker/hash-checker (Android app, Gradle 7.0.2 / AGP 7.0.3).

What is actually graded
-----------------------
The gold test patch for PR #197 touches exactly one file::

    app/src/androidTest/java/.../calculator/HashCalculatorTaskExceptionTest.java

``androidTest`` is the **instrumentation** source set: the suite runs inside an
Android runtime, and upstream CI runs it on ``macos-latest`` behind
``reactivecircus/android-emulator-runner@v2`` with ``api-level: 27``
(``.github/workflows/build.yml``, job ``intergation_tests``). There is no
emulator in this harness and no way to add one that is reproducible on both
architectures, so the test cannot be *executed* here.

It can still be graded, because of what the patch actually is. The test change
is a single added argument::

    HashCalculatorTask(context, HashType.MD5,
                       Uri.fromFile(new File("")),
    +                  Uri.fromFile(new File("")),
                       hashValue -> hashValueToAssert[0] = hashValue);

and the fix patch is the constructor that accepts it::

    public HashCalculatorTask(@NonNull Context context, @NonNull HashType hashType,
                              @NonNull Uri fileUri,
    +                         @NonNull Uri folderUri,
                              @NonNull HashCalculatorTaskTarget completeListener)

So the gold test patch is **compile-gated** against the gold fix patch. Against
the unpatched tree the call site resolves; with only the test patch applied
``javac`` cannot resolve a five-argument constructor and
``:app:compileThirdPartyStoresDebugAndroidTestJavaWithJavac`` fails; with both
patches it resolves again. That is a genuine, non-tautological FAIL -> PASS
signal for the exact API the PR introduces, and it needs no device.

Reporting Gradle tasks as tests is the established pattern in this tree for
Android repos whose graded surface is a compile step -- see
``java/TeamNewPipe/NewPipe.py``, which runs
``testDebugUnitTest compileDebugAndroidTestJavaWithJavac --continue`` and keys
``TestResult`` on ``> Task :...`` lines for the same reason.

Stage matrix (task ``:app:compileThirdPartyStoresDebugAndroidTestJavaWithJavac``):

===========  ==================  ==========================================
stage        patches applied     outcome
===========  ==================  ==========================================
``run``      none                UP-TO-DATE from the image-build warm-up
``test``     test.patch          FAILED -- no 5-arg constructor exists
``fix``      test + fix.patch    executes and succeeds
===========  ==================  ==========================================

which is the ``FAIL -> PASS`` transition ``Report.check()`` rule 3 requires.

``Report.check()`` rule 5 (the cheating guard) is clean by construction: the
test patch touches only ``app/src/androidTest/...`` and the fix patch touches
only ``README.md``, ``app/src/main/**`` and the ``values-*/strings.xml`` set, so
``set(fix_patch_files) & set(test_patch_files)`` is empty.

Variant selection
-----------------
``app/build.gradle`` declares ``flavorDimensions "distribute-version"`` with the
flavors ``thirdPartyStores`` and ``googlePlay``, so there is no plain
``compileDebugAndroidTestJavaWithJavac`` -- every task name carries the flavor.
``thirdPartyStores`` is used because ``googlePlay`` is the only flavor that
pulls ``com.google.android.play:core:1.10.2``, which is irrelevant to the graded
change and is one more resolvable dependency that can go down. The two flavors
share every source directory that this PR touches.

``CI`` must NOT be set -- this is the one place the config contradicts house style
-------------------------------------------------------------------------------
``app/build.gradle`` evaluates this inside ``signingConfigs { release { ... } }``,
which runs during the **configuration** phase of every single Gradle
invocation::

    } else if (System.getenv('CI')) {
        throw new InvalidUserDataException(
            "You should define sign keys in gradle.properties or in ENV.")
    }

Upstream CI survives it only because the workflow injects four
``RELEASE_KEYSTORE_*`` secrets that this harness does not have. With ``CI=true``
and no keystore, configuration aborts before any task runs, every stage yields
an empty log, ``parse_log`` returns 0/0/0 and ``Report.check()`` rule 1 rejects
the instance -- the exact silent-failure mode ``CI=true`` is normally set to
avoid. ``prepare.sh`` and all three run scripts therefore ``unset CI`` instead of
exporting it. Nothing else in this build reads ``CI``.

Toolchain
---------
Pinned to what the repo itself declares, not guessed:

* **JDK 11** -- ``.github/workflows/build.yml`` uses ``actions/setup-java@v1``
  with ``java-version: 11`` in all three jobs. It is also the only workable
  choice: ``gradle-wrapper.properties`` pins Gradle **7.0.2**, which does not
  support JDK 17, and ``gradle.properties`` carries ``-XX:MaxPermSize=512m``,
  which HotSpot merely warns about on 11 but rejects outright on 17.
* **Gradle 7.0.2 / AGP 7.0.3** -- from ``gradle/wrapper/gradle-wrapper.properties``
  and the root ``build.gradle`` buildscript classpath. The wrapper downloads its
  own distribution, so nothing is pinned here.
* **Android SDK** ``platforms;android-31`` + ``build-tools;31.0.0`` -- exactly
  ``compileSdkVersion 31`` / ``buildToolsVersion '31.0.0'`` from
  ``app/build.gradle``.
* ``commandlinetools-linux-9477386`` -- the last cmdline-tools release that runs
  on JDK 11; 11076708 and later require JDK 17, which this build cannot use.

``/root/.gradle/gradle.properties`` overrides the project's daemon JVM args.
``GRADLE_USER_HOME/gradle.properties`` outranks the project file in Gradle's
precedence order, which is the only reliable way to replace
``-Xmx1536m -XX:MaxPermSize=512m`` (too small for an AGP 7 build, and carrying a
flag that is obsolete on every JDK this image could use). ``GRADLE_OPTS`` is
deliberately not used for this: it configures the client JVM, not the daemon
that actually compiles.

No Maven mirror is installed. Rewriting Gradle's repository chain to a mirror
has previously turned a single mirror 5xx into what looked like a broken fix
patch; the repos this build declares (``google()``, ``mavenCentral()``,
``jitpack.io``) are used unmodified.

``checkstyle.xml`` is fetched at image-build time
-------------------------------------------------
``app/checkstyle.gradle`` runs this in an eagerly-created task block, i.e. during
**configuration** of every invocation::

    if (!checkstyleConfig.exists()) {
        new URL("https://raw.githubusercontent.com/.../checkstyle.xml")
            .withInputStream { ... }
    }

The file is ``.gitignore``d (line 70), so it is never in the tree after a clone
and the download would otherwise run inside all three graded stages -- turning
raw.githubusercontent.com into a live dependency of the grade, and a hard
configuration failure if it is unreachable. ``prepare.sh`` seeds it once, after
``git clean -fdx``, with a minimal valid Checker config as the fallback. Because
it is gitignored it cannot dirty ``git status --porcelain`` for
``check_git_changes.sh`` or interfere with ``git apply``. The content is never
read: no Checkstyle task is in the graded task list.

Unit tests are forced to re-run
-------------------------------
``prepare.sh`` warms the build at image-build time, so the ``run`` stage would
otherwise report ``> Task :app:testThirdPartyStoresDebugUnitTest UP-TO-DATE``
and print no per-test lines at all, while the ``fix`` stage -- whose sources did
change -- would print them. The four ``app/src/test`` classes would then be
``NONE`` in run and test but ``PASS`` in fix, which the classifier turns into
phantom ``n2p`` entries. The init script in ``GRADLE_USER_HOME`` sets
``outputs.upToDateWhen { false }`` on every ``Test`` task so the same unit-test
names appear, identically, in all three stages. Compile tasks stay incremental,
which is what keeps three Android builds affordable.

That same init script enables ``testLogging { events 'passed','failed','skipped' }``.
Gradle prints nothing per test by default; without it the unit tests would
collapse into the single task-level result ``:app:testThirdPartyStoresDebugUnitTest``.
It is applied from ``GRADLE_USER_HOME`` rather than the project so the repo under
test is never edited.

Test identity
-------------
Two name shapes, neither of which can collide with the other:

* Gradle tasks -- ``:app:compileThirdPartyStoresDebugAndroidTestJavaWithJavac``.
  Always leading-colon, always unique within a build.
* JUnit cases -- ``com.smlnskgmail.jaman.hashchecker.calculator.HashTypeTest > someTest``.
  Fully-qualified class plus method.

Neither carries a duration, a count, or any other per-run value, so a name is
byte-identical across the three stages. That matters more than usual here:
an unstable name surfaces as the ``PASS -> NONE -> FAIL`` anomaly
``Report.check()`` rule 4 rejects.

Architecture
------------
**amd64 only.** Nothing in the Dockerfile hardcodes an architecture -- the
cmdline-tools archive is pure JVM and arch-independent -- but AGP 7 resolves
``aapt2`` from Google's Maven repository and Google publishes a Linux x86_64
binary only. ``processThirdPartyStoresDebugAndroidTestResources`` is an
unavoidable dependency of the graded compile task, so an arm64 image would fail
in every stage. Build this single-arch; an arm64 variant would need an aapt2
override that does not exist upstream for this AGP line.
"""

import base64
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The Gradle project path and the two graded tasks, in one place so that the
# three run scripts and prepare.sh cannot drift apart. A stage that ran a
# different command than its siblings would make a FAIL -> PASS transition
# attributable to the command rather than to the fix patch.
_UNIT_TEST_TASK = ":app:testThirdPartyStoresDebugUnitTest"
_ANDROID_TEST_COMPILE_TASK = (
    ":app:compileThirdPartyStoresDebugAndroidTestJavaWithJavac"
)

# --console=plain: Gradle's rich console rewrites `> Task :x` lines in place.
#   Docker's non-TTY pipe usually disables it already; asserting it removes the
#   one input parse_log cannot recover from.
# --no-daemon: a reused daemon would carry state between the warm-up and the
#   graded stages.
# --continue: the whole design depends on the unit tests still running in the
#   `test` stage after the androidTest compile has failed.
_GRADLE_CMD = (
    f"./gradlew {_UNIT_TEST_TASK} {_ANDROID_TEST_COMPILE_TASK} "
    "--continue --no-daemon --console=plain"
)

# `DockerfileEnhancer._ENV_BLOCK` emits `ENV HTTP_PROXY=${HTTP_PROXY}` over an
# ARG whose default is `""`, so with no proxy configured these four variables are
# *defined and empty* rather than absent. Most tools treat that as "no proxy";
# `sdkmanager` does not -- it feeds any set value straight into `new URL(...)`
# and dies during argument parsing, before it ever reaches the network:
#
#     Error: The proxy server URL extracted from HTTP_PROXY or HTTPS_PROXY
#     environment variable could not be parsed.
#     java.net.MalformedURLException: no protocol:
#         at com.android.sdklib.tool.sdkmanager.SdkManagerCliSettings.<init>
#
# Measured 2026-08-24 on the first build of this image. Only *empty* values are
# dropped, so a real proxy supplied through the build args still reaches every
# tool. This has to be re-applied per shell: Docker's ENV cannot unset a
# variable, only set it to empty, and the run scripts inherit the same block at
# container start.
_SCRUB_EMPTY_PROXY = """\
for _pv in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do
    if [ -z "$(printenv $_pv)" ]; then unset $_pv; fi
done
unset _pv"""

# Applied to every Gradle invocation in the image: init scripts placed directly
# in GRADLE_USER_HOME are picked up automatically, so the repo under test is
# never modified to get this behaviour.
_GRADLE_INIT_GRADLE = """\
// --- kotlinx-html-jvm:0.7.2 ---------------------------------------------
// The root build.gradle puts `com.jaredsburrows:gradle-license-plugin:0.8.90`
// on the buildscript classpath. That plugin depends on
// org.jetbrains.kotlinx:kotlinx-html-jvm:0.7.2, which was published to JCenter
// and never to Maven Central, so since JCenter's shutdown the repositories this
// project declares (google, mavenCentral, plugins.gradle.org) cannot resolve it
// and EVERY Gradle invocation dies while configuring the root project:
//
//     > Could not resolve all artifacts for configuration ':classpath'.
//        > Could not find org.jetbrains.kotlinx:kotlinx-html-jvm:0.7.2.
//
// Verified 2026-08-24: repo1.maven.org returns 404 for that POM and JetBrains'
// public Space repository returns 200. This is dead infrastructure, not a
// property of the PR -- the same failure hits every commit of this repo.
//
// `beforeProject` is the hook that runs before a project's build script is
// evaluated, which is the only point at which the buildscript classpath can
// still be influenced. `includeGroup` keeps the repository from being consulted
// for anything other than this one group, so it can never shadow or reorder the
// repositories the build declares for itself -- this is an addition, not a
// mirror rewrite.
gradle.beforeProject { project ->
    project.buildscript.repositories.maven {
        url 'https://maven.pkg.jetbrains.space/public/p/kotlinx-html/maven'
        content { includeGroup 'org.jetbrains.kotlinx' }
    }
    project.repositories.maven {
        url 'https://maven.pkg.jetbrains.space/public/p/kotlinx-html/maven'
        content { includeGroup 'org.jetbrains.kotlinx' }
    }
}

gradle.allprojects { project ->
    project.tasks.withType(org.gradle.api.tasks.testing.Test).configureEach { task ->
        // prepare.sh warms the build, so a stage whose sources did not change
        // would report the test task UP-TO-DATE and emit no per-test lines at
        // all -- leaving those names NONE in run/test but PASS in fix.
        task.outputs.upToDateWhen { false }

        // Gradle prints nothing per test by default. These three events are what
        // turn each case into a `com.pkg.Class > method PASSED` line.
        task.testLogging { logging ->
            logging.events 'passed', 'failed', 'skipped'
            logging.showStandardStreams = false
            logging.exceptionFormat 'short'
        }
    }
}
"""

# Base64 so the Groovy survives the Dockerfile f-string with no brace escaping
# and no quoting rules to get wrong. The plaintext above is the source of truth.
_GRADLE_INIT_B64 = base64.b64encode(_GRADLE_INIT_GRADLE.encode()).decode()

# Shared body of run.sh / test-run.sh / fix-run.sh. Identical in all three by
# construction: the only thing that differs between the graded stages is which
# patch was applied above this block.
_TEST_BODY = """\
# Runs in /home/{repo}; every caller cd's there first.
#
# Deliberately non-fatal, then re-armed. The `test` stage is *supposed* to end
# with a non-zero Gradle exit: the androidTest compile task cannot resolve the
# five-argument HashCalculatorTask constructor that the gold test patch calls,
# and that failure is the graded signal. Aborting on it under `set -e` would cut
# the stage off before the log reached stdout, leaving parse_log with nothing
# and tripping Report.check() rule 1 on an instance that is in fact working.
#
# This does not weaken the failure signal -- the start-up assertion at the
# bottom is what guarantees a stage cannot silently report 0/0/0.
set +e
{gradle_cmd} > /tmp/gradle.out 2>&1
GRADLE_RC=$?
set -e

# parse_log reads stdout, so the captured build output has to land there.
cat /tmp/gradle.out

if [ "$GRADLE_RC" -ne 0 ]; then
    echo "NOTE: gradle exited $GRADLE_RC; see the task results above"
fi

# Start-up guarantee. A build that died in the configuration phase -- an
# unreachable repository, a missing SDK component, or `CI` leaking back into the
# environment and re-arming the signingConfigs abort in app/build.gradle --
# prints no `> Task :app:` line at all. Failing here surfaces that as a broken
# stage instead of an empty TestResult that looks like a legitimate 0/0/0.
grep -q "^> Task :app:" /tmp/gradle.out
"""


class HashCheckerImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- JDK 11 plus the Android SDK.

    Tagged per PR rather than with a shared ``:base``: one shared tag would be
    rewritten by every other instance of this repo, silently changing the
    foundation an already-verified instance was built against.

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance``
    rewrites the ``git clone`` below into the standard clone +
    ``checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` + ``CMD`` sequence
    and supplies ``REPO_URL`` / ``BASE_COMMIT`` as build args. Nothing that
    matters is emitted after the clone line for exactly that reason -- the
    enhancer appends ``CMD`` there, and any later instruction would be stranded
    below it.
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

    def dependency(self) -> str | Image:
        # Debian bullseye, with the JDK apt-installed -- the conventional Java
        # shape, where the base is a bare OS and D10 supplies the runtime.
        #
        # JDK 11 is what .github/workflows/build.yml pins, and the only JDK that
        # both Gradle 7.0.2 and the project's -XX:MaxPermSize flag tolerate.
        #
        # bullseye specifically, and this is load-bearing rather than taste:
        #
        # * It still carries `openjdk-11-jdk` (11.0.32). bookworm dropped it --
        #   verified 2026-08-24: `apt-cache policy openjdk-11-jdk` finds nothing
        #   on bookworm, only openjdk-17, which Gradle 7.0.2 cannot use.
        # * Debian serves every architecture from one archive, so
        #   `dpkg --add-architecture amd64` just works. That is what makes the
        #   arm64 aapt2 shim below possible at all. The previous base,
        #   `eclipse-temurin:11-jdk`, resolves to Ubuntu 26.04, where
        #   `qemu-user-static` has no installation candidate and the arm64 apt
        #   sources point at ports.ubuntu.com, which carries no amd64 packages --
        #   both verified 2026-08-24. On that base the arm64 image can be built
        #   but never made to work.
        return "debian:bullseye"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV LC_ALL=C.UTF-8
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV PATH="/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH"

# DEBIAN_FRONTEND / LANG / TZ come from DockerfileEnhancer._ENV_BLOCK; declaring
# them again here would only create two places to keep in sync.
#
# openjdk-11-jdk-headless, not -jre: Gradle needs javac. No JAVA_HOME is set --
# Debian's alternatives put java on PATH, and its value is arch-dependent
# (.../java-11-openjdk-amd64 vs -arm64), so hardcoding one would break the other.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git unzip wget openjdk-11-jdk-headless \\
    && rm -rf /var/lib/apt/lists/*

# GRADLE_USER_HOME/gradle.properties outranks the project's gradle.properties in
# Gradle's precedence order. That is the only reliable way to drop the project's
# `-Xmx1536m -XX:MaxPermSize=512m` -- too small for an AGP 7 build, and carrying
# a flag no supported JDK still honours. GRADLE_OPTS cannot do this: it sizes the
# client JVM, not the daemon that compiles.
RUN mkdir -p /root/.gradle && \\
    echo "org.gradle.jvmargs=-Xmx4g -Dfile.encoding=UTF-8" > /root/.gradle/gradle.properties && \\
    echo "org.gradle.daemon=false" >> /root/.gradle/gradle.properties && \\
    echo "org.gradle.parallel=false" >> /root/.gradle/gradle.properties && \\
    echo "org.gradle.configureondemand=false" >> /root/.gradle/gradle.properties

# Forces every Test task to re-run and to log per-case events. See the module
# docstring; the plaintext lives in _GRADLE_INIT_GRADLE.
RUN echo "{_GRADLE_INIT_B64}" | base64 -d > /root/.gradle/init.gradle

# ---- arm64 only: make AGP's x86-64 aapt2 runnable under emulation ----------
#
# AGP 7.0.3 resolves aapt2 from Google's Maven repo, and Google publishes a
# Linux **x86_64-only** binary for the 7.x line -- there is no linux-aarch64
# asset. `processThirdPartyStoresDebugAndroidTestResources` is an unavoidable
# dependency of the graded compile task, so on arm64 the image builds green and
# is then useless. Measured 2026-08-24 on the first multi-arch build:
#
#     > Task :app:processThirdPartyStoresDebugAndroidTestResources FAILED
#       > AAPT2 aapt2-7.0.3-7396180-linux Daemon #1: Daemon startup failed
#     compileThirdPartyStoresDebugAndroidTestJavaWithJavac -- never ran
#     -> 25 pass / 2 fail, f2p task absent from every stage, 0 resolved.
#
# The shim: fetch that exact x86-64 aapt2, give the image an amd64 multiarch
# runtime for it, and point AGP at a wrapper that runs it under qemu-user.
# Verified 2026-08-24 in an arm64 container -- one-shot *and* the daemon
# protocol AGP actually drives, which a one-shot test would not have caught:
#
#     $ /usr/local/bin/aapt2-qemu version
#     Android Asset Packaging Tool (aapt) 2.19-7396180
#     $ printf 'quit\\n' | /usr/local/bin/aapt2-qemu daemon
#     Ready
#     Exiting daemon                      (rc=0)
#
# TARGETARCH-guarded so the amd64 image is byte-for-byte unaffected: it keeps
# using AGP's own native aapt2, which is faster and needs no emulation. The
# version is pinned to the one AGP 7.0.3 resolves (read out of the working
# amd64 image's Gradle transforms cache), so the wrapper cannot drift from the
# binary AGP expects.
#
# The wrapper MUST be named exactly `aapt2` and therefore lives in its own
# directory. AGP validates the override path against SdkConstants.FN_AAPT2
# before it applies the plugin, and rejects anything else outright --
# measured 2026-08-24 with a wrapper called `aapt2-qemu`:
#
#     > Failed to apply plugin 'com.android.internal.application'.
#        > Custom AAPT2 location does not point to an AAPT2 executable:
#          /usr/local/bin/aapt2-qemu
#
# That fails during configuration, before any task runs, so the graded-task
# guard in prepare.sh is what catches it rather than a silent 0/1.
RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        set -eux; \\
        dpkg --add-architecture amd64; \\
        apt-get update; \\
        apt-get install -y --no-install-recommends \\
            qemu-user-static libc6:amd64 libstdc++6:amd64 zlib1g:amd64; \\
        rm -rf /var/lib/apt/lists/*; \\
        mkdir -p /opt/aapt2-real; \\
        wget -q https://dl.google.com/dl/android/maven2/com/android/tools/build/aapt2/7.0.3-7396180/aapt2-7.0.3-7396180-linux.jar -O /tmp/aapt2.jar; \\
        unzip -o -q /tmp/aapt2.jar aapt2 -d /opt/aapt2-real; \\
        rm -f /tmp/aapt2.jar; \\
        chmod +x /opt/aapt2-real/aapt2; \\
        mkdir -p /opt/aapt2-shim; \\
        printf '#!/bin/sh\\nexec /usr/bin/qemu-x86_64-static /opt/aapt2-real/aapt2 "$@"\\n' > /opt/aapt2-shim/aapt2; \\
        chmod +x /opt/aapt2-shim/aapt2; \\
        /opt/aapt2-shim/aapt2 version; \\
        printf 'quit\\n' | /opt/aapt2-shim/aapt2 daemon; \\
        echo "android.aapt2FromMavenOverride=/opt/aapt2-shim/aapt2" >> /root/.gradle/gradle.properties; \\
    fi

# 9477386 is the last cmdline-tools release that runs on JDK 11. The archive is
# pure JVM, so it carries no architecture of its own.
RUN mkdir -p $ANDROID_HOME/cmdline-tools && \\
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip -O /tmp/cmdline-tools.zip && \\
    unzip -q /tmp/cmdline-tools.zip -d $ANDROID_HOME/cmdline-tools && \\
    mv $ANDROID_HOME/cmdline-tools/cmdline-tools $ANDROID_HOME/cmdline-tools/latest && \\
    rm -f /tmp/cmdline-tools.zip

# Pre-write the license hashes rather than piping into `sdkmanager --licenses`,
# which is interactive and JDK-quirky. With these present sdkmanager never
# prompts, so a non-TTY build cannot hang on it.
RUN mkdir -p $ANDROID_HOME/licenses && \\
    echo "8933bad161af4178b1185d1a37fbf41ea5269c55" > $ANDROID_HOME/licenses/android-sdk-license && \\
    echo "d56f5187479451eabf01fb78af6dfcb131a6481e" >> $ANDROID_HOME/licenses/android-sdk-license && \\
    echo "24333f8a63b6825ea9c5514f83c2829b004d1fee" >> $ANDROID_HOME/licenses/android-sdk-license && \\
    echo "84831b9409646a918e30573bab4c9c91346d8abd" > $ANDROID_HOME/licenses/android-sdk-preview-license && \\
    echo "504667f4c0de7af1a06de9f4b1727b84351f2910" >> $ANDROID_HOME/licenses/android-sdk-preview-license

# Exactly compileSdkVersion 31 / buildToolsVersion '31.0.0' from app/build.gradle.
# Failing loudly here beats discovering a missing platform three stages later.
# The proxy scrub is mandatory, not defensive -- see _SCRUB_EMPTY_PROXY.
RUN for _pv in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do \\
        if [ -z "$(printenv $_pv)" ]; then unset $_pv; fi; \\
    done; \\
    sdkmanager --sdk_root=$ANDROID_HOME "platform-tools" "platforms;android-31" "build-tools;31.0.0" > /tmp/sdkmanager.log 2>&1 || \\
    (echo "sdkmanager failed:" && tail -40 /tmp/sdkmanager.log && exit 1)

{code}

{self.clear_env}

"""


class HashCheckerImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT, seeds checkstyle.xml, warms the build."""

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
        return HashCheckerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

# app/build.gradle aborts configuration when CI is set and no RELEASE_KEYSTORE_*
# secrets are present. See the module docstring -- this is not an oversight.
unset CI

# Drop proxy variables the enhancer defined as empty. See _SCRUB_EMPTY_PROXY.
{proxy_scrub}

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

chmod +x ./gradlew

# app/checkstyle.gradle downloads this ruleset during the CONFIGURATION phase of
# every Gradle invocation when it is missing, and .gitignore line 70 guarantees
# it is missing after a clone. Seeding it once here keeps raw.githubusercontent.com
# out of the three graded stages. It stays gitignored, so it cannot dirty
# `git status --porcelain` or interfere with `git apply`. Its content is never
# read: no Checkstyle task is in the graded task list, hence the inert fallback.
if [ ! -f /home/{pr.repo}/checkstyle.xml ]; then
    wget -q -O /home/{pr.repo}/checkstyle.xml \\
        https://raw.githubusercontent.com/fartem/repository-rules/master/rules/java/android/checkstyle.xml || {{
        echo '<?xml version="1.0"?>' > /home/{pr.repo}/checkstyle.xml
        echo '<!DOCTYPE module PUBLIC "-//Checkstyle//DTD Checkstyle Configuration 1.3//EN" "https://checkstyle.org/dtds/configuration_1_3.dtd">' >> /home/{pr.repo}/checkstyle.xml
        echo '<module name="Checker"/>' >> /home/{pr.repo}/checkstyle.xml
    }}
fi

# Warm the Gradle distribution, the dependency cache and the build outputs so the
# graded stages are incremental. `|| true`: a transient resolution failure here
# must not abort the image build, and the assertions below cover what matters.
{gradle_cmd} > /tmp/warmup.log 2>&1 || true

# The one assumption in this config that is derived rather than read: the AGP
# task names for the `thirdPartyStores` + `debug` variant. If either is wrong,
# Gradle says so and every stage would otherwise report a uniform 0/0/0 that
# looks like an unfixable instance rather than a typo.
if grep -qE "Task .[^']*. not found|Cannot locate tasks that match" /tmp/warmup.log; then
    echo "FATAL: gradle task name mismatch -- check the flavor/buildType in app/build.gradle" >&2
    tail -60 /tmp/warmup.log >&2
    exit 1
fi

# Configuration-phase failures (unreachable repository, missing SDK component,
# CI leaking in) never reach task execution. Catch that at build time.
if ! grep -q "^> Task :app:" /tmp/warmup.log; then
    echo "FATAL: gradle never reached task execution during the warm-up" >&2
    tail -60 /tmp/warmup.log >&2
    exit 1
fi

# Reaching *some* task is not enough -- the two graded tasks have to have run
# and succeeded on the unpatched tree, or the image is green but ungradeable.
#
# Measured 2026-08-24 on a linux/arm64 build: 35 `> Task :app:` lines were
# emitted, so the check above passed, yet `processThirdPartyStoresDebugAndroidTestResources`
# FAILED ("AAPT2 ... Daemon startup failed" -- AGP resolves an x86-64 aapt2 that
# cannot exec on aarch64) and the graded compile task never ran at all. That
# image would have shipped and produced 25/2/8 with the f2p task absent from
# every stage, i.e. a silently unresolvable instance rather than a loud build
# failure. Assert the graded tasks specifically.
for _task in "{unit_test_task}" "{android_test_task}"; do
    if ! grep -qE "^> Task $_task( |$)" /tmp/warmup.log; then
        echo "FATAL: graded task $_task never executed during the warm-up" >&2
        grep -E "^> Task :app:.* FAILED$" /tmp/warmup.log >&2 || true
        tail -60 /tmp/warmup.log >&2
        exit 1
    fi
    if grep -qE "^> Task $_task FAILED$" /tmp/warmup.log; then
        echo "FATAL: graded task $_task FAILED on the unpatched tree" >&2
        tail -60 /tmp/warmup.log >&2
        exit 1
    fi
done
unset _task

""".format(pr=self.pr, gradle_cmd=_GRADLE_CMD, proxy_scrub=_SCRUB_EMPTY_PROXY,
                             unit_test_task=_UNIT_TEST_TASK,
                             android_test_task=_ANDROID_TEST_COMPILE_TASK),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

# NOT `export CI=true`. app/build.gradle throws InvalidUserDataException during
# configuration when CI is set without the RELEASE_KEYSTORE_* secrets, which
# would empty every stage. See the module docstring.
unset CI

# Drop proxy variables the enhancer defined as empty. See _SCRUB_EMPTY_PROXY.
{proxy_scrub}

cd /home/{pr.repo}
""".format(pr=self.pr, proxy_scrub=_SCRUB_EMPTY_PROXY)
                + _TEST_BODY.format(repo=self.pr.repo, gradle_cmd=_GRADLE_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

unset CI

# Drop proxy variables the enhancer defined as empty. See _SCRUB_EMPTY_PROXY.
{proxy_scrub}

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr, proxy_scrub=_SCRUB_EMPTY_PROXY)
                + _TEST_BODY.format(repo=self.pr.repo, gradle_cmd=_GRADLE_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

unset CI

# Drop proxy variables the enhancer defined as empty. See _SCRUB_EMPTY_PROXY.
{proxy_scrub}

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr, proxy_scrub=_SCRUB_EMPTY_PROXY)
                + _TEST_BODY.format(repo=self.pr.repo, gradle_cmd=_GRADLE_CMD),
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# `> Task :app:compileThirdPartyStoresDebugAndroidTestJavaWithJavac FAILED`
# `> Task :app:testThirdPartyStoresDebugUnitTest`
# `> Task :app:preBuild UP-TO-DATE`
# The leading colon is kept in the reported name so a Gradle task ID can never be
# confused with a JUnit one, in this log or in the emitted dataset.
_TASK_LINE = re.compile(r"^> Task (:\S+)(?:\s+(\S+))?\s*$")

# `com.smlnskgmail.jaman.hashchecker.calculator.HashTypeTest > someTest PASSED`
# Produced only because the init script enables testLogging events. The leading
# `\S` cannot match a `> Task` line, which begins the line with `>`.
_JUNIT_LINE = re.compile(r"^(\S.* > .+?) (PASSED|FAILED|SKIPPED)$")

# Gradle's task-status suffixes. Anything else after the task path -- including
# no suffix at all -- means the task executed and succeeded.
_TASK_FAILED_SUFFIXES = {"FAILED"}
_TASK_SKIPPED_SUFFIXES = {"SKIPPED", "NO-SOURCE"}


def parse_gradle_log(log: str) -> TestResult:
    """Report Gradle task outcomes and JUnit cases as one flat TestResult.

    Two independent name shapes share the result set: ``:app:<task>`` for Gradle
    tasks and ``<FQCN> > <method>`` for JUnit cases. Task outcomes are what carry
    the graded signal for this PR -- the gold test lives in ``androidTest`` and
    cannot execute without a device, but its compile task can fail and recover.
    See the module docstring.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # `--console=plain` should already prevent this, but a colourised log would
    # defeat every anchor below, so stripping is unconditional.
    clean = ANSI_ESCAPE.sub("", log)

    for raw in clean.splitlines():
        line = raw.rstrip()

        m = _TASK_LINE.match(line)
        if m:
            name, suffix = m.group(1), m.group(2)
            if suffix in _TASK_FAILED_SUFFIXES:
                failed_tests.add(name)
            elif suffix in _TASK_SKIPPED_SUFFIXES:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)
            continue

        # Everything Gradle writes with a `> ` prefix is progress bookkeeping
        # (`> Configure project :app`, `> Transform ...`), never a test result.
        if line.startswith("> "):
            continue

        m = _JUNIT_LINE.match(line)
        if m:
            name, status = m.group(1), m.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

    # TestResult.__post_init__ rejects overlapping sets. A task can be reported
    # more than once across a `--continue` build; failure is the honest verdict.
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


@Instance.register("hash-checker", "hash-checker")
class HashChecker(Instance):
    """Instance handler for hash-checker/hash-checker.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves
    on. The org keeps its hyphen because the JSONL does.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return HashCheckerImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_gradle_log(log)
