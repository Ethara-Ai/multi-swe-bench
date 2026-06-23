"""skylot/jadx JDK 21 era (PRs >= 1832) -- two-tier image config.

Single shared base ``base-eclipse-temurin-21`` reused by every JDK 21 per-PR
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

# init.gradle: mavenCentral() + per-test event logging. No jcommander
# substitution needed on this era.
_INIT_GRADLE = """\
allprojects {
    repositories {
        mavenCentral()
        maven { url "https://plugins.gradle.org/m2/" }
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


class JadxJdk21Base(JadxBaseImage):
    JDK_IMAGE = "eclipse-temurin:21"
    INIT_GRADLE = _INIT_GRADLE


class JadxJdk21ImageDefault(JadxPRImage):
    BASE_CLASS = JadxJdk21Base


@Instance.register("skylot", "jadx_1832_to_99999")
class Jadx1832To99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JadxJdk21ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_gradle_log(test_log)
