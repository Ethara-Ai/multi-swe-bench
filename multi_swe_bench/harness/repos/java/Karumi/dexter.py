"""Karumi/Dexter - Android library, JDK 8 / Gradle 5.6.4 / AGP 3.6.2.

Every value below was discovered by running the real toolchain in Docker at base
commit 11c6351c, not inferred from manifests:

  .travis.yml            jdk: oraclejdk8            -> JDK 8, not a modern LTS
                         android: platforms;android-28, build-tools-27.0.3
  gradle-wrapper.props   gradle-5.6.4-all.zip
  build.gradle           com.android.tools.build:gradle:3.6.2
  dexter/build.gradle    apply plugin: 'com.android.library'
                         compileSdkVersion 28
                         testImplementation junit:4.12, mockito-core:2.28.2
  settings.gradle        include ':dexter', ':sample'

Two things make this repo harder than a plain JVM project:

1. `com.android.library` means Gradle cannot even CONFIGURE without an Android
   SDK present. The base image therefore installs cmdline-tools plus
   platforms;android-28 and build-tools;28.0.3. Release 6858069 is used because
   it is the last cmdline-tools that runs on Java 8 - newer ones require Java 17
   and would not start under this JDK.

2. The build declares `jcenter()`, which Bintray shut down. In practice the
   fatal error was not jcenter but Maven Central returning HTTP 429 (Too Many
   Requests) for the AGP dependency tree:

     Could not GET 'https://repo.maven.apache.org/maven2/.../jaxb-runtime-2.3.1.pom'
     Received status code 429 from server: Too Many Requests

   init.gradle puts Google's read-only GCS mirror of Central first, which
   resolves the same coordinates without the rate limit.

Only `:dexter:test` is run. The repo's own CI script is
`./gradlew checkstyle build test connectedDebugAndroidTest`, but
`connectedDebugAndroidTest` drives an emulator over adb, which this harness
cannot provide - and `checkstyle` would fail the build on style grounds that
have nothing to do with the patch.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# `:dexter:test` runs BOTH testDebugUnitTest and testReleaseUnitTest, so every
# test is reported twice - once per build variant. parse_log keeps the variant
# in the name so the two stay distinct.
GRADLE_TEST = (
    "./gradlew :dexter:test --console=plain --no-daemon "
    "--init-script /home/init.gradle "
    # Bound every HTTP call. Without these, a socket that dies mid-transfer
    # leaves Gradle blocked on a read FOREVER - observed on the linux/arm64 leg
    # under QEMU, where the build sat for 4 hours consuming 2 seconds of CPU with
    # the network byte counters frozen. A timeout turns that silent hang into a
    # fast, visible failure.
    "-Dorg.gradle.internal.http.connectionTimeout=60000 "
    "-Dorg.gradle.internal.http.socketTimeout=60000 "
    # BuildKit caps its container near 2 GB; an unbounded JVM under emulation
    # thrashes against that and can be OOM-killed mid-resolution.
    "-Dorg.gradle.jvmargs=-Xmx1g"
)

INIT_GRADLE = """\
// Injected with --init-script so build.gradle stays PRISTINE - the dataset's
// patches are applied with `git apply`, which breaks if a prepare-time edit has
// already rewritten a tracked build file.

def mirrors = { rh ->
    // FIRST: Google's read-only mirror of Maven Central. Resolving the AGP 3.6.2
    // dependency tree straight from repo.maven.apache.org returns HTTP 429
    // (Too Many Requests) and the build dies before a single test compiles.
    rh.maven { url "https://maven-central.storage-download.googleapis.com/maven2/" }
    // Android artifacts (AGP, androidx, material) live only here.
    rh.google()
    // Fallback for anything the mirror misses.
    rh.mavenCentral()
    rh.gradlePluginPortal()
}

// The repo declares jcenter(), which Bintray shut down. These repositories are
// ADDED rather than replacing the project's own, so a coordinate that still
// resolves upstream keeps working; the mirror simply gets consulted first.
settingsEvaluated { s -> s.pluginManagement { pm -> mirrors(pm.repositories) } }

allprojects { p ->
    p.buildscript.repositories { mirrors(delegate) }
    mirrors(p.repositories)

    p.tasks.withType(Test).configureEach {
        // Gradle prints nothing per test by default; parse_log would then see
        // only the task-level `:dexter:test` line and could never derive f2p.
        testLogging {
            events "passed", "failed", "skipped"
            showStandardStreams = false
            displayGranularity = 0
        }
        outputs.upToDateWhen { false }

        // Without this a failing test fails the task and Gradle stops, so the
        // stage reports a truncated suite. f2p is derived by comparing the three
        // stages, so a suite that shrinks when a test fails invents transitions
        // that never happened and hides the real ones. The test stage is EXPECTED
        // to fail here - that is the whole point - so it must not abort.
        ignoreFailures = true

        maxParallelForks = 1
    }
}
"""


class DexterImageBase(Image):
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
        # .travis.yml pins `jdk: oraclejdk8`. Gradle 5.6.4 + AGP 3.6.2 are from
        # 2020 and do not run on a modern LTS.
        return "eclipse-temurin:8-jdk"

    def image_tag(self) -> str:
        # Per-PR: DockerfileEnhancer injects a hardening block that detaches at
        # one ${BASE_COMMIT} and prunes every other object, so a shared tag would
        # let whichever PR built first pin the commit for all the others.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

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

        return f"""\
FROM {image_name}

{self.global_env}

ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=/opt/android-sdk
ENV PATH=/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH
ENV GRADLE_OPTS="-Dorg.gradle.daemon=false"
ENV LC_ALL=C.UTF-8
ENV CI=true

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl unzip ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# Release 6858069 is the last command-line-tools that runs on Java 8; every
# later build requires Java 17 and exits immediately under this JDK.
RUN mkdir -p $ANDROID_SDK_ROOT/cmdline-tools \\
 && cd /tmp \\
 && curl -sSLo cmdtools.zip https://dl.google.com/android/repository/commandlinetools-linux-6858069_latest.zip \\
 && unzip -q cmdtools.zip -d $ANDROID_SDK_ROOT/cmdline-tools \\
 && mv $ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools $ANDROID_SDK_ROOT/cmdline-tools/latest \\
 && rm cmdtools.zip

# `env -u` strips the proxy variables for these two commands only. DockerfileEnhancer
# injects HTTP_PROXY="" / HTTPS_PROXY="" (empty by default), and sdkmanager treats a
# SET-but-empty value as a proxy URL and dies before downloading anything:
#     Error: The proxy server URL extracted from HTTP_PROXY or HTTPS_PROXY
#            environment variable could not be parsed.
#     java.net.MalformedURLException: no protocol:
# Unsetting per-command rather than globally keeps the image's proxy support intact
# for the MITM case, where the variables are given real values at build time.
#
# `yes |` because sdkmanager prompts for each licence; without it the build hangs
# forever with no TTY attached.
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \\
        sh -c 'yes | sdkmanager --licenses' > /dev/null 2>&1 || true
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \\
        sdkmanager "platform-tools" "platforms;android-28" "build-tools;28.0.3" > /dev/null

WORKDIR /home/

{code}

{self.clear_env}

"""


class DexterImageDefault(Image):
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
        return DexterImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "init.gradle", INIT_GRADLE),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
# Assert the reset actually produced a clean tree rather than assuming it did.
# A stray modified file would flow into all three graded stages and corrupt the
# comparison with nothing in the log to explain why.
bash /home/check_git_changes.sh

git checkout {pr.base.sha}
bash /home/check_git_changes.sh

chmod +x gradlew

# Warm the Gradle distribution and the whole AGP dependency tree into this image
# layer so the scored stages neither pay for the download nor depend on the
# network.
#
# `timeout 1800` is NOT belt-and-braces on top of `|| true` - it covers a case
# `|| true` cannot. `|| true` handles a command that FAILS; a command that HANGS
# never returns, so it never reaches `||` at all. The linux/arm64 leg did exactly
# that under QEMU: Gradle blocked on a dead socket and the build sat for 4 hours
# with the network counters frozen. Docker has no per-step timeout, so nothing
# else would ever have broken the deadlock.
#
# `|| true` still matters on its own: a compile failure at the base commit is a
# legitimate state for some PRs and must not fail the image build.
# The verdict is recorded so a hollow image is DETECTABLE afterwards. If the
# timeout fires, `|| true` lets the build continue and the image still gets
# tagged - structurally valid but with an incomplete dependency cache. Without
# this marker the two architectures would look identical from the manifest while
# behaving differently, which is exactly how a silent quality defect ships.
# Inspect with: docker run <image> cat /home/.warm_status
if timeout 1800 {gradle_test} > /tmp/warm.log 2>&1; then
  echo "warm-up: OK" > /home/.warm_status
else
  echo "warm-up: INCOMPLETE (exit $?)" > /home/.warm_status
  tail -20 /tmp/warm.log || true
fi
cat /home/.warm_status

# Leave the tree pristine so the test/fix patches apply cleanly. build/ and
# .gradle/ are gitignored, so keeping them does not dirty the tree.
git checkout -- . || true
""".format(pr=self.pr, gradle_test=GRADLE_TEST),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{gradle_test}
""".format(pr=self.pr, gradle_test=GRADLE_TEST),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{gradle_test}
""".format(pr=self.pr, gradle_test=GRADLE_TEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
{gradle_test}
""".format(pr=self.pr, gradle_test=GRADLE_TEST),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Generated from files() rather than hard-coded, so a file added there can
        # never be written into the build context yet left uncopied - which would
        # surface at build time as `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/{f.name}\n" for f in self.files())

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

"""


# Strips only `Gradle Test Executor N > `, keeping the task name. The executor
# NUMBER changes between runs, so leaving it in would make the same test look
# like a different test in each stage and every f2p comparison would be garbage.
# The task name is kept because `:dexter:test` runs both testDebugUnitTest and
# testReleaseUnitTest - collapsing them would hide a variant-specific failure.
_EXECUTOR_RE = re.compile(
    r"^Gradle Test Run\s+(?P<task>\S+)\s+>\s+Gradle Test Executor\s+\d+\s+>\s+"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_gradle_test_log(test_log: str) -> TestResult:
    """Parse `./gradlew :dexter:test` output.

    Captured verbatim from the container at base commit 11c6351c:

        Gradle Test Run :dexter:testDebugUnitTest > Gradle Test Executor 1 > \
com.karumi.dexter.MultiplePermissionsReportTest > shouldReplaceOldPermissionGrantedReportsWithTheNewOnes PASSED

    which normalises to:

        :dexter:testDebugUnitTest > com.karumi.dexter.MultiplePermissionsReportTest > shouldReplace...
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    line_re = re.compile(r"^(?P<name>.+?)\s+(?P<status>PASSED|FAILED|SKIPPED)$")

    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Task-level lines such as `> Task :dexter:test FAILED` are build
        # lifecycle events, not test cases; counting them would put names in the
        # report that can never be f2p or p2p candidates.
        if line.startswith(">"):
            continue

        match = line_re.match(line)
        if not match:
            continue

        name = _EXECUTOR_RE.sub(
            lambda m: f"{m.group('task')} > ", match.group("name").strip()
        ).strip()

        # A real test line always carries the `Class > method` separator.
        if " > " not in name:
            continue

        status = match.group("status")
        if status == "FAILED":
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif status == "PASSED":
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)
        else:  # SKIPPED
            if name not in failed_tests and name not in passed_tests:
                skipped_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("Karumi", "Dexter")
class Dexter(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DexterImageDefault(self.pr, self._config)

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
        return parse_gradle_test_log(test_log)