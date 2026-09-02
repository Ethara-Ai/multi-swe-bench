from __future__ import annotations

import re
from typing import Any

from multi_swe_bench.harness.image import Config
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

# Importing the interval adapters is what puts them in Instance._registry, which
# is the table this module reads. Not unused.
from multi_swe_bench.harness.repos.python.xarray_contrib import (  # noqa: F401
    cf_xarray_103_to_294,
    cf_xarray_354_to_473,
)

# Registration for the PLAIN name "xarray-contrib/cf-xarray", forwarding to
# whichever interval adapter owns the PR number.
#
# Why it is needed: Instance.create() builds its lookup key from
# pr.number_interval and falls back to "{org}/{repo}" when that field is empty.
# Only the interval names are registered, so anything reaching Instance.create
# WITHOUT number_interval dies with
#     Instance 'xarray-contrib/cf-xarray' is not registered.
#
# That is not hypothetical. gen_report's dataset mode calls collect_report_tasks()
# BEFORE it touches self.raw_dataset, and the number_interval lookup inside is
# guarded on `hasattr(self, "_raw_dataset")` -- still False at that point, because
# raw_dataset is a lazily-populated property. Every ReportTask is therefore built
# with number_interval="" and every report fails. (run_evaluation() pre-loads with
# `_ = self.dataset` for exactly this reason; run_dataset() has no equivalent.)
#
# Registering the plain name fixes it from the repo config alone, with no edit to
# the harness. Repos whose plain name is registered -- the overwhelming majority --
# never hit the bug, which is why it stayed hidden.

# Bounds are read back out of the registered names rather than restated here, so
# adding or renaming an interval adapter needs no edit to this file. The names are
# HIGH_to_LOW, matching the enrichment's own `(?P<hi>\d+)_to_(?P<lo>\d+)` parse.
_NAME_RE = re.compile(r"^xarray-contrib/cf-xarray_(?P<hi>\d+)_to_(?P<lo>\d+)$")


def _intervals() -> list[tuple[int, int, type]]:
    matches = (
        (_NAME_RE.match(name), impl) for name, impl in Instance._registry.items()
    )
    # Keep only the entries whose name parsed; filter drops the rest without a
    # branch.
    parsed = filter(lambda pair: pair[0], matches)
    return [(int(m.group("lo")), int(m.group("hi")), impl) for m, impl in parsed]


@Instance.register("xarray-contrib", "cf-xarray")
class CfXarrayDispatch(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args: Any, **kwargs: Any):
        covers = lambda bounds: bounds[0] <= pr.number <= bounds[1]  # noqa: E731
        owners = [impl for _, _, impl in filter(covers, _intervals())]
        covered = ", ".join(
            f"{low}..{high}" for low, high, _ in sorted(_intervals())
        )
        # Indexing an empty list raises IndexError, which would say nothing
        # useful; the explicit check turns "PR outside every interval" into a
        # message that names the ranges that do exist.
        owners or _unroutable(pr.number, covered)
        # Returning an instance of a DIFFERENT class means Python skips this
        # class's __init__, and the delegate is already fully built.
        return owners[0](pr, config, *args, **kwargs)


def _unroutable(number: int, covered: str):
    raise ValueError(
        f"xarray-contrib/cf-xarray PR {number} falls outside every registered "
        f"interval ({covered}). Add an interval adapter that covers it, or set "
        f"number_interval on the record explicitly."
    )
