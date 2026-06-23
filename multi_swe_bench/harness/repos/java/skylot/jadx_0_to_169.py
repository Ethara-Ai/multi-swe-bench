"""skylot/jadx earliest JDK 8 era (PRs 0..169) -- two-tier image config.

Shares the ``base-eclipse-temurin-8`` base with the 170..792 era (identical JDK
+ init.gradle). Kept as a distinct registry key for completeness; the generic
``skylot/jadx`` dispatcher routes <=792 to the 170..792 PR class. See
``_common.JadxBaseImage`` / ``JadxPRImage``.
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


class JadxJdk8Era1Base(JadxBaseImage):
    JDK_IMAGE = "eclipse-temurin:8"
    INIT_GRADLE = _INIT_GRADLE


class JadxJdk8Era1ImageDefault(JadxPRImage):
    BASE_CLASS = JadxJdk8Era1Base


@Instance.register("skylot", "jadx_0_to_169")
class Jadx0To169(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JadxJdk8Era1ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_gradle_log(test_log)
