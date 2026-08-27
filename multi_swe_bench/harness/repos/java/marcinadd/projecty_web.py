import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ProjectyWebImageBase(Image):
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
        # JDK 11, matching .github/workflows/gradle.yml ("Set up JDK 11").
        # Not a free choice: build.gradle still uses the `compile` /
        # `testCompile` configurations, which Gradle 7 removed, so the pinned
        # wrapper (gradle-6.5-bin.zip) has to stay -- and Gradle 6.5 supports
        # Java 14 at most. 11 is the version CI actually proved this tree on.
        return "eclipse-temurin:11-jdk"

    def image_tag(self) -> str:
        # PR-scoped, not a bare "base". The tag is what the PR layer's FROM
        # resolves to (image_full_name() = image_name():image_tag()), so a
        # repo-wide "base" would have every PR of this repo write and read one
        # mutable image -- each pinned to its own BASE_COMMIT. Building a second
        # PR would overwrite it and the earlier PR layer would then apply its
        # patches onto the wrong tree, silently and without an error.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Base image must stay plain: no `# syntax=` directive and a literal
        # clone URL (not "${REPO_URL}"). Either would disable DockerfileEnhancer,
        # dropping proxy/CA-cert injection, `git checkout ${BASE_COMMIT}`, and the
        # history-hardening block. See harness/image.py::DockerfileEnhancer.
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y git ca-certificates curl unzip && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class ProjectyWebImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return ProjectyWebImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "test-logging.gradle",
                """// build.gradle carries no `testLogging` block, so Gradle reports only the
// task result and never the individual cases -- parse_log would see an empty
// run. Injected as an init script rather than appended to build.gradle so the
// repo tree stays byte-identical to the base commit and `git apply` of the
// patches cannot conflict.
allprojects {
    tasks.withType(Test).configureEach {
        testLogging {
            events "passed", "failed", "skipped"
            showStandardStreams = false
            exceptionFormat = "full"
        }
    }
}

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

chmod +x gradlew

# Warm the Gradle distribution (6.5, fetched by the wrapper) and the whole
# dependency graph. Running the suite rather than `testClasses` is deliberate:
# the test runtime classpath (h2, spring-security-test, assertj) is only
# resolved once tests actually execute.
./gradlew test --no-daemon --init-script /home/test-logging.gradle || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
./gradlew cleanTest test --no-daemon --init-script /home/test-logging.gradle

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
# These suites reference production symbols that only fix.patch adds --
# Notification, NotificationType, NotificationObjectType and
# TeamRoleService#patchTeamRole. Gradle compiles every test source in a single
# :compileTestJava task, so leaving them in place fails the whole compile with
# 27 errors and not a single test runs -- every test reports NONE for this
# stage. Removing them lets the remaining suites compile and emit real
# PASS/FAIL. All of it stays in fix-run.sh, where fix.patch supplies the
# symbols. The three below are new files created by test.patch, so there is no
# earlier revision to fall back to and deletion is the only option.
rm -f src/test/java/com/projecty/projectyweb/notification/NotificationServiceTests.java
rm -f src/test/java/com/projecty/projectyweb/notification/ProjectNotificationAspectTests.java
rm -f src/test/java/com/projecty/projectyweb/notification/TeamNotificationAspectTests.java
# TeamRoleServiceTests is the one of the four that already existed at
# base.sha -- it holds 7 passing tests, and test.patch only *adds* cases to
# it that call TeamRoleService#patchTeamRole (a fix.patch symbol). Deleting
# the file would drop those 7 from this stage too, so they would report NONE
# here despite passing both before and after the fix. Restoring the base
# revision keeps them measured and removes only the uncompilable additions.
git checkout HEAD -- src/test/java/com/projecty/projectyweb/team/TeamRoleServiceTests.java
./gradlew cleanTest test --no-daemon --init-script /home/test-logging.gradle

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew cleanTest test --no-daemon --init-script /home/test-logging.gradle

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


@Instance.register("marcinadd", "projecty-web")
class ProjectyWeb(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ProjectyWebImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # One line per case, emitted by the injected testLogging block:
        #     com.projecty.projectyweb.team.TeamRoleServiceTests > shouldAddRole PASSED
        # The name is kept whole (class + " > " + method) because method names
        # such as `shouldReturnNotFound` repeat across test classes and would
        # otherwise merge into a single entry.
        re_case = re.compile(
            r"^(?P<name>\S.*?\s>\s.+?)\s+(?P<status>PASSED|FAILED|SKIPPED)$"
        )

        for line in clean_log.splitlines():
            line = line.strip()

            # `> Task :test FAILED` is the task result, not a case. It carries no
            # " > " separator so re_case misses it anyway, but skip it explicitly
            # so a future Gradle format change cannot silently inflate the counts.
            if line.startswith("> Task"):
                continue

            match = re_case.match(line)
            if not match:
                continue

            name = match.group("name").strip()
            status = match.group("status")

            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        # Deduplicate - worst result wins.
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
