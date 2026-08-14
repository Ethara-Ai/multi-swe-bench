import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Per-test Gradle logging. The default Gradle console only prints task-level
# lines (`> Task :app:testDebugUnitTest`), never individual test methods, so the
# harness cannot obtain stable per-test identities. This init script turns on
# `passed/failed/skipped` events for every Test task, producing lines like:
#   ... > com.squarecheck.shared.util.DateUtilTest > getDay PASSED
_INIT_GRADLE = """\
allprojects {
    tasks.withType(Test).configureEach {
        testLogging {
            events "passed", "failed", "skipped"
            showStandardStreams = false
            displayGranularity = 0
        }
    }
}
"""


class SquareCheckAndroidImageBase(Image):
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
        # eclipse-temurin:8-jdk-jammy ships JDK 8 (required by Gradle 6.1.1 /
        # Android Gradle Plugin 4.0.2); Ubuntu 22.04 no longer packages
        # openjdk-8, so a temurin base is used instead of ubuntu + apt.
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

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git \\
    wget \\
    unzip \\
    && rm -rf /var/lib/apt/lists/*

# cmdline-tools 6858069 (Sept 2020) is the last release whose sdkmanager runs on
# JDK 8; newer command-line tools require JDK 11+, which this JDK-8 build cannot
# provide. It installs the compileSdk 30 / build-tools 30.0.2 required by the app.
RUN mkdir -p ${{ANDROID_HOME}}/cmdline-tools && \\
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-6858069_latest.zip -O /tmp/cmdline-tools.zip && \\
    unzip -q /tmp/cmdline-tools.zip -d ${{ANDROID_HOME}}/cmdline-tools && \\
    mv ${{ANDROID_HOME}}/cmdline-tools/cmdline-tools ${{ANDROID_HOME}}/cmdline-tools/latest && \\
    rm /tmp/cmdline-tools.zip

ENV PATH=${{ANDROID_HOME}}/cmdline-tools/latest/bin:${{ANDROID_HOME}}/platform-tools:${{PATH}}

RUN unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy && \\
    yes | sdkmanager --licenses > /dev/null 2>&1 && \\
    sdkmanager "platforms;android-30" "build-tools;30.0.2" "platform-tools" > /dev/null 2>&1

{code}

{self.clear_env}

"""


class SquareCheckAndroidImageDefault(Image):
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
        return SquareCheckAndroidImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

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
./gradlew clean testDebugUnitTest --continue --no-daemon --init-script /home/init.gradle || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
# timeout: the Gradle JVM can hang after BUILD SUCCESSFUL under qemu/arm64 (leaked
# non-daemon aapt2 worker threads spin forever), and this docker-run path has no
# timeout, so bound it. aapt2.threads=1 + useWorkers=false disables the worker
# daemons that cause the hang. Test results are printed before any kill, so the
# report parser still captures them. `|| true` keeps set -e from aborting on a kill.
timeout --kill-after=60 1800 ./gradlew clean testDebugUnitTest --continue --no-daemon --init-script /home/init.gradle -Pandroid.aapt2.threads=1 -Pandroid.experimental.aapt2.useWorkers=false || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
timeout --kill-after=60 1800 ./gradlew clean testDebugUnitTest --continue --no-daemon --init-script /home/init.gradle -Pandroid.aapt2.threads=1 -Pandroid.experimental.aapt2.useWorkers=false || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
timeout --kill-after=60 1800 ./gradlew clean testDebugUnitTest --continue --no-daemon --init-script /home/init.gradle -Pandroid.aapt2.threads=1 -Pandroid.experimental.aapt2.useWorkers=false || true

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

        # `RUN bash /home/prepare.sh` is intentionally omitted. Running the full
        # Gradle build at image-build time baked a multi-GB layer into the PR
        # image, and `docker buildx build --platform linux/amd64 --load` deadlocks
        # while importing such a large cross-arch image on Apple Silicon (the small
        # base image loads fine; the bloated PR image hangs forever at export).
        # Keeping the PR image small lets --load succeed. The Gradle build still
        # runs at grading time via run.sh/test-run.sh/fix-run.sh, which the harness
        # executes with `docker run` (no buildx --load, so no hang).
        prepare_commands = ""

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("agustinanisaf", "SquareCheck-android")
class SquareCheckAndroid(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SquareCheckAndroidImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Gradle per-test lines emitted by init.gradle's testLogging, e.g.:
        #   Gradle Test Run :app:testDebugUnitTest > Gradle Test Executor 1 \
        #       > com.squarecheck.shared.util.DateUtilTest > getDay PASSED
        # Capture only the trailing "<FQCN> > <method>" so the identity maps to
        # the owning test file (report.py file-hosts matcher) and never picks up
        # Gradle build-task lines (which would misclassify compilation as a test).
        test_re = re.compile(r" > ([\w.$]+ > [^>]+?) (PASSED|FAILED|SKIPPED)$")

        for line in clean_log.splitlines():
            m = test_re.search(line.rstrip())
            if not m:
                continue
            name = m.group(1).strip()
            status = m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # Enforce pairwise-disjoint sets with FAILED > SKIPPED > PASSED priority.
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
