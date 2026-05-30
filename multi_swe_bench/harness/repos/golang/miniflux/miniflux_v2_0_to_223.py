from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .miniflux_v2 import (
    MinifluxImageDefault,
    miniflux_parse_log,
)

# Pre-modules era (PR 9 .. 191 in the dataset; cutoff 223): no go.mod, uses
# `dep` (Gopkg.toml + vendor/). Source must be staged under
# $GOPATH/src/github.com/miniflux/miniflux because the code's internal imports
# still reference the old miniflux/miniflux module path.
#
# golang:1.22 + GO111MODULE=off + vendor/ is sufficient to run `go test ./...`
# even though the original repo declared Go 1.9; Go is forward compatible for
# the surface area these tests exercise.
_GO_VERSION = "1.22"
_INTERVAL_NAME = "v2_0_to_223"


@Instance.register("miniflux", _INTERVAL_NAME)
class MinifluxV2_0_to_223(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MinifluxImageDefault(
            self.pr,
            self._config,
            go_version=_GO_VERSION,
            interval_name=_INTERVAL_NAME,
            prepare_style="gopath",
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
        return miniflux_parse_log(test_log)
