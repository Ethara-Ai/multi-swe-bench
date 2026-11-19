from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.javascript.rollup.rollup_777_to_777 import ROLLUP_777_TO_777
from multi_swe_bench.harness.repos.javascript.rollup.rollup_2208_to_902 import ROLLUP_2208_TO_902
from multi_swe_bench.harness.repos.javascript.rollup.rollup_2240_to_2240 import (
    ROLLUP_2240_TO_2240,
    ROLLUP_2240_TO_2240_ImageBase,
    ROLLUP_2240_TO_2240_ImageDefault,
)
from multi_swe_bench.harness.repos.javascript.rollup.rollup_2254_to_2218 import ROLLUP_2254_TO_2218
from multi_swe_bench.harness.repos.javascript.rollup.rollup_4018_to_2263 import ROLLUP_4018_TO_2263
from multi_swe_bench.harness.repos.javascript.rollup.rollup_4574_to_4021 import (
    ROLLUP_4574_TO_4021,
    ROLLUP_4574_TO_4021_ImageBase,
    ROLLUP_4574_TO_4021_ImageDefault,
)
from multi_swe_bench.harness.repos.javascript.rollup.rollup_5026_to_4593 import ROLLUP_5026_TO_4593
from multi_swe_bench.harness.repos.javascript.rollup.rollup_5737_to_5207 import ROLLUP_5737_TO_5207
from multi_swe_bench.harness.repos.javascript.rollup.rollup_5944_to_5778 import ROLLUP_5944_TO_5778
from multi_swe_bench.harness.repos.javascript.rollup.rollup_6583_to_6065 import ROLLUP_6583_TO_6065
from multi_swe_bench.harness.repos.javascript.rollup.rollup_6651_to_6628 import ROLLUP_6651_TO_6628

_ERA_BOUNDARY = 2240

_RANGES = [
    (777, 777, ROLLUP_777_TO_777),
    (902, 2208, ROLLUP_2208_TO_902),
    (2218, 2254, ROLLUP_2254_TO_2218),
    (2263, 4018, ROLLUP_4018_TO_2263),
    (4021, 4574, ROLLUP_4574_TO_4021),
    (4593, 5026, ROLLUP_5026_TO_4593),
    (5207, 5737, ROLLUP_5737_TO_5207),
    (5778, 5944, ROLLUP_5944_TO_5778),
    (6065, 6583, ROLLUP_6583_TO_6065),
    (6628, 6651, ROLLUP_6651_TO_6628),
]

__all__ = [
    "Rollup",
    "ROLLUP_2240_TO_2240",
    "ROLLUP_2240_TO_2240_ImageBase",
    "ROLLUP_2240_TO_2240_ImageDefault",
    "ROLLUP_4574_TO_4021",
    "ROLLUP_4574_TO_4021_ImageBase",
    "ROLLUP_4574_TO_4021_ImageDefault",
]


def _select(number: int):
    if number == _ERA_BOUNDARY:
        return ROLLUP_2240_TO_2240
    for low, high, era in _RANGES:
        if low <= number <= high:
            return era
    if number <= _ERA_BOUNDARY:
        return ROLLUP_2240_TO_2240
    return ROLLUP_4574_TO_4021


@Instance.register("rollup", "rollup")
class Rollup(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._era: Instance = _select(pr.number)(pr, config, *args, **kwargs)

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
