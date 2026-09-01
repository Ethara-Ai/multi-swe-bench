from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.airbnb.lottie_android_2284_to_0 import (
    LOTTIE_ANDROID_2284_TO_0,
)
from multi_swe_bench.harness.repos.java.airbnb.lottie_android_99999_to_2285 import (
    LOTTIE_ANDROID_99999_TO_2285,
)

INTERVAL_99999_TO_2285_MIN_PR = 2285


@Instance.register("airbnb", "lottie-android")
class LottieAndroid(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        if pr.number >= INTERVAL_99999_TO_2285_MIN_PR:
            self._era = LOTTIE_ANDROID_99999_TO_2285(pr, config, *args, **kwargs)
        else:
            self._era = LOTTIE_ANDROID_2284_TO_0(pr, config, *args, **kwargs)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return self._era.dependency()

    def run(self, run_cmd: str = "") -> str:
        return self._era.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return self._era.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return self._era.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, test_log: str) -> TestResult:
        return self._era.parse_log(test_log)


LOTTIE_ANDROID_PR_NUMBERS = (496, 624, 754, 1100, 2323)

for _n in LOTTIE_ANDROID_PR_NUMBERS:
    Instance.register("airbnb", str(_n))(LottieAndroid)
