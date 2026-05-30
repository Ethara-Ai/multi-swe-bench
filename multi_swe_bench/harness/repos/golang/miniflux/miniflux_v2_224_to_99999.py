from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .miniflux_v2 import (
    MinifluxImageDefault,
    miniflux_parse_log,
)

# Modules era (PR 224 .. 4192): go.mod present. Covers both `module miniflux.app`
# (PR 224 .. ~1843) and `module miniflux.app/v2` (PR 2035+) because they share
# the same build flow — single root go.mod, `go test ./...` from repo root.
# GOTOOLCHAIN=auto on golang:1.22 downloads newer toolchains as declared by
# each PR's go.mod (verified up to go 1.26.0). GOFLAGS=-mod=mod ignores the
# legacy `vendor/modules.txt` (left over from `dep` migration) which is
# inconsistent under Go 1.21+.
_GO_VERSION = "1.22"
_INTERVAL_NAME = "v2_224_to_99999"


@Instance.register("miniflux", _INTERVAL_NAME)
class MinifluxV2_224_to_99999(Instance):
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
            prepare_style="modules",
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
