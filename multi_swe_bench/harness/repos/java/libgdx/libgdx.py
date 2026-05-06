import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class LibgdxImageBase(Image):
    """Base image with JDK 11 for Gradle 6-7 era (most PRs)."""

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::http::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::https::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \
    for i in 1 2 3 4 5; do apt-get update && break || sleep 15; done && \
    apt-get install -y git openjdk-11-jdk curl unzip python3

{code}

{copy_commands}

{self.clear_env}

"""


class LibgdxImageBaseJDK8(Image):
    """Base image with JDK 8 for Gradle 3-5 era (PRs 4914, 5029, 5073, 5207, 5701, 5839)."""

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
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::http::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::https::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \
    for i in 1 2 3 4 5; do apt-get update && break || sleep 15; done && \
    apt-get install -y git openjdk-8-jdk curl unzip python3

{code}

{copy_commands}

{self.clear_env}

"""


class LibgdxImageBaseJDK17(Image):
    """Base image with JDK 17 for Gradle 8+ era (PRs 6792, 6853, 7069, 7533)."""

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
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::http::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::https::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \
    for i in 1 2 3 4 5; do apt-get update && break || sleep 15; done && \
    apt-get install -y git openjdk-17-jdk curl unzip python3

{code}

{copy_commands}

{self.clear_env}

"""


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
        #   (6792 fix_patch upgrades AGP 7.3.1→8.1.2 + Gradle 7.5.1→8.4, needs JDK 17)
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

    def _init_gradle_script(self) -> str:
        """Gradle init script to configure test logging and repositories."""
        return textwrap.dedent("""\
            allprojects {
                buildscript {
                    repositories {
                        maven { url 'https://maven.aliyun.com/repository/public/' }
                        maven { url 'https://maven.aliyun.com/repository/jcenter/' }
                        maven { url 'https://maven.aliyun.com/repository/google/' }
                        maven { url 'https://plugins.gradle.org/m2/' }
                        mavenCentral()
                    }
                }

                repositories {
                    maven { url 'https://maven.aliyun.com/repository/public/' }
                    maven { url 'https://maven.aliyun.com/repository/jcenter/' }
                    maven { url 'https://maven.aliyun.com/repository/google/' }
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
        # Use --continue so we get full results even if some tests fail
        # Only run :gdx:test since that's where the unit tests live
        # --no-build-cache not supported on Gradle < 4.0 (PR 5839 uses Gradle 3.1)
        _old_gradle_prs = {5839}
        if self.pr.number in _old_gradle_prs:
            return "./gradlew :gdx:test --init-script ~/.gradle/init.gradle --max-workers 2 --continue"
        return "./gradlew :gdx:test --no-build-cache --init-script ~/.gradle/init.gradle --max-workers 2 --continue"

    def _restore_project_structure_cmd(self) -> str:
        # PRs 6792 and 7069 upgrade Gradle in fix_patch — don't restore wrapper
        _no_restore_wrapper_prs = {6792, 7069}
        if self.pr.number in _no_restore_wrapper_prs:
            restore = "git checkout HEAD -- settings.gradle backends/ tests/ 2>/dev/null || true"
        else:
            restore = "git checkout HEAD -- settings.gradle gradle/wrapper/ gradlew gradlew.bat backends/ tests/ 2>/dev/null || true"

        # Strip subprojects that cause Gradle configuration failures:
        # - tests:gdx-tests-android (requires specific AGP + Gradle version match)
        # - tests:gdx-tests-gwt (requires gwt-gradle-plugin from defunct jcenter)
        # - backends:gdx-backend-android (requires real Android SDK)
        # Remove publish.gradle apply (references projects that may not exist)
        # Fix configure() blocks in build.gradle and gradle/dist.gradle
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
            echo "org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=512m" >> gradle.properties
            {local_props}
            python3 /home/strip_binary_patches.py /home/test.patch /tmp/test.patch.clean
            git apply --ignore-whitespace /tmp/test.patch.clean || git apply --ignore-whitespace --reject /tmp/test.patch.clean 2>&1 || true
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
            echo "org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=512m" >> gradle.properties
            {local_props}
            python3 /home/strip_binary_patches.py /home/test.patch /tmp/test.patch.clean
            python3 /home/strip_binary_patches.py /home/fix.patch /tmp/fix.patch.clean
            git apply --ignore-whitespace /tmp/fix.patch.clean || git apply --ignore-whitespace --reject /tmp/fix.patch.clean 2>&1 || true
            git apply --ignore-whitespace /tmp/test.patch.clean || git apply --ignore-whitespace --reject /tmp/test.patch.clean 2>&1 || true
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

            with open(infile) as f:
                content = f.read()

            content = re.sub(
                r'diff --git [^\\n]*\\n(?:(?:old|new|deleted|similarity|rename|index|copy)[^\\n]*\\n)*Binary files [^\\n]*differ\\n?',
                '',
                content,
            )

            with open(outfile, 'w') as f:
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

        proxy_setup = ""
        proxy_cleanup = ""
        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p ~/.gradle && \\
                        if [ ! -f "$HOME/.gradle/gradle.properties" ]; then \\
                            touch "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        if ! grep -q "systemProp.http.proxyHost" "$HOME/.gradle/gradle.properties"; then \\
                            echo 'systemProp.http.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.http.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        echo 'export GRADLE_USER_HOME=/root/.gradle' >> ~/.bashrc && \\
                        /bin/bash -c "source ~/.bashrc"
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f ~/.gradle/gradle.properties
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

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
