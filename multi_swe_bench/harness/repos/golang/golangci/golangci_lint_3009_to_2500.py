from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .golangci_lint import (
    GolangciLintImageDefault,
    golangci_lint_parse_log,
)

_GO_VERSION = "1.20"
_INTERVAL_NAME = "golangci-lint_3009_to_2500"


@Instance.register("golangci", _INTERVAL_NAME)
class GolangciLint_3009_to_2500(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GolangciLintImageDefault(
            self.pr, self._config, go_version=_GO_VERSION, interval_name=_INTERVAL_NAME
        )

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
        return golangci_lint_parse_log(test_log)
