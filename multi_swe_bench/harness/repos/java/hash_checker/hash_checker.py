import base64
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_UNIT_TEST_TASK = ":app:testThirdPartyStoresDebugUnitTest"
_ANDROID_TEST_COMPILE_TASK = (
    ":app:compileThirdPartyStoresDebugAndroidTestJavaWithJavac"
)

_GRADLE_CMD = (
    f"./gradlew {_UNIT_TEST_TASK} {_ANDROID_TEST_COMPILE_TASK} "
    "--continue --no-daemon --console=plain"
)

_SCRUB_EMPTY_PROXY = """\
for _pv in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do
    if [ -z "$(printenv $_pv)" ]; then unset $_pv; fi
done
unset _pv"""

_GRADLE_INIT_GRADLE = """\
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
        task.outputs.upToDateWhen { false }

        task.testLogging { logging ->
            logging.events 'passed', 'failed', 'skipped'
            logging.showStandardStreams = false
            logging.exceptionFormat 'short'
        }
    }
}
"""

_GRADLE_INIT_B64 = base64.b64encode(_GRADLE_INIT_GRADLE.encode()).decode()

_TEST_BODY = """\
set +e
{gradle_cmd} > /tmp/gradle.out 2>&1
GRADLE_RC=$?
set -e

cat /tmp/gradle.out

if [ "$GRADLE_RC" -ne 0 ]; then
    echo "NOTE: gradle exited $GRADLE_RC; see the task results above"
fi

grep -q "^> Task :app:" /tmp/gradle.out
"""


class HashCheckerImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git unzip wget openjdk-11-jdk-headless \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /root/.gradle && \\
    echo "org.gradle.jvmargs=-Xmx4g -Dfile.encoding=UTF-8" > /root/.gradle/gradle.properties && \\
    echo "org.gradle.daemon=false" >> /root/.gradle/gradle.properties && \\
    echo "org.gradle.parallel=false" >> /root/.gradle/gradle.properties && \\
    echo "org.gradle.configureondemand=false" >> /root/.gradle/gradle.properties

RUN echo "{_GRADLE_INIT_B64}" | base64 -d > /root/.gradle/init.gradle

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

RUN mkdir -p $ANDROID_HOME/cmdline-tools && \\
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip -O /tmp/cmdline-tools.zip && \\
    unzip -q /tmp/cmdline-tools.zip -d $ANDROID_HOME/cmdline-tools && \\
    mv $ANDROID_HOME/cmdline-tools/cmdline-tools $ANDROID_HOME/cmdline-tools/latest && \\
    rm -f /tmp/cmdline-tools.zip

RUN mkdir -p $ANDROID_HOME/licenses && \\
    echo "8933bad161af4178b1185d1a37fbf41ea5269c55" > $ANDROID_HOME/licenses/android-sdk-license && \\
    echo "d56f5187479451eabf01fb78af6dfcb131a6481e" >> $ANDROID_HOME/licenses/android-sdk-license && \\
    echo "24333f8a63b6825ea9c5514f83c2829b004d1fee" >> $ANDROID_HOME/licenses/android-sdk-license && \\
    echo "84831b9409646a918e30573bab4c9c91346d8abd" > $ANDROID_HOME/licenses/android-sdk-preview-license && \\
    echo "504667f4c0de7af1a06de9f4b1727b84351f2910" >> $ANDROID_HOME/licenses/android-sdk-preview-license

RUN for _pv in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do \\
        if [ -z "$(printenv $_pv)" ]; then unset $_pv; fi; \\
    done; \\
    sdkmanager --sdk_root=$ANDROID_HOME "platform-tools" "platforms;android-31" "build-tools;31.0.0" > /tmp/sdkmanager.log 2>&1 || \\
    (echo "sdkmanager failed:" && tail -40 /tmp/sdkmanager.log && exit 1)

{code}

{self.clear_env}

"""


class HashCheckerImageDefault(Image):
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

unset CI

{proxy_scrub}

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

chmod +x ./gradlew

if [ ! -f /home/{pr.repo}/checkstyle.xml ]; then
    wget -q -O /home/{pr.repo}/checkstyle.xml \\
        https://raw.githubusercontent.com/fartem/repository-rules/master/rules/java/android/checkstyle.xml || {{
        echo '<?xml version="1.0"?>' > /home/{pr.repo}/checkstyle.xml
        echo '<!DOCTYPE module PUBLIC "-//Checkstyle//DTD Checkstyle Configuration 1.3//EN" "https://checkstyle.org/dtds/configuration_1_3.dtd">' >> /home/{pr.repo}/checkstyle.xml
        echo '<module name="Checker"/>' >> /home/{pr.repo}/checkstyle.xml
    }}
fi

{gradle_cmd} > /tmp/warmup.log 2>&1 || true

if grep -qE "Task .[^']*. not found|Cannot locate tasks that match" /tmp/warmup.log; then
    echo "FATAL: gradle task name mismatch -- check the flavor/buildType in app/build.gradle" >&2
    tail -60 /tmp/warmup.log >&2
    exit 1
fi

if ! grep -q "^> Task :app:" /tmp/warmup.log; then
    echo "FATAL: gradle never reached task execution during the warm-up" >&2
    tail -60 /tmp/warmup.log >&2
    exit 1
fi

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

unset CI

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

_TASK_LINE = re.compile(r"^> Task (:\S+)(?:\s+(\S+))?\s*$")

_JUNIT_LINE = re.compile(r"^(\S.* > .+?) (PASSED|FAILED|SKIPPED)$")

_TASK_FAILED_SUFFIXES = {"FAILED"}
_TASK_SKIPPED_SUFFIXES = {"SKIPPED", "NO-SOURCE"}


def parse_gradle_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

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
