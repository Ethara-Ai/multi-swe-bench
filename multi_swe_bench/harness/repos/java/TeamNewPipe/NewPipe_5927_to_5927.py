import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.TeamNewPipe.NewPipe import _filter_binary_patches


class NewPipeJdk8ImageBase(Image):
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

        return f"""FROM {image_name}
{self.global_env}

ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git \\
    openjdk-8-jdk \\
    openjdk-11-jre-headless \\
    wget \\
    unzip \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8
ENV JAVA_HOME=/usr/lib/jvm/java-8

RUN mkdir -p ${{ANDROID_HOME}}/cmdline-tools && \\
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip -O /tmp/cmdline-tools.zip && \\
    unzip -q /tmp/cmdline-tools.zip -d ${{ANDROID_HOME}}/cmdline-tools && \\
    mv ${{ANDROID_HOME}}/cmdline-tools/cmdline-tools ${{ANDROID_HOME}}/cmdline-tools/latest && \\
    rm /tmp/cmdline-tools.zip

ENV PATH=${{ANDROID_HOME}}/cmdline-tools/latest/bin:${{ANDROID_HOME}}/platform-tools:${{PATH}}

# On arm64, create a fake emulator package so sdkmanager does not fail
# trying to resolve the x86-only emulator dependency
RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        mkdir -p ${{ANDROID_HOME}}/emulator && \\
        echo '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' > ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '<ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02" xmlns:ns3="http://schemas.android.com/repository/android/common/01">' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '  <localPackage path="emulator"><type-details xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="ns2:genericDetailsType"/>' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '  <revision><major>31</major><minor>3</minor><micro>14</micro></revision>' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '  <display-name>Android Emulator (fake for arm64)</display-name></localPackage>' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '</ns2:repository>' >> ${{ANDROID_HOME}}/emulator/package.xml; \\
    fi

# sdkmanager requires Java 11+; temporarily use JDK 11 to run it
RUN unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy && \\
    export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture) && \\
    yes | sdkmanager --licenses > /dev/null 2>&1 && \\
    sdkmanager "platforms;android-29" "build-tools;29.0.3" "platform-tools"

# On arm64, replace x86_64 build-tools binaries with aarch64 versions
# from github.com/lzhiyong/android-sdk-tools (using 33.0.3 binaries which
# are backward-compatible with 29.0.3 build outputs)
RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        LZHIYONG_VER="33.0.3" && \\
        curl -fsSL "https://github.com/lzhiyong/android-sdk-tools/releases/download/${{LZHIYONG_VER}}/android-sdk-tools-static-aarch64.zip" -o /tmp/arm64-build-tools.zip && \\
        unzip -q /tmp/arm64-build-tools.zip -d /tmp/arm64-bt && \\
        for BIN in aapt aapt2 zipalign dexdump split-select; do \\
            if [ -f "/tmp/arm64-bt/$BIN" ]; then \\
                cp -f "/tmp/arm64-bt/$BIN" "${{ANDROID_HOME}}/build-tools/29.0.3/$BIN" && \\
                chmod +x "${{ANDROID_HOME}}/build-tools/29.0.3/$BIN"; \\
            fi; \\
        done && \\
        rm -rf /tmp/arm64-build-tools.zip /tmp/arm64-bt; \\
    fi

{code}

# JCenter is dead — inject mavenCentral() into all projects via init.gradle
RUN mkdir -p /root/.gradle && \\
    echo 'allprojects {{ repositories {{ mavenCentral() }} }}' > /root/.gradle/init.gradle

{self.clear_env}

"""


class NewPipeJdk8ImageDefault(Image):
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
        return NewPipeJdk8ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _filter_binary_patches(self.pr.fix_patch)
        filtered_test_patch = _filter_binary_patches(self.pr.test_patch)

        return [
            File(
                ".",
                "fix.patch",
                f"{filtered_fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{filtered_test_patch}",
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
                "fix_jcenter_deps.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
sed -i "s/exoPlayerVersion = '2.13.2'/exoPlayerVersion = '2.13.3'/" app/build.gradle
sed -i 's/com\\.xwray:groupie:/com.github.lisawray.groupie:groupie:/' app/build.gradle
sed -i 's/com\\.xwray:groupie-viewbinding:/com.github.lisawray.groupie:groupie-viewbinding:/' app/build.gradle
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
bash /home/fix_jcenter_deps.sh
./gradlew clean assembleDebug testDebugUnitTest --continue || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/fix_jcenter_deps.sh
./gradlew clean assembleDebug testDebugUnitTest --continue

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/fix_jcenter_deps.sh
./gradlew clean assembleDebug testDebugUnitTest --continue

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/fix_jcenter_deps.sh
./gradlew clean assembleDebug testDebugUnitTest --continue

""".format(pr=self.pr),
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


@Instance.register("TeamNewPipe", "NewPipe_5927_to_5927")
class NewPipeEra1(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NewPipeJdk8ImageDefault(self.pr, self._config)

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
