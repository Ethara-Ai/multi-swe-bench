import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_INIT_GRADLE = """\
allprojects { p ->
    p.buildscript.repositories {
        google()
        mavenCentral()
        maven { url 'https://jitpack.io' }
    }
    p.repositories {
        google()
        mavenCentral()
        maven { url 'https://jitpack.io' }
    }
    p.tasks.withType(Test) {
        testLogging {
            events "passed", "failed", "skipped"
            showStandardStreams = false
            displayGranularity = 0
        }
    }
}
"""


def _parse_gradle_test_log(test_log: str) -> TestResult:
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    test_re = re.compile(r" > ([\w.$]+ > [^>]+?) (PASSED|FAILED|SKIPPED)$")

    for line in clean.splitlines():
        m = test_re.search(line.rstrip())
        if not m:
            continue

        name = m.group(1).strip()
        status = m.group(2)
        if status == "PASSED":
            passed.add(name)
        elif status == "FAILED":
            failed.add(name)
        else:
            skipped.add(name)

    passed -= failed
    skipped -= failed
    passed -= skipped

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


class LoyaltyCardLockerImageBase(Image):
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
        return "eclipse-temurin:8-jdk-jammy"

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

ENV LC_ALL=C.UTF-8
ENV CI=true
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk

WORKDIR /home/

RUN apt-get update -o Acquire::Retries=5 && \\
    apt-get install -y --no-install-recommends -o Acquire::Retries=5 \\
    git \\
    ca-certificates \\
    wget \\
    unzip \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p ${{ANDROID_HOME}}/cmdline-tools && \\
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-6858069_latest.zip -O /tmp/cmdline-tools.zip && \\
    unzip -q /tmp/cmdline-tools.zip -d ${{ANDROID_HOME}}/cmdline-tools && \\
    mv ${{ANDROID_HOME}}/cmdline-tools/cmdline-tools ${{ANDROID_HOME}}/cmdline-tools/latest && \\
    rm /tmp/cmdline-tools.zip

ENV PATH=${{ANDROID_HOME}}/cmdline-tools/latest/bin:${{ANDROID_HOME}}/platform-tools:${{PATH}}

RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        mkdir -p ${{ANDROID_HOME}}/emulator && \\
        echo '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' > ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '<ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02" xmlns:ns3="http://schemas.android.com/repository/android/common/01">' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '  <localPackage path="emulator"><type-details xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="ns2:genericDetailsType"/>' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '  <revision><major>31</major><minor>3</minor><micro>14</micro></revision>' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '  <display-name>Android Emulator (fake for arm64)</display-name></localPackage>' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '</ns2:repository>' >> ${{ANDROID_HOME}}/emulator/package.xml; \\
    fi

RUN unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy && \\
    yes | sdkmanager --licenses > /dev/null 2>&1 && \\
    sdkmanager "platforms;android-29" "build-tools;29.0.3" "build-tools;28.0.3" "platform-tools"

RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        LZHIYONG_VER="33.0.3" && \\
        curl -fsSL "https://github.com/lzhiyong/android-sdk-tools/releases/download/${{LZHIYONG_VER}}/android-sdk-tools-static-aarch64.zip" -o /tmp/arm64-build-tools.zip && \\
        unzip -q /tmp/arm64-build-tools.zip -d /tmp/arm64-bt && \\
        for SDK_VER in 29.0.3 28.0.3; do \\
            for BIN in aapt aapt2 zipalign dexdump split-select; do \\
                if [ -f "/tmp/arm64-bt/$BIN" ] && [ -d "${{ANDROID_HOME}}/build-tools/$SDK_VER" ]; then \\
                    cp -f "/tmp/arm64-bt/$BIN" "${{ANDROID_HOME}}/build-tools/$SDK_VER/$BIN" && \\
                    chmod +x "${{ANDROID_HOME}}/build-tools/$SDK_VER/$BIN"; \\
                fi; \\
            done; \\
        done && \\
        rm -rf /tmp/arm64-build-tools.zip /tmp/arm64-bt; \\
    fi

RUN mkdir -p /root/.gradle && \\
    printf '%s\\n' \\
      'org.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8 -Djava.awt.headless=true' \\
      > /root/.gradle/gradle.properties

{code}

{self.clear_env}

"""


class LoyaltyCardLockerImageDefault(Image):
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
        return LoyaltyCardLockerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    _TEST_CMD = (
        "timeout --kill-after=60 2400 ./gradlew clean testReleaseUnitTest "
        "--continue --no-daemon --init-script /home/init.gradle"
    )

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
                _INIT_GRADLE,
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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
timeout --kill-after=60 2400 ./gradlew clean testReleaseUnitTest --continue --no-daemon --init-script /home/init.gradle || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
{test_cmd}

""".format(repo=self.pr.repo, test_cmd=self._TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}

""".format(repo=self.pr.repo, test_cmd=self._TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}

""".format(repo=self.pr.repo, test_cmd=self._TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("brarcher", "loyalty-card-locker")
class LoyaltyCardLocker(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LoyaltyCardLockerImageDefault(self.pr, self._config)

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
        return _parse_gradle_test_log(test_log)
