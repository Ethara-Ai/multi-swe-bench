import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Base images (§3 reference format)
# ---------------------------------------------------------------------------
# One shared base per JDK era. Each base:
#   * starts with `# syntax=docker/dockerfile:1.6` so DockerfileEnhancer opts
#     out (keeps FULL history at the base; the PR layer does strict hardening),
#   * carries ARG TARGETARCH / ARG REPO_URL, TZ=UTC ENV, ethara.ai LABEL,
#     `git clone "${REPO_URL}"`, and light hardening only,
#   * installs only ca-certificates (§8 audit-clean) — no network
#     interception, no extra trust injection, no mirrors.


def _libgdx_base_dockerfile(base_os: str, jdk_pkg: str) -> str:
    """Render a §3-compliant shared base Dockerfile for the given OS + JDK."""
    return f"""# syntax=docker/dockerfile:1.6
FROM {base_os}

ARG TARGETARCH
ARG REPO_URL="https://github.com/libgdx/libgdx.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="libgdx/libgdx" \\
      org.opencontainers.image.description="libgdx/libgdx Docker image" \\
      org.opencontainers.image.source="https://github.com/libgdx/libgdx" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries && \\
    echo 'Acquire::http::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \\
    echo 'Acquire::https::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \\
    for i in 1 2 3 4 5; do apt-get update && break || sleep 15; done && \\
    apt-get install -y --no-install-recommends \\
    git {jdk_pkg} curl unzip python3 ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/libgdx

WORKDIR /home/libgdx
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class LibgdxImageBaseJDK8(Image):
    """Shared base with JDK 8 for the Gradle 3-5 era.

    PRs 4914, 5029, 5073, 5207, 5698, 5701, 5839.
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
        return "ubuntu:20.04"

    def image_tag(self) -> str:
        return "base-jdk-8"

    def workdir(self) -> str:
        return "base-jdk-8"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return _libgdx_base_dockerfile("ubuntu:20.04", "openjdk-8-jdk")


class LibgdxImageBase(Image):
    """Shared base with JDK 11 for the Gradle 6-7 era.

    PRs 5976, 6889.
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

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return _libgdx_base_dockerfile("ubuntu:22.04", "openjdk-11-jdk")


class LibgdxImageBaseJDK17(Image):
    """Shared base with JDK 17 for the Gradle 8+ era.

    PRs 6792, 6853, 7069, 7533.
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

    def image_tag(self) -> str:
        return "base-jdk-17"

    def workdir(self) -> str:
        return "base-jdk-17"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return _libgdx_base_dockerfile("ubuntu:22.04", "openjdk-17-jdk")


# ---------------------------------------------------------------------------
# PR image (§4 reference format)
# ---------------------------------------------------------------------------
class LibgdxImageDefault(Image):
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
        # Era selection based on base Gradle version:
        # PRs 4914, 5029, 5073, 5207, 5698, 5701, 5839: Gradle 3-6, JDK 8
        # PRs 5976, 6889: Gradle 6.9-7.5, JDK 11
        # PRs 6792, 6853, 7069, 7533: Gradle 8+, JDK 17
        #   (6792 fix_patch upgrades AGP 7.3.1->8.1.2 + Gradle 7.5.1->8.4, needs JDK 17)
        _jdk17_prs = {6792, 6853, 7069, 7533}
        if self.pr.number in _jdk17_prs:
            return LibgdxImageBaseJDK17(self.pr, self._config)
        elif self.pr.number <= 5839:
            return LibgdxImageBaseJDK8(self.pr, self._config)
        else:
            return LibgdxImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _hardening_block(self) -> str:
        """Canonical Image._HARDENING_BLOCK with the literal base.sha baked in
        (the PR layer performs the strict, no-reward-hack scrub; §4/§9)."""
        return Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

    def _init_gradle_script(self) -> str:
        """Gradle init script: standard public repositories + test logging.

        Repos are declared in URL form (not the gradlePluginPortal()/google()
        method shortcuts) because those methods only exist on Gradle >= 4.4 /
        >= 4.0 and this dataset spans Gradle 3.1 -> 8.x. The URL form works on
        every version. Only Maven Central + the public Gradle plugin portal +
        Google's Android Maven are used, so the §8 audit stays clean; they are a
        fallback for old libgdx builds that referenced the now-defunct jcenter.

        For PR 5839 (Gradle 3.1 era, build configured at source 1.6) the
        bundle backports tests using the diamond operator, so Java source
        level 1.7 is forced — scoped to that PR only.
        """
        _needs_java7 = {5839}
        compat_block = ""
        if self.pr.number in _needs_java7:
            # Task-level override: the project's own build.gradle sets
            # sourceCompatibility AFTER init scripts run, so a project-level
            # value would be overridden. Task-level config wins.
            compat_block = textwrap.dedent("""\
                allprojects {
                    tasks.withType(JavaCompile) {
                        sourceCompatibility = '1.7'
                        targetCompatibility = '1.7'
                    }
                }

            """)
        return compat_block + textwrap.dedent("""\
            allprojects {
                buildscript {
                    repositories {
                        maven { url 'https://plugins.gradle.org/m2/' }
                        maven { url 'https://dl.google.com/dl/android/maven2/' }
                        mavenCentral()
                    }
                }

                repositories {
                    maven { url 'https://dl.google.com/dl/android/maven2/' }
                    mavenCentral()
                }

                tasks.withType(Test) {
                    testLogging {
                        events 'passed', 'failed', 'skipped'
                        showStandardStreams = false
                        exceptionFormat 'full'
                    }
                    outputs.upToDateWhen { false }
                }
            }
        """)

    def _gradle_test_cmd(self) -> str:
        """Build the Gradle test command targeting only the gdx subproject tests."""
        # Use --continue so we get full results even if some tests fail.
        # Only run :gdx:test since that's where the unit tests live.
        # --no-build-cache not supported on Gradle < 4.0 (PR 5839 uses Gradle 3.1).
        _old_gradle_prs = {5839}
        if self.pr.number in _old_gradle_prs:
            return "./gradlew :gdx:test --init-script ~/.gradle/init.gradle --max-workers 2 --continue"
        return "./gradlew :gdx:test --no-build-cache --init-script ~/.gradle/init.gradle --max-workers 2 --continue"

    def _restore_project_structure_cmd(self) -> str:
        # PRs 6792 and 7069 upgrade Gradle in fix_patch — don't restore wrapper.
        _no_restore_wrapper_prs = {6792, 7069}
        # PR 4914's fix restructures the whole multi-project layout
        # (gdx-controllers AND gdx-box2d into nested subprojects). Gradle's
        # configuration phase evaluates EVERY build file, so settings.gradle,
        # tests/ and backends/ must stay mutually consistent — keep them all
        # patched and restore only the wrapper.
        _keep_patched_layout_prs = {4914}
        if self.pr.number in _no_restore_wrapper_prs:
            restore = "git checkout HEAD -- settings.gradle backends/ tests/ 2>/dev/null || true"
        elif self.pr.number in _keep_patched_layout_prs:
            restore = "git checkout HEAD -- gradle/wrapper/ gradlew gradlew.bat 2>/dev/null || true"
        else:
            restore = "git checkout HEAD -- settings.gradle gradle/wrapper/ gradlew gradlew.bat backends/ tests/ 2>/dev/null || true"

        # Strip subprojects that cause Gradle configuration failures:
        # - tests:gdx-tests-android (requires specific AGP + Gradle version match)
        # - tests:gdx-tests-gwt (requires gwt-gradle-plugin from defunct jcenter)
        # - backends:gdx-backend-android (requires real Android SDK)
        # Remove publish.gradle apply (references projects that may not exist).
        # Fix configure() blocks in build.gradle and gradle/dist.gradle.
        strip_cmd = (
            "sed -i '/gdx-tests-android/d' settings.gradle 2>/dev/null || true && "
            "sed -i '/gdx-tests-gwt/d' settings.gradle 2>/dev/null || true && "
            "sed -i '/gdx-tests-ios/d' settings.gradle 2>/dev/null || true && "
            "sed -i '/gdx-backend-android/d' settings.gradle 2>/dev/null || true && "
            "sed -i '/gdx-controllers-android/d' settings.gradle 2>/dev/null || true && "
            "sed -i '/publish.gradle/d' build.gradle 2>/dev/null || true && "
            r"sed -i '/gdx-tests-android\|gdx-backend-android/s/configure(subprojects.*) {/configure(subprojects) {/' build.gradle 2>/dev/null || true && "
            r"sed -i '/gdx-tests-android\|gdx-backend-android/s/configure(subprojects.*) {/configure(subprojects) {/' gradle/dist.gradle 2>/dev/null || true && "
            r"sed -i '/gdx-tests-android/s/configure(subprojects.*) {/configure(subprojects) {/' tests/build.gradle 2>/dev/null || true && "
            r"sed -i '/gdx-backend-android/s/configure(subprojects.*) {/configure(subprojects) {/' backends/build.gradle 2>/dev/null || true && "
            "chmod +x gradlew 2>/dev/null || true"
        )

        return f"{restore} && {strip_cmd}"

    def _prepare_script(self) -> str:
        _jdk17_prs = {6792, 6853, 7069, 7533}
        android_sdk_block = ""
        if self.pr.number not in _jdk17_prs:
            android_sdk_block = textwrap.dedent("""\
                # Create fake Android SDK to satisfy Gradle's Android plugin configuration
                ANDROID_HOME=/home/android-sdk
                mkdir -p $ANDROID_HOME/platforms/android-27 $ANDROID_HOME/build-tools/27.0.3 $ANDROID_HOME/licenses
                touch $ANDROID_HOME/platforms/android-27/android.jar
                echo "24333f8a63b6825ea9c5514f83c2829b004d1fee" > $ANDROID_HOME/licenses/android-sdk-license
                echo "sdk.dir=$ANDROID_HOME" > local.properties
            """)

        return textwrap.dedent("""\
            #!/bin/bash
            set -e

            cd /home/{repo}
            git reset --hard
            bash /home/check_git_changes.sh
            git checkout {sha}
            bash /home/check_git_changes.sh

            # Set up Gradle init script for test logging
            mkdir -p ~/.gradle
            cat /home/init.gradle > ~/.gradle/init.gradle

            # Increase Gradle memory
            if [ -f gradle.properties ]; then
                sed -i "/^org.gradle.jvmargs=/d" gradle.properties
            fi
            echo "org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=512m" >> gradle.properties

            {android_sdk_block}
            # Pre-build to download dependencies (allow failure)
            ./gradlew :gdx:compileJava --init-script ~/.gradle/init.gradle --max-workers 2 || true

        """).format(repo=self.pr.repo, sha=self.pr.base.sha, android_sdk_block=android_sdk_block)

    def _local_properties_cmd(self) -> str:
        _jdk17_prs = {6792, 6853, 7069, 7533}
        if self.pr.number in _jdk17_prs:
            return ""
        return 'echo "sdk.dir=/home/android-sdk" > local.properties'

    def _run_script(self) -> str:
        gradle_cmd = self._gradle_test_cmd()
        restore_cmd = self._restore_project_structure_cmd()
        local_props = self._local_properties_cmd()
        return textwrap.dedent("""\
            #!/bin/bash
            set -e

            cd /home/{repo}
            git checkout -- .
            echo "org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=512m" >> gradle.properties
            {local_props}
            {restore_cmd}
            {gradle_cmd}
        """).format(repo=self.pr.repo, gradle_cmd=gradle_cmd, restore_cmd=restore_cmd, local_props=local_props)

    def _test_run_script(self) -> str:
        gradle_cmd = self._gradle_test_cmd()
        restore_cmd = self._restore_project_structure_cmd()
        local_props = self._local_properties_cmd()
        return textwrap.dedent("""\
            #!/bin/bash
            set -e

            cd /home/{repo}
            git checkout -- .
            python3 /home/strip_binary_patches.py /home/test.patch /tmp/test.patch.clean
            git apply --ignore-whitespace /tmp/test.patch.clean 2>/dev/null || {{
              echo "strict apply failed; fuzzy patch(1) fallback"
              git checkout -- .
              patch -p1 -N -f --no-backup-if-mismatch --fuzz=3 < /tmp/test.patch.clean 2>&1 | tail -15 || true
              find . -name '*.rej' -delete 2>/dev/null || true
            }}
            echo "org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=512m" >> gradle.properties
            {local_props}
            {restore_cmd}
            {gradle_cmd}
        """).format(repo=self.pr.repo, gradle_cmd=gradle_cmd, restore_cmd=restore_cmd, local_props=local_props)

    def _fix_run_script(self) -> str:
        gradle_cmd = self._gradle_test_cmd()
        restore_cmd = self._restore_project_structure_cmd()
        local_props = self._local_properties_cmd()
        return textwrap.dedent("""\
            #!/bin/bash
            set -e

            cd /home/{repo}
            git checkout -- .
            python3 /home/strip_binary_patches.py /home/test.patch /tmp/test.patch.clean
            python3 /home/strip_binary_patches.py /home/fix.patch /tmp/fix.patch.clean
            git apply --ignore-whitespace /tmp/fix.patch.clean /tmp/test.patch.clean 2>/dev/null || {{
              echo "strict combined apply failed; fuzzy patch(1) fallback"
              git checkout -- .
              patch -p1 -N -f --no-backup-if-mismatch --fuzz=3 < /tmp/fix.patch.clean 2>&1 | tail -10 || true
              patch -p1 -N -f --no-backup-if-mismatch --fuzz=3 < /tmp/test.patch.clean 2>&1 | tail -10 || true
              find . -name '*.rej' -delete 2>/dev/null || true
            }}
            echo "org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=512m" >> gradle.properties
            {local_props}
            {restore_cmd}
            {gradle_cmd}
        """).format(repo=self.pr.repo, gradle_cmd=gradle_cmd, restore_cmd=restore_cmd, local_props=local_props)

    def _strip_binary_patches_script(self) -> str:
        return textwrap.dedent("""\
            #!/usr/bin/env python3
            import re
            import sys

            infile = sys.argv[1]
            outfile = sys.argv[2]

            # Binary mode is REQUIRED: text mode's universal-newlines silently
            # rewrites CRLF->LF, corrupting hunks that touch CRLF files (the
            # jni/OIS C++ and .bat sources) -> "corrupt patch" at apply time.
            with open(infile, 'rb') as f:
                content = f.read()

            content = re.sub(
                rb'diff --git [^\\n]*\\n(?:(?:old|new|deleted|similarity|rename|index|copy)[^\\n]*\\n)*Binary files [^\\n]*differ\\n?',
                b'',
                content,
            )

            with open(outfile, 'wb') as f:
                f.write(content)
        """)

    def files(self) -> list[File]:
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
                "init.gradle",
                self._init_gradle_script(),
            ),
            File(
                ".",
                "check_git_changes.sh",
                textwrap.dedent("""\
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
                """),
            ),
            File(
                ".",
                "prepare.sh",
                self._prepare_script(),
            ),
            File(
                ".",
                "strip_binary_patches.py",
                self._strip_binary_patches_script(),
            ),
            File(
                ".",
                "run.sh",
                self._run_script(),
            ),
            File(
                ".",
                "test-run.sh",
                self._test_run_script(),
            ),
            File(
                ".",
                "fix-run.sh",
                self._fix_run_script(),
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
        hardening = self._hardening_block()

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("libgdx", "libgdx")
class Libgdx(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LibgdxImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_res = [
            # Gradle task success: "> Task :gdx:compileTestJava"
            re.compile(r"^> Task :(\S+)$"),
            re.compile(r"^> Task :(\S+) UP-TO-DATE$"),
            re.compile(r"^> Task :(\S+) FROM-CACHE$"),
            # Individual test method results
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

        for line in test_log.splitlines():
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

        # Clean up overlaps
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ============
# §11b: JSONL + registry ship together; the trajectory harness calls
# Instance.create() -> "{org}/{number_interval}", so every dash-joined bundle
# value must be a registered routing key. libgdx is single-class (Libgdx self-
# dispatches JDK era by pr.number), so all bundle keys point to Libgdx.
# Keys are data-derived from prs_in_bundle in
#   Dataset/libgdx__libgdx_lht_final.jsonl — regenerate if bundles change.
_BUNDLE_NIS_Libgdx = [
    "4914-5657-5710-5714-5722-5724-5729-5734-5741-5743-5745-5753-5757-5758-5768-5769-5771-5773-5775-5776-5778-5779-5780-5782-5787-5789-5792-5793-5804-5806-5811-5819-5821-5833-5835-5838-5847-5860-5862-5866-5873-5877-5880-5883-5887-5889-5890-5892-5895-5900-5921-5922-6009-6015-6042-6047-6056-6059-6068-6069-6074-6075-6078-6079-6082-6092-6100-6107-6109",
    "5029-5192-5197-5199-5210-5231",
    "5073-5408-5428-5556-5659-5677-5682-5683-5691-5702-5784-5901-5949-5955-6016-6101-6104-6110-6117-6118-6120-6121-6126-6130-6131-6132-6133-6135-6138-6139-6140-6141-6142-6143-6146-6147-6148-6149-6151-6152-6153-6154-6155-6156-6160-6163-6164-6165-6167-6168-6169-6170-6171-6172-6173-6174-6175-6177-6178-6179-6181-6182-6183-6185-6186-6187-6188-6193-6194-6196-6200-6201-6206-6208-6215-6216-6217-6220-6222-6225-6231-6232-6233-6234-6235-6236-6238-6240-6242-6243-6244-6245-6250-6251-6252-6253",
    "5207-6049-6211-6226-6241-6255-6257-6258-6261-6263-6264-6265-6273-6277-6278-6284-6286-6289-6290-6291-6296-6299-6300-6301-6304-6305-6307-6308-6312-6316-6320-6321-6323-6329-6332-6337-6338-6340",
    "5698-6229-6369-6386-6421-6425-6456-6463-6467-6469-6472-6489-6494-6501-6505-6506-6507-6508-6510-6517-6520-6521-6524-6527-6528-6534-6536-6539-6543-6545-6548-6549-6550-6551-6555-6563-6564-6569-6571-6572-6573-6576-6577-6579-6581-6582-6586-6587-6590-6591-6595-6600-6601-6603-6604-6605-6609-6610-6611-6615-6617-6619-6627-6637-6640-6644-6648-6649-6651-6652-6656-6659-6661-6665-6669-6672-6678-6680-6682-6683-6692-6693-6696-6698-6703-6706-6707-6718-6719-6720-6721-6722-6723-6725-6727-6730-6731-6732-6733-6734-6741-6750-6752-6753-6754-6756-6757-6758-6760-6761-6762-6765-6767-6772-6773-6774-6776-6778-6786-6788-6793-6794-6796-6798-6801-6802-6803-6805-6812-6815-6818-6819-6822-6823-6824-6825-6826-6828-6829-6830-6831-6833-6836-6837-6841-6842-6843-6846-6849-6850-6851-6852-6855-6857-6863",
    "5701-6262-6303-6306-6309-6325-6327-6331-6336-6341-6342-6346-6348-6350-6351-6355-6357-6358-6360-6365-6368-6370-6373-6382",
    "5839-6366-6379-6388-6389-6391-6396-6398-6399-6400-6403-6404-6408-6409-6412-6422-6423-6424-6428-6434-6435-6439-6443-6445-6451-6455-6457-6460-6471-6475-6476-6484-6495-6496-6498",
    "5976-6274-6394-6431-6522-6681-6694-6714-6717-6777-6811-6821-6827-6838-6847-6854-6859-6868-6871-6873-6876-6877-6879-6881-6887-6892-6893-6894-6898-6899-6905-6913-6914-6915-6917-6919-6920-6924-6927-6928-6930-6931-6936-6940-6941-6945-6946-6947-6954-6959-6963-6965-6966-6968-6970-6972-6973-6975-6976-6978-6979-6980-6993-6994-6995-7000-7001-7003-7005-7014-7015-7017-7018-7020-7028-7030-7032-7034-7035-7036-7037-7040-7041-7052-7054-7063-7064-7068-7070-7073-7077-7101-7106-7109-7112-7117-7119-7120-7121-7122-7124-7126-7127-7128-7129-7131-7132-7134-7138-7145-7146-7147-7149-7153-7154-7156-7157-7160-7161-7162-7165-7166-7167-7171-7173",
    "6792-6885-6886-6939-7004-7096-7185-7222-7243-7246-7247-7250-7254-7255-7270-7271-7274-7275-7276-7278-7280-7284-7286-7287-7288-7302-7305-7306-7309-7310-7311-7312-7315-7317-7320-7322-7327-7332-7344-7346-7347-7350-7357-7358-7359-7361-7362-7364-7366-7371-7372-7374-7375-7376-7377-7378-7379-7382-7383-7384-7388-7389-7391-7393-7395-7399-7401-7410-7412-7416-7428-7429-7435-7439-7440-7442-7443-7444-7451-7455-7457-7458-7462-7466-7475",
    "6853-7076-7080-7135-7398-7424-7467-7469-7471-7483-7484-7486-7497-7499-7500-7505-7510-7511-7512-7519-7522-7524-7530-7532-7534-7541-7543-7544-7553-7555-7557-7559",
    "6889-7044-7074-7140-7164-7172-7176-7178-7179-7181-7184-7192-7193-7195-7196-7207-7213-7215-7217-7225-7229-7231-7233-7236-7237-7238-7239-7242-7244-7249-7251-7259-7260-7265-7266",
    "7069-7415-7426-7452-7453-7456-7474-7498-7515-7523-7525-7526-7540-7560-7562-7567-7569-7570-7571-7575-7576-7578-7579-7585-7586-7590-7592-7595-7596-7597-7598-7600-7601-7602-7604-7605-7606-7607-7608-7612-7614-7615-7616-7619-7625-7629-7631-7632-7633-7641-7643",
    "7533-7603-7635-7637-7640-7645-7648-7650-7652-7653-7657-7658-7659-7661-7662-7664-7667-7669-7673-7674-7675-7677-7678-7679-7680-7682-7683-7684-7694-7698-7700-7706-7708-7710-7711-7714",
]
for _ni in _BUNDLE_NIS_Libgdx:
    Instance.register("libgdx", _ni)(Libgdx)
