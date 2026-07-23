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


# LHT bundled-PR dataset instances: Instance.create() uses pr.number_interval as a
# registry-key substitute (see harness/instance.py) -- for a single-PR instance
# number_interval is empty and the plain "miniflux/v2" key is used, but an LHT
# record bundles several PR numbers into one instance and stamps the exact
# dash-joined list (NOT a min-max range -- the bundle can have gaps) into
# number_interval, e.g. prs_in_bundle [146, 147, 150, 155, 157] -> "146-147-150-155-157".
# Each bundle in miniflux__v2_lht_final.jsonl with a lead PR number < 224 (pre-modules
# era) must resolve to a registered class, so alias every literal bundle string found
# in that dataset to MinifluxV2_0_to_223 (same image/build logic regardless of which
# PRs were squashed into the instance).
_LHT_BUNDLE_INTERVALS = [
    "9-17-20-29-30",
    "34-44-47",
    "53-56-60-67-69",
    "86-90-99",
    "100-101",
    "115-116",
    "125-131-133-135",
    "143-144-145-152-153-154-157-158-160-161-162-164",
    "166-167-168-169-171-172-175-177-181-182-183",
    "191-199-209-211-212-213-215-216-218-219",
]

for _interval in _LHT_BUNDLE_INTERVALS:
    Instance.register("miniflux", _interval)(MinifluxV2_0_to_223)
