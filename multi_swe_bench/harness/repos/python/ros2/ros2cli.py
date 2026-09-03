from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.python.ros2.ros2cli_524_to_524 import (
    ROS2CLI_524_TO_524,
)
from multi_swe_bench.harness.repos.python.ros2.ros2cli_590_to_590 import (
    ROS2CLI_590_TO_590,
)
from multi_swe_bench.harness.repos.python.ros2.ros2cli_749_to_749 import (
    ROS2CLI_749_TO_749,
)
from multi_swe_bench.harness.repos.python.ros2.ros2cli_935_to_925 import (
    ROS2CLI_935_TO_925,
)


@Instance.register("ros2", "ros2cli")
class ROS2CLI(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

        if pr.number <= 524:
            era = ROS2CLI_524_TO_524
        elif pr.number <= 700:
            era = ROS2CLI_590_TO_590
        elif pr.number <= 800:
            era = ROS2CLI_749_TO_749
        else:
            era = ROS2CLI_935_TO_925

        self._delegate: Instance = era(pr, config, *args, **kwargs)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return self._delegate.dependency()

    def run(self, run_cmd: str = "") -> str:
        return self._delegate.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return self._delegate.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return self._delegate.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, test_log: str) -> TestResult:
        return self._delegate.parse_log(test_log)
