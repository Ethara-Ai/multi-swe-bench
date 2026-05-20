from __future__ import annotations

"""Default delve config router.

Registered as Instance("go-delve", "delve") to handle PRs that don't have
a number_interval field in the JSONL data. Dispatches to the correct Go era
config based on PR number.
"""

from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.golang.go_delve.delve_premod import (
    _ImageDefault as _PremodImageDefault,
    _DelveInstanceBase as _PremodInstanceBase,
)
from multi_swe_bench.harness.repos.golang.go_delve.delve_go1_11 import (
    _ImageDefault as _Go111ImageDefault,
)
from multi_swe_bench.harness.repos.golang.go_delve.delve_go1_17 import (
    _ImageDefault as _Go117ImageDefault,
)
from multi_swe_bench.harness.repos.golang.go_delve.delve_go1_21 import (
    _ImageDefault as _Go121ImageDefault,
)
from multi_swe_bench.harness.repos.golang.go_delve.delve_go1_24 import (
    _ImageDefault as _Go124ImageDefault,
)


class Delve(_PremodInstanceBase):
    """Router that dispatches to the correct Go era based on PR number.

    PR <= 1299  -> golang:1.10  GOPATH (pre-module)
    PR 1300-2499 -> golang:1.11  module
    PR 2500-3499 -> golang:1.17  module
    PR 3500-3977 -> golang:1.21  module
    PR >= 3978   -> golang:1.24  module (backward compat for go 1.22)
    """

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 1299:
            return _PremodImageDefault(self.pr, self._config)
        elif self.pr.number <= 2499:
            return _Go111ImageDefault(self.pr, self._config)
        elif self.pr.number <= 3499:
            return _Go117ImageDefault(self.pr, self._config)
        elif self.pr.number <= 3977:
            return _Go121ImageDefault(self.pr, self._config)
        else:
            return _Go124ImageDefault(self.pr, self._config)


Instance.register("go-delve", "delve")(Delve)
