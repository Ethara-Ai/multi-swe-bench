from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.golang.stashapp.stash_1000_to_0 import Stash1000To0
from multi_swe_bench.harness.repos.golang.stashapp.stash_2500_to_1001 import (
    Stash2500To1001,
)
from multi_swe_bench.harness.repos.golang.stashapp.stash_5000_to_2501 import (
    Stash5000To2501,
)
from multi_swe_bench.harness.repos.golang.stashapp.stash_99999_to_5001 import (
    Stash99999To5001,
)

ERA_ROUTES = (
    (1000, Stash1000To0),
    (2500, Stash2500To1001),
    (5000, Stash5000To2501),
)

ERA_LATEST = Stash99999To5001


def era_for(number: int):
    for upper, cls in ERA_ROUTES:
        if number <= upper:
            return cls
    return ERA_LATEST


@Instance.register("stashapp", "stash")
class Stash(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._era = era_for(pr.number)(pr, config, *args, **kwargs)

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
