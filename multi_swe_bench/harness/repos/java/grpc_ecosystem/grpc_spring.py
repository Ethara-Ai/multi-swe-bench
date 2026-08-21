"""grpc-ecosystem/grpc-spring -- Spring Boot starter module for gRPC.

Two-tier config in the house style (``*ImageBase`` -> ``*ImageDefault`` ->
``Instance``), matching e.g. ``java/roc_streaming/roc_java.py``.

Toolchain, read off the repo at this dataset's base commit
(3236156, ``grpc-spring-boot-starter`` 2.13.0-SNAPSHOT):

* ``gradle/wrapper/gradle-wrapper.properties`` -> **Gradle 7.0** (wrapper, so
  the distribution is downloaded on first invocation and cached in the image by
  ``prepare.sh``).
* ``build.gradle`` -> ``java { toolchain { languageVersion = of(8) } }`` plus
  ``sourceCompatibility/targetCompatibility = 1.8``. Both CI workflows
  (``.github/workflows/{build-master,pull-request}.yml``) run a
  ``java: ['8', '11']`` matrix. The image therefore ships **JDK 8** and runs
  Gradle on it: Gradle 7.0's minimum is Java 8, and a JDK-8 daemon lets the
  toolchain resolve to the current JVM, so no toolchain auto-provisioning (a
  network download of a second JDK) is ever attempted.
* ``test { useJUnitPlatform() }`` -> **JUnit 5**, run through Gradle.

Only three of the twelve subprojects carry test sources at this commit --
``grpc-client-spring-boot-autoconfigure`` (7 files),
``grpc-server-spring-boot-autoconfigure`` (16) and ``tests`` (91, where both of
this PR's new test classes land). The ``examples:*`` subprojects have none, so a
plain ``./gradlew test`` is exactly the graded surface and nothing more.

Results are read from the **JUnit XML reports**, not the console. Gradle's
``testLogging`` block here emits ``passed``/``skipped``/``failed`` events, but
the rendered lines are locale- and terminal-dependent and carry no stable
module qualifier; ``<module>/build/test-results/test/TEST-*.xml`` is written
unconditionally and gives a ``classname`` + ``name`` pair that is identical in
all three stages. run.sh / test-run.sh / fix-run.sh cat those files between
explicit markers and ``parse_log`` reads that. Same approach as the other
XML-based configs in this tree (see ``java/roc_streaming/roc_java.py``).

Two repo-specific hazards this config defuses:

1. **Gradle build cache.** The repo's own ``gradle.properties`` sets
   ``org.gradle.caching=true`` and ``org.gradle.vfs.watch=true``. A cached/
   ``UP-TO-DATE`` ``test`` task between stages would leave the *previous*
   stage's XML on disk and make the f2p comparison meaningless. The image ships
   ``/root/.gradle/gradle.properties`` -- which takes precedence over the
   project file -- turning both off, and every run script wipes
   ``*/build/test-results`` before invoking Gradle.

2. **Detached HEAD.** ``build.gradle`` evaluates ``versioning.info.commit``
   (net.nemerosa.versioning) at configuration time, i.e. on *every* Gradle
   invocation. The base image's history-hardening block deletes every ref, so
   HEAD is detached with no branches at all. ``prepare.sh`` re-creates a local
   ``master`` at the base commit -- it points at the commit that is already
   HEAD, so no extra history is reachable and nothing leaks -- to keep the
   plugin on the same footing it has in CI.

The base image is tagged ``base-pr-<n>``, not a plain ``base``. The tag carries
the PR number even though every layer in it is PR-independent, because the image
is NOT actually PR-independent: ``DockerfileEnhancer`` rewrites the clone below
into ``git checkout ${BASE_COMMIT}`` followed by the history-scrub block, which
prunes every commit not reachable from that HEAD. The resulting image is
therefore specific to ONE base commit. The harness dedupes images in a set keyed
on ``image_full_name()`` (build_dataset.py, run_mode_image), so a constant
``base`` tag would collapse every PR of this repo onto whichever commit was
built first -- and a later PR's ``git checkout`` would then fail against history
that ``git gc --prune=now`` had already destroyed. Encoding the PR number keeps
each base honest at the cost of rebuilding the apt layers per PR (cheap:
Docker's layer cache hits everything above the clone). It also satisfies the
Dockerfile QC contract, which requires the PR layer's ``FROM`` to name
``mswebench/<org>_m_<repo>:base-pr-<n>`` so the base and PR halves can be checked
as a matched pair.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Emitted by run.sh / test-run.sh / fix-run.sh around the concatenated JUnit XML
# so parse_log never has to guess where the Gradle chatter ends.
BEGIN_MARKER = "===== BEGIN TEST RESULTS ====="
END_MARKER = "===== END TEST RESULTS ====="

# --- Gradle distribution, pre-seeded into the wrapper's cache -----------------
#
# Left to itself, `./gradlew` downloads this zip on first use. That works on
# amd64 and FAILS DETERMINISTICALLY on emulated arm64: under QEMU the JVM cannot
# drain the TLS socket fast enough for a ~107 MB transfer, and the wrapper's own
# read timeout fires --
#
#     javax.net.ssl.SSLException: Read timed out
#       at Downloading https://services.gradle.org/distributions/gradle-7.0-bin.zip
#
# -- observed on three consecutive attempts (146s / 1218s / 1614s) during a
# `--platform linux/amd64,linux/arm64` build. Not a flake: the emulated JVM is
# simply too slow, so retrying can never help.
#
# Fetching it with curl instead sidesteps the whole problem -- curl is a small C
# program, cheap under emulation, and has no equivalent short read timeout.
#
# The wrapper finds a pre-seeded distribution at
#     $GRADLE_USER_HOME/wrapper/dists/<zip-name-minus-.zip>/<hash>/<dist-root>/
# and only trusts it when the sibling `<zip-name>.ok` marker exists (see
# Install.java in the wrapper). <hash> is Gradle's own
# PathAssembler.getHash(): base36 of the MD5 of the distribution URL string.
# Recompute it if _GRADLE_DIST_URL ever changes:
#
#     python -c "import hashlib;n=int.from_bytes(hashlib.md5(URL.encode()).digest(),'big');s='';\
#     exec(\"while n:\\n n,r=divmod(n,36);s='0123456789abcdefghijklmnopqrstuvwxyz'[r]+s\");print(s)"
#
# The value below was cross-checked BOTH ways: computed from the formula, and
# read back out of a successfully-built amd64 image whose wrapper had populated
# the cache itself. They agree.
#
# NOTE: this is what makes `curl` and `unzip` load-bearing in the apt layer.
# They look like trimmable bloat until you read this block.
_GRADLE_DIST_URL = "https://services.gradle.org/distributions/gradle-7.0-bin.zip"
_GRADLE_DIST_BASE = "gradle-7.0-bin"  # zip filename minus ".zip"
_GRADLE_DIST_HASH = "2p9ebqfz6ilrfozi676ogco7n"
_GRADLE_DIST_ROOT = "gradle-7.0"  # directory the zip unpacks to

# Read-only Maven Central mirror, tried before Central itself.
# repo.maven.apache.org answers 429 to build hosts under load, and a rate-limited
# dependency resolution kills the image build outright. Same mirror the
# roc_streaming/qulice configs use.
_CENTRAL_MIRROR = "https://maven-central.storage-download.googleapis.com/maven2/"

# grgit 4.0.1 is a hard blocker without this substitution, and it is not a
# flake -- it fails identically on every attempt, on every machine, forever.
#
# `build.gradle` applies `net.nemerosa.versioning` 2.14.0, whose only transitive
# dependency is `org.ajoberstar.grgit:grgit-core:4.0.1`. That artifact was
# published ONLY to JCenter/Bintray, which was shut down in 2021 -- after this
# PR's base commit, which is why CI was green at the time and is unreproducible
# now. It is on no surviving repository: Central 404s (its oldest published
# grgit-core is 4.1.1) and plugins.gradle.org/m2 just 303-redirects to that same
# Central 404. Since the plugin is applied to the ROOT project, resolution of
# the `:classpath` configuration fails at CONFIGURATION time, so *every* Gradle
# invocation dies before any task runs -- warm-up, and all three graded stages.
#
# 4.1.1 is the minimum bump that exists on Central and stays inside grgit's 4.x
# major, and the plugin only ever calls `Grgit.open`/`describe`/`branch`, which
# are unchanged across that range. Substituting the transitive dep is deliberately
# narrower than bumping the plugin itself: the build keeps the exact
# `net.nemerosa.versioning:2.14.0` the commit asked for, and no file in the
# working tree is touched, so `check_git_changes.sh` stays green.
#
# The substitution is pinned to the exact group+version so it can only ever fire
# on this one dead coordinate -- it cannot silently retarget some future grgit.
# It is installed on both the buildscript and the project configurations; only
# the buildscript one fires today (grgit is not a compile dependency), and the
# project-level copy is a verified no-op kept as a backstop.
#
# The Gradle plugin MARKERS (spring-boot, spotless, protobuf, versioning,
# gradle-lombok, ben-manes.versions, spring dependency-management) live on the
# Gradle plugin portal, while their transitive dependencies are Central
# artifacts -- so both repositories have to stay in the list, mirror first.
# Declaring pluginManagement.repositories at all REPLACES the implicit
# gradlePluginPortal() default, hence the explicit entry.
_INIT_GRADLE = f"""\
def centralMirror = "{_CENTRAL_MIRROR}"

settingsEvaluated {{ settings ->
    settings.pluginManagement {{
        repositories {{
            maven {{ url centralMirror }}
            gradlePluginPortal()
            mavenCentral()
        }}
    }}
}}

allprojects {{
    buildscript {{
        repositories {{
            maven {{ url centralMirror }}
            gradlePluginPortal()
            mavenCentral()
        }}
        configurations.all {{
            resolutionStrategy.eachDependency {{ details ->
                if (details.requested.group == 'org.ajoberstar.grgit'
                        && details.requested.version == '4.0.1') {{
                    details.useVersion '4.1.1'
                    details.because 'grgit 4.0.1 was published only to JCenter, which is shut down'
                }}
            }}
        }}
    }}
    repositories {{
        maven {{ url centralMirror }}
        mavenCentral()
    }}
    configurations.all {{
        resolutionStrategy.eachDependency {{ details ->
            if (details.requested.group == 'org.ajoberstar.grgit'
                    && details.requested.version == '4.0.1') {{
                details.useVersion '4.1.1'
                details.because 'grgit 4.0.1 was published only to JCenter, which is shut down'
            }}
        }}
    }}
}}
"""

# Lives at /root/.gradle, which outranks the project's own gradle.properties.
#
# caching=false / vfs.watch=false deliberately override the repo's settings: the
# build cache can make `test` UP-TO-DATE across stages (see module docstring),
# and Gradle 7.0's file-system watching needs inotify headroom the container
# does not reliably have.
#
# The daemon is disabled here as well as on every command line, and the heap /
# metaspace bumps keep the Spring Boot context-loading tests off the default
# ceiling.
_GRADLE_PROPERTIES = """\
org.gradle.daemon=false
org.gradle.caching=false
org.gradle.vfs.watch=false
org.gradle.parallel=false
org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=768m -Dfile.encoding=UTF-8
"""


def _junit_xml_parse(test_log: str) -> TestResult:
    """Parse concatenated JUnit ``TEST-*.xml`` surefire-style reports.

    A ``<testcase .../>`` self-closing element is a pass; a body containing
    ``<failure``/``<error`` is a failure, ``<skipped`` is a skip. Test ids are
    ``<classname>.<name>``, which is what Gradle writes for JUnit 5 and is
    byte-identical across the run/test/fix stages -- no timing or count
    metadata is part of the id.
    """
    clean = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
    name_re = re.compile(r'\bname="([^"]*)"')
    classname_re = re.compile(r'\bclassname="([^"]*)"')

    for m in testcase_re.finditer(clean):
        nm = name_re.search(m.group(1))
        cn = classname_re.search(m.group(1))
        if not nm or not cn:
            continue
        tid = f"{cn.group(1)}.{nm.group(1)}"
        inner = m.group(3) or ""
        if m.group(2) == "/>":
            passed.add(tid)
        elif "<failure" in inner or "<error" in inner:
            failed.add(tid)
        elif "<skipped" in inner:
            skipped.add(tid)
        else:
            passed.add(tid)

    # A test that appears both ways across reports is a failure, not a pass.
    passed -= failed
    passed -= skipped
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


class GrpcSpringImageBase(Image):
    """Repo-level base image, tag ``base-pr-<n>``.

    Carries the JDK 8 toolchain, git/curl, and the Gradle configuration that the
    per-PR image then uses to warm its caches. Those layers are PR-independent,
    but the checked-out+pruned repo underneath them is not -- see the module
    docstring for why the tag must carry the PR number anyway.
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

    def dependency(self) -> str:
        # JDK 8 to match `JavaLanguageVersion.of(8)` and the primary CI matrix
        # entry. Official multi-arch image (linux/amd64 + linux/arm64), Ubuntu
        # 22.04 based, so the enhancer's ca-certificates symlinks land where it
        # expects them.
        return "eclipse-temurin:8-jdk-jammy"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        # Keep in step with image_tag(): build_dataset.build_image() names the
        # build-context directory from workdir(), so a mismatch would leave the
        # rendered Dockerfile in images/base/ while the tag says base-pr-<n>.
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "init.gradle", _INIT_GRADLE),
            File(".", "gradle.properties", _GRADLE_PROPERTIES),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Keep the repo clone LAST: DockerfileEnhancer._standardize_repo_fetch
        # rewrites this exact line into clone + `git checkout ${BASE_COMMIT}` +
        # the git-history hardening block + CMD, so anything emitted after it
        # would land after that CMD.
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        global_env = self.global_env
        global_env_block = f"\n{global_env}\n" if global_env.strip() else ""

        return f"""FROM {image_name}
{global_env_block}
# Only LC_ALL. DEBIAN_FRONTEND, LANG and TZ are already set by
# DockerfileEnhancer._ENV_BLOCK with these same values -- repeating them here
# would leave the rendered Dockerfile carrying two ENV blocks a reader has to
# diff before trusting that neither overrides the other.
ENV LC_ALL=C.UTF-8

WORKDIR /home/

# git for the clone/checkout/patch flow, curl+ca-certificates for the Gradle
# wrapper download, unzip for the wrapper distribution, procps because the
# Gradle launcher shells out to `ps` when probing for a running daemon.
# No arch-specific sources or binary downloads -- everything below resolves
# from the distro archive for whatever TARGETARCH is being built.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates \\
        curl \\
        git \\
        wget \\
        unzip \\
        procps \\
    && rm -rf /var/lib/apt/lists/*

# eclipse-temurin already exports JAVA_HOME=/opt/java/openjdk; assert it so a
# future base-image bump that moves the JDK fails here rather than inside an
# opaque Gradle task.
RUN test -x "${{JAVA_HOME}}/bin/java" && java -version 2>&1 | grep -q '"1\\.8'

# Gradle honours GRADLE_USER_HOME/gradle.properties over the project's own copy,
# and applies every init script found in GRADLE_USER_HOME/init.d or named
# init.gradle. See the module docstring for why both files exist.
ENV GRADLE_USER_HOME=/root/.gradle
COPY init.gradle /root/.gradle/init.gradle
COPY gradle.properties /root/.gradle/gradle.properties

# Pre-seed the Gradle distribution so `./gradlew` never downloads it itself.
# This is what makes the arm64 (QEMU-emulated) build viable -- see the
# _GRADLE_DIST_* block in this module for the failure it prevents and how the
# hash directory name is derived.
#
# `set -eux` + the final `test -x` mean a bad URL, a truncated download or a
# changed archive layout fails the BASE build loudly, rather than silently
# leaving the wrapper to fall back to its own (arm64-fatal) download path.
RUN set -eux; \\
    dist_dir="${{GRADLE_USER_HOME}}/wrapper/dists/{_GRADLE_DIST_BASE}/{_GRADLE_DIST_HASH}"; \\
    mkdir -p "$dist_dir"; \\
    curl -fsSL --retry 5 --retry-delay 5 --retry-connrefused \\
        -o "$dist_dir/{_GRADLE_DIST_BASE}.zip" "{_GRADLE_DIST_URL}"; \\
    unzip -q "$dist_dir/{_GRADLE_DIST_BASE}.zip" -d "$dist_dir"; \\
    touch "$dist_dir/{_GRADLE_DIST_BASE}.zip.ok"; \\
    test -x "$dist_dir/{_GRADLE_DIST_ROOT}/bin/gradle"

{code}

{self.clear_env}

"""


class GrpcSpringImageDefault(Image):
    """Per-PR image, tag ``pr-<n>``: checks out the base commit and warms the
    Gradle distribution, dependency cache and compiled classes so
    run/test-run/fix-run only pay for the test execution itself."""

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
        return GrpcSpringImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_cmd(self) -> str:
        """Run the graded tests and dump the JUnit XML between the markers.

        Shared verbatim by run.sh / test-run.sh / fix-run.sh -- one source, so
        the three stages can never drift onto different test commands and make
        the f2p comparison meaningless.

        `--continue` keeps sibling modules going past a failing one, which
        matters here: with test.patch applied but fix.patch absent the `tests`
        module fails to COMPILE (the new classes reference `@GrpcClientBean`,
        which only fix.patch introduces), and without `--continue` the two
        autoconfigure modules would never run.

        The gradle exit status is CAPTURED rather than discarded: a red test run
        is the graded signal and must not abort the script before the reports
        are emitted, but `|| true` would also swallow a runner that never
        started. `rc` records it, the reports are dumped, then the real status
        is re-raised at the end. Stale reports are cleared first so a partial
        rerun cannot resurrect a previous stage's verdict.
        """
        repo = self.pr.repo
        return (
            f"export CI=true\n"
            f"cd /home/{repo}\n"
            f"find /home/{repo} -type d -name test-results -prune -exec rm -rf {{}} + 2>/dev/null || true\n"
            f"rc=0\n"
            f"./gradlew test --continue --no-daemon --stacktrace || rc=$?\n"
            f"echo '{BEGIN_MARKER}'\n"
            f"find /home/{repo} -path '*/build/test-results/test/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null\n"
            f"echo '{END_MARKER}'\n"
            f"exit $rc\n"
        )

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_cmd = self._test_cmd()

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
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
# `build.gradle` reads `versioning.info.commit` at configuration time; the base
# image's history-hardening block leaves HEAD detached with no refs at all, so
# re-create a local branch AT THE SAME COMMIT for net.nemerosa.versioning to
# stand on. Nothing new becomes reachable -- master points at HEAD.
git checkout -B master {self.pr.base.sha}
bash /home/check_git_changes.sh

# CI does this too (`Grant execute permission for gradlew`); the git index bit
# survives a clone, but a copied working tree would not carry it.
chmod +x gradlew

# Warm the whole chain at this exact commit: the Gradle 7.0 distribution, the
# plugin classpath, the Spring/gRPC dependency graph, protoc + the grpc-java
# protoc plugin, the generated stubs and both main and test classes.
#
# `|| true` on the warm-up is required, not sloppiness: the Gradle plugin portal
# and Maven Central both answer 429 under load, and an unlucky request must not
# poison the image. Everything this step does is re-done on demand by the run
# scripts, which have the network available too -- a cold cache costs time, not
# correctness.
#
# But `|| true` also means a PERMANENTLY broken build (an unresolvable
# dependency, not a flake) still yields a green image that only detonates later
# inside the graded stages, where it reads as "0 tests" rather than "broken".
# So the outcome is recorded and, on failure, shouted -- the image still builds,
# but `docker build` output and /home/prepare_warmup_failed carry the verdict.
warm_ok=0
for attempt in 1 2 3; do
    if ./gradlew classes testClasses --no-daemon --stacktrace; then
        warm_ok=1
        break
    fi
    echo "prepare.sh: gradle warm-up attempt $attempt failed, retrying in 30s" >&2
    sleep 30
done || true

if [ "$warm_ok" != 1 ]; then
    touch /home/prepare_warmup_failed
    echo "############################################################" >&2
    echo "prepare.sh: WARNING -- gradle warm-up FAILED all 3 attempts." >&2
    echo "  The image is still being built, but if this was not a" >&2
    echo "  transient 429 the graded stages will produce 0 tests and" >&2
    echo "  the report will be rejected. Read the failure above before" >&2
    echo "  trusting any run/test/fix result from this image." >&2
    echo "############################################################" >&2
fi

# Leave no reports behind: `test` is never run here, but a failed warm-up can
# still have produced a partial `test-results` tree, and no stage may inherit it.
find /home/{repo} -type d -name test-results -prune -exec rm -rf {{}} + 2>/dev/null || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

{test_cmd}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Error: failed to apply test.patch" >&2; exit 1; }}

{test_cmd}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Error: failed to apply test.patch + fix.patch" >&2; exit 1; }}

{test_cmd}""",
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


@Instance.register("grpc-ecosystem", "grpc-spring")
class GrpcSpring(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GrpcSpringImageDefault(self.pr, self._config)

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
        return _junit_xml_parse(test_log)
