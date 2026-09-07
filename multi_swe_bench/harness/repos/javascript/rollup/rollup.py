from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.javascript.rollup.rollup_2240_to_2240 import (
    ROLLUP_2240_TO_2240,
    ROLLUP_2240_TO_2240_ImageBase,
    ROLLUP_2240_TO_2240_ImageDefault,
)
from multi_swe_bench.harness.repos.javascript.rollup.rollup_4574_to_4021 import (
    ROLLUP_4574_TO_4021,
    ROLLUP_4574_TO_4021_ImageBase,
    ROLLUP_4574_TO_4021_ImageDefault,
)

_ERA_BOUNDARY = 2240

__all__ = [
    "Rollup",
    "ROLLUP_2240_TO_2240",
    "ROLLUP_2240_TO_2240_ImageBase",
    "ROLLUP_2240_TO_2240_ImageDefault",
    "ROLLUP_4574_TO_4021",
    "ROLLUP_4574_TO_4021_ImageBase",
    "ROLLUP_4574_TO_4021_ImageDefault",
]


@Instance.register("rollup", "rollup")
class Rollup(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

        if pr.number <= _ERA_BOUNDARY:
            self._era: Instance = ROLLUP_2240_TO_2240(pr, config, *args, **kwargs)
        else:
            self._era = ROLLUP_4574_TO_4021(pr, config, *args, **kwargs)

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
