import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Per-test Gradle logging. The default Gradle console only prints task-level
# lines (`> Task :test`) and per-test lines for FAILURES only, never individual
# PASSED methods, so the harness cannot obtain stable per-test identities. This
# init script turns on `passed/failed/skipped` events for every Test task,
# producing lines like:
#   Gradle suite > Gradle test \
#       > io.visual_regression_tracker.sdk_java.VisualRegressionTrackerTest \
#       > shouldStartBuild PASSED
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


class VisualRegressionTrackerSdkJavaImageBase(Image):
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
        # PR #54 migrates the SDK from JDK 8 to JDK 11 (java.net.http.HttpClient).
        # The test patch references Java 11 HTTP APIs, so even the pre-fix code
        # must be compiled with a JDK 11 toolchain. Gradle 6.1 (the repo's
        # wrapper) supports JDK 11, so eclipse-temurin:11 is used directly.
        return "eclipse-temurin:11"

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
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class VisualRegressionTrackerSdkJavaImageDefault(Image):
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
        return VisualRegressionTrackerSdkJavaImageBase(self.pr, self._config)

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
chmod +x gradlew
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
./gradlew clean test --continue --no-daemon --init-script /home/init.gradle || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
./gradlew clean test --continue --no-daemon --init-script /home/init.gradle

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./gradlew clean test --continue --no-daemon --init-script /home/init.gradle

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew clean test --continue --no-daemon --init-script /home/init.gradle

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("Visual-Regression-Tracker", "sdk-java")
class VisualRegressionTrackerSdkJava(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VisualRegressionTrackerSdkJavaImageDefault(self.pr, self._config)

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
        #   Gradle suite > Gradle test \
        #       > io.visual_regression_tracker.sdk_java.VisualRegressionTrackerTest \
        #       > shouldStartBuild PASSED
        # Capture only the trailing "<FQCN> > <method>" so the identity maps to
        # the owning test file and never picks up Gradle build-task lines (which
        # would misclassify compilation as a test).
        test_re = re.compile(r" > ([\w.$]+ > .+?) (PASSED|FAILED|SKIPPED)$")

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
