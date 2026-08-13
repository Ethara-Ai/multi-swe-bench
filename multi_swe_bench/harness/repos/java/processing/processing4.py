import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Per-test Gradle logging + JVM isolation. Two jobs:
#   1. testLogging events — the default Gradle console only prints task-level
#      lines (`> Task :core:test`), never individual test methods, so the
#      harness cannot obtain stable per-test identities. Turning on
#      passed/failed/skipped events produces lines like:
#        ... > PAppletKeyEventTest > testSingleKeyPressAndRelease PASSED
#   2. forkEvery = 1 — the :core:test suite runs several test classes in one
#      JVM by default. Some processing.core tests leak static state (AWT / key
#      modifier handling) that poisons PAppletKeyEventTest's modifier + focus
#      cases: they pass in a clean JVM but fail when other classes ran first.
#      Without isolation the fix's effect is masked (the target tests fail in
#      BOTH the test-only and fix stages → no fail->pass → invalid instance).
#      forkEvery = 1 gives each test class a fresh JVM, restoring the real
#      fail->pass transition.
_INIT_GRADLE = """\
allprojects {
    tasks.withType(Test).configureEach {
        forkEvery = 1
        testLogging {
            events "passed", "failed", "skipped"
            showStandardStreams = false
            displayGranularity = 0
        }
    }
}
"""


class Processing4ImageBase(Image):
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
        # processing4 targets JDK 17 (all CI workflows use temurin 17); the
        # Gradle wrapper (8.11) and JOGL/JUnit deps resolve cleanly on it.
        return "eclipse-temurin:17-jdk"

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
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class Processing4ImageDefault(Image):
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
        return Processing4ImageBase(self.pr, self._config)

    # image_tag keeps the `-v2` suffix to reuse the already-built multiarch image
    # (the first `-v1`/plain image shipped without a JOGL cache due to a jogamp.org
    # timeout masked by `|| true`; the fixed, retrying prepare.sh built `-v2`).
    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-v2"

    # workdir MUST stay `pr-<number>` with no suffix: gen_report.collect_report_tasks
    # parses the instance dir name as int(name[3:]) and only reports the task when
    # `org/repo:pr-<number>` matches a raw-dataset key. A `-v2` suffix here makes
    # int("966-v2") raise ValueError, so the instance is silently dropped.
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

# JOGL / GlueGen (org.jogamp.*) are published ONLY on jogamp.org, which is slow
# and frequently times out (they are not on Maven Central). Warm the Gradle
# cache here, at image-build time, with long HTTP timeouts and retries so the
# jars are baked into the image; the run scripts then use --offline and never
# contact jogamp.org again. This step MUST populate the cache -- if every
# attempt hits a network/resolution error we fail the build loudly instead of
# shipping an image without JOGL (which would make every stage capture 0 tests).
GRADLE_TIMEOUTS="-Dorg.gradle.internal.http.connectionTimeout=180000 -Dorg.gradle.internal.http.socketTimeout=180000"
n=0
while true; do
  n=$((n+1))
  ./gradlew clean :core:test --continue --no-daemon --init-script /home/init.gradle $GRADLE_TIMEOUTS > /home/prepare_gradle.log 2>&1 || true
  if grep -qE "Could not resolve|Could not GET|Could not download|Read timed out|Connect to .* failed" /home/prepare_gradle.log; then
    if [ "$n" -ge 5 ]; then
      echo "prepare.sh: dependency resolution still failing after $n attempts" >&2
      cat /home/prepare_gradle.log >&2
      exit 1
    fi
    echo "prepare.sh: attempt $n hit a network/resolution error, retrying in 15s..." >&2
    sleep 15
    continue
  fi
  break
done
cat /home/prepare_gradle.log
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
./gradlew clean :core:test --offline --continue --no-daemon --init-script /home/init.gradle

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./gradlew clean :core:test --offline --continue --no-daemon --init-script /home/init.gradle

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew clean :core:test --offline --continue --no-daemon --init-script /home/init.gradle

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


@Instance.register("processing", "processing4")
class Processing4(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Processing4ImageDefault(self.pr, self._config)

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
        #   Gradle Test Run :core:test > Gradle Test Executor 1 \
        #       > processing.core.PAppletKeyEventTest > testSingleKeyPressAndRelease PASSED
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
