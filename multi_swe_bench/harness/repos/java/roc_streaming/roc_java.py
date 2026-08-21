"""roc-streaming/roc-java -- JNI bindings for Roc Toolkit.

Two-tier config in the house style (``*ImageBase`` -> ``*ImageDefault`` ->
``Instance``).

The graded tests are plain JUnit 5 tests, but they exercise the JNI layer, so a
test run needs the FULL native chain in place:

    libroc (C library, built from roc-toolkit with scons)
      -> libroc_jni.so (built by CMake/ninja via the repo's `cmake-library`
         included build, linked against libroc with a plain `-lroc`)
      -> copied into <repo>/libs by the `copyNativeDebugDeps` task, which
         `test` dependsOn, with java.library.path pointed at it.

This mirrors the repo's own CI (`.github/workflows/build.yaml` ->
`scripts/linux/{install_dependencies,build_roc,build_bindings}.sh`), with two
deliberate deviations:

1. **roc-toolkit is pinned to v0.2.4** instead of tracking main. The bindings'
   compatibility rule (README "Versioning") is major-equal / minor-at-least, so
   today's roc-toolkit (0.4.x+) will not compile against the 2023-era JNI
   sources. v0.2.4 was tagged 2023-05-13T20:44Z -- fourteen minutes after this
   PR's base commit (3c205e8, 2023-05-13T20:30Z) -- so it is exactly the libroc
   that CI would have picked up at the time.

2. **Results are read from the JUnit XML reports**, not from the console. The
   build applies the `com.adarshr.test-logger` plugin with the `mocha` theme,
   whose per-test lines are unicode tick/cross glyphs -- fragile to parse and
   dependent on terminal encoding. `build/test-results/test/TEST-*.xml` is
   written regardless of theme, so run.sh/test-run.sh/fix-run.sh cat it between
   explicit markers and `parse_log` reads that. Same approach as the other
   XML-based configs in this tree (see e.g. java/yegor256/qulice.py).

Note on CMake args: the repo's `cmake-library` plugin joins all `-D` arguments
into a SINGLE argv element (``String.join(" ", ...)`` in CMake.java), so
`-DROC_INCLUDE_PATH=...` / `-DROC_LIBRARY_PATH=...` never actually reach CMake
as separate defines. CMakeLists.txt therefore falls through to its
`target_link_libraries(roc_jni -lroc)` branch -- which is why libroc must be
installed into the default linker/include prefix (scons installs to /usr on
Linux) and `ldconfig` must be run. Do not try to "fix" this by passing those properties; CI doesn't
either, and the fallback is the path that works.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Contemporaneous libroc for this PR's base commit. See module docstring.
ROC_TOOLKIT_TAG = "v0.2.4"

# Emitted by run.sh / test-run.sh / fix-run.sh around the concatenated JUnit XML
# so parse_log never has to guess where the build chatter ends.
BEGIN_MARKER = "===== BEGIN TEST RESULTS ====="
END_MARKER = "===== END TEST RESULTS ====="

# Read-only Maven Central mirror, tried before Central itself. repo.maven.apache.org
# answers 429 to build hosts under load, and a rate-limited plugin-classpath
# resolution kills the image build outright (observed on jansi:1.18, pulled in
# transitively by the test-logger plugin). Same mirror the qulice config uses.
_CENTRAL_MIRROR = "https://maven-central.storage-download.googleapis.com/maven2/"

# The `com.adarshr.test-logger` plugin MARKER lives only on the Gradle plugin
# portal (it is not on the Central mirror), while its transitive dependencies are
# Central artifacts -- so both repositories have to stay in the list, mirror
# first. Declaring pluginManagement.repositories at all REPLACES the implicit
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
    }}
    repositories {{
        maven {{ url centralMirror }}
        mavenCentral()
    }}
}}
"""

# Lives at /root/.gradle so it applies to the root build AND the `cmake-library`
# included build. The daemon is already disabled on every command line; the heap
# and metaspace bumps keep the javadoc/jacoco tasks off the default ceiling.
_GRADLE_PROPERTIES = """\
org.gradle.daemon=false
org.gradle.jvmargs=-Xmx2g -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8
org.gradle.parallel=false
"""


def _junit_xml_parse(test_log: str) -> TestResult:
    """Parse concatenated JUnit `TEST-*.xml` surefire-style reports.

    A `<testcase .../>` self-closing element is a pass; a body containing
    `<failure`/`<error` is a failure, `<skipped` is a skip.
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


class RocJavaImageBase(Image):
    """Repo-level base image, tag ``base-pr-<n>``.

    Carries everything expensive: the apt toolchain, and roc-toolkit v0.2.4
    compiled with scons (bundling OpenFEC from source) and installed to the
    default /usr prefix so the JNI layer's `-lroc` resolves.

    The tag carries the PR number even though the apt/roc-toolkit layers are
    PR-independent. That is deliberate: ``DockerfileEnhancer`` rewrites the
    clone below into ``git checkout ${BASE_COMMIT}`` followed by the
    history-scrub block, which prunes every commit not reachable from that
    HEAD. The resulting image is therefore specific to ONE base commit. The
    harness dedupes images in a set keyed on ``image_full_name()``
    (build_dataset.py, run_mode_image), so a constant ``base`` tag would
    collapse every PR of this repo onto whichever commit was built first --
    and a later PR's ``git checkout`` would then fail against history that
    ``git gc --prune=now`` had already destroyed. Encoding the PR number keeps
    each base honest at the cost of rebuilding the apt layers per PR (cheap:
    Docker's layer cache hits everything above the clone).
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        # Keep in step with image_tag(): build_dataset.build_image() names the
        # build-context directory from workdir(), so a mismatch would leave the
        # rendered Dockerfile in images/base/ while the tag says base-pr-<n>.
        return self.image_tag()

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
# just made the rendered Dockerfile carry two ENV blocks a reader has to
# diff before trusting neither overrides the other.
ENV LC_ALL=C.UTF-8

WORKDIR /home/

# Toolchain: JDK 11 (the repo targets Java 8 bytecode but Gradle 6.4 refuses
# JDK 14+, and CI's primary desktop matrix entry is java 11), plus the exact
# native dependency set from scripts/linux/install_dependencies.sh. ninja-build
# backs `-Dgenerator=ninja`; cmake drives the roc_jni build.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates \\
        curl \\
        git \\
        wget \\
        unzip \\
        openjdk-11-jdk \\
        g++ \\
        pkg-config \\
        scons \\
        ragel \\
        gengetopt \\
        libuv1-dev \\
        libunwind-dev \\
        libpulse-dev \\
        libspeexdsp-dev \\
        libsox-dev \\
        libcpputest-dev \\
        libssl-dev \\
        libtool \\
        intltool \\
        autoconf \\
        automake \\
        make \\
        cmake \\
        ninja-build \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-11
ENV JAVA_HOME=/usr/lib/jvm/java-11
ENV PATH=${{JAVA_HOME}}/bin:${{PATH}}

# libroc, pinned to the release contemporaneous with this PR's base commit.
# --disable-tools skips roc-send/roc-recv/roc-conv: the bindings link the
# library only, and the tools roughly double the build time.
RUN git clone --recurse-submodules --depth 1 --branch {ROC_TOOLKIT_TAG} \\
        https://github.com/roc-streaming/roc-toolkit.git /tmp/roc && \\
    scons -C /tmp/roc -Q --compiler=gcc --build-3rdparty=openfec --disable-tools && \\
    scons -C /tmp/roc -Q --compiler=gcc --build-3rdparty=openfec --disable-tools install && \\
    ldconfig && \\
    rm -rf /tmp/roc

# Fail the base build loudly here rather than leaving a broken image that only
# blows up later inside a gradle task. roc-toolkit's scons install defaults to
# the /usr prefix on Linux, but accept /usr/local too so a prefix change
# upstream doesn't turn into a false negative.
RUN ldconfig -p | grep -q 'libroc\\.so' && \\
    (test -f /usr/include/roc/config.h || test -f /usr/local/include/roc/config.h)

COPY init.gradle /root/.gradle/init.gradle
COPY gradle.properties /root/.gradle/gradle.properties

{code}

{self.clear_env}

"""


class RocJavaImageDefault(Image):
    """Per-PR image, tag ``pr-<n>``: checks out the base commit and warms the
    whole gradle + cmake chain so run/test-run/fix-run only pay for the test
    execution itself."""

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
        return RocJavaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_cmd(self) -> str:
        """Run the graded tests and dump the JUnit XML between the markers.

        Shared verbatim by run.sh / test-run.sh / fix-run.sh -- one source, so
        the three stages can never drift onto different test commands and make
        the f2p comparison meaningless.

        `--continue` keeps sibling tasks going past a failing one. The gradle
        exit status is CAPTURED rather than discarded: a red test run is the
        graded signal and must not abort the script before the reports are
        emitted, but `|| true` would also swallow a runner that never started.
        `rc` records it, the reports are dumped, then the real status is
        re-raised at the end. Stale reports are cleared first so a partial
        rerun cannot resurrect a previous phase's verdict.
        """
        repo = self.pr.repo
        return (
            f"export CI=true\n"
            f"cd /home/{repo}\n"
            f"rm -rf build/test-results build/reports\n"
            f"rc=0\n"
            f"./gradlew test --continue --no-daemon -Dgenerator=ninja || rc=$?\n"
            f"echo '{BEGIN_MARKER}'\n"
            f"find /home/{repo} -path '*/test-results/test/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null\n"
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
bash /home/check_git_changes.sh

# Warm the whole chain at this exact commit: the gradle 6.4 distribution, the
# plugin/test dependencies, the `cmake-library` included build, and the debug
# roc_jni cmake variant that `test` depends on.
#
# This step must SUCCEED -- no `|| true`. It is the only place that proves the
# native chain actually compiles; swallowing a failure here yields a green image
# with a cold cache whose eval then fails opaquely at run time.
#
# The retry is not superstition: the plugin portal and Maven Central both answer
# 429 under load, and a single unlucky request would otherwise poison the image.
for attempt in 1 2 3; do
    if ./gradlew classes testClasses copyNativeDebugDeps --no-daemon -Dgenerator=ninja; then
        break
    fi
    if [ "$attempt" = 3 ]; then
        echo "prepare.sh: gradle warm failed after 3 attempts" >&2
        exit 1
    fi
    echo "prepare.sh: gradle warm attempt $attempt failed, retrying in 30s" >&2
    sleep 30
done

# Second pass over the rest of the task graph (release native variant, jar,
# javadoc, jacoco). Tests may legitimately fail at the base commit -- that is
# the graded signal, not a build error -- so this one tolerates failure, and the
# reports it leaves behind are wiped so no phase can inherit them.
./gradlew build --continue --no-daemon -Dgenerator=ninja || true
rm -rf build/test-results build/reports
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


@Instance.register("roc-streaming", "roc-java")
class RocJava(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RocJavaImageDefault(self.pr, self._config)

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
