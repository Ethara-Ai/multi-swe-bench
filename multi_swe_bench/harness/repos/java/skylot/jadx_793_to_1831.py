"""skylot/jadx JDK 11 era (PRs 793..1831) -- two-tier image config.

A single shared base image (``base-eclipse-temurin-11``) is built once and
reused as the FROM parent of every JDK 11 per-PR image; the per-PR image only
checks out its base commit, warms per-SHA deps, and hardens git history. See
``_common.JadxBaseImage`` / ``JadxPRImage`` for the shared logic.
"""

from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.skylot._common import (
    JadxBaseImage,
    JadxPRImage,
    parse_gradle_log,
)

# init.gradle: mavenCentral(), substitute jcommander 1.80 -> 1.78 (1.80 was
# never published to Maven Central), and emit per-test PASSED/FAILED events.
_INIT_GRADLE = """\
allprojects {
    repositories {
        mavenCentral()
        maven { url "https://plugins.gradle.org/m2/" }
    }
    configurations.all {
        resolutionStrategy.dependencySubstitution {
            substitute module("com.beust:jcommander:1.80") with module("com.beust:jcommander:1.78")
        }
    }
    tasks.withType(Test).configureEach { test ->
        test.testLogging {
            events "passed", "failed", "skipped"
            exceptionFormat "short"
            showStandardStreams = false
        }
    }
}
"""


class JadxJdk11Base(JadxBaseImage):
    JDK_IMAGE = "eclipse-temurin:11"
    INIT_GRADLE = _INIT_GRADLE


class JadxJdk11ImageDefault(JadxPRImage):
    BASE_CLASS = JadxJdk11Base


@Instance.register("skylot", "jadx_793_to_1831")
class Jadx793To1831(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JadxJdk11ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_gradle_log(test_log)
