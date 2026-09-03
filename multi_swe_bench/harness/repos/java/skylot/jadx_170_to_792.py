"""skylot/jadx JDK 8 era (PRs 170..792) -- two-tier image config.

Single shared base ``base-eclipse-temurin-8`` reused by every JDK 8 per-PR
image. See ``_common.JadxBaseImage`` / ``JadxPRImage``.
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

# init.gradle: mavenCentral(), substitute jcommander 1.74 -> 1.72, per-test
# event logging. JDK 8 era jadx uses the older `tasks.withType(Test) { }` form.
_INIT_GRADLE = """\
allprojects {
    repositories {
        mavenCentral()
        maven { url "https://plugins.gradle.org/m2/" }
    }
    configurations.all {
        resolutionStrategy.dependencySubstitution {
            substitute module("com.beust:jcommander:1.74") with module("com.beust:jcommander:1.72")
        }
    }
    tasks.withType(Test) { test ->
        test.testLogging {
            events "passed", "failed", "skipped"
            exceptionFormat "short"
            showStandardStreams = false
        }
    }
}
"""


class JadxJdk8Era2Base(JadxBaseImage):
    # Pinned to a specific build (QC D2) instead of the floating :8 tag, so the
    # image is reproducible over time. 8u462-b08 is verified multi-arch
    # (linux/amd64 + linux/arm64), which the arm64 build pass requires.
    JDK_IMAGE = "eclipse-temurin:8u462-b08-jdk"
    INIT_GRADLE = _INIT_GRADLE


class JadxJdk8Era2ImageDefault(JadxPRImage):
    BASE_CLASS = JadxJdk8Era2Base


@Instance.register("skylot", "jadx_170_to_792")
class Jadx170To792(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JadxJdk8Era2ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_gradle_log(test_log)
