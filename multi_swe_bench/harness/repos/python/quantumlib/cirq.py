from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

# Dispatcher for the PLAIN registry name "quantumlib/Cirq".
#
# Why this file has to exist: Instance.create() keys on `{org}/{number_interval}`
# and falls back to `{org}/{repo}` when number_interval is empty. Every record in
# the raw dataset ships with number_interval="", so without a class registered
# under the bare name "quantumlib/Cirq" the lookup fails outright with
# "Instance 'quantumlib/Cirq' is not registered". build_dataset can survive that
# by enriching the field first, but gen_report collects its tasks BEFORE the raw
# dataset is loaded and cannot -- the identical failure cost cf-xarray a full
# rebuild before a dispatcher was added there.
#
# NARROWEST INTERVAL WINS. quantumlib/Cirq has several overlapping interval
# adapters: Cirq_5650_to_4103 spans 1547 PR numbers, Cirq_5054_to_5005 spans 49.
# A PR covered by both should get the one written specifically for it, so
# candidates are ranked by span and the tightest is chosen. That rule is what
# lets a focused adapter be added for a handful of PRs without touching -- or
# being shadowed by -- the broad adapter that still serves everything else.
_NAME_RE = re.compile(r"^quantumlib/Cirq_(?P<hi>\d+)_to_(?P<lo>\d+)$")


def _intervals() -> list[tuple[int, int, type]]:
    """Every registered quantumlib/Cirq interval, as (lo, hi, impl)."""
    matches = ((_NAME_RE.match(name), impl) for name, impl in Instance._registry.items())
    parsed = filter(lambda pair: pair[0], matches)
    return [(int(m.group("lo")), int(m.group("hi")), impl) for m, impl in parsed]


@Instance.register("quantumlib", "Cirq")
class CirqDispatch(Instance):
    """Not an implementation -- a router.

    __new__ returns an instance of the winning interval class instead of `self`,
    so the harness never sees this type. Registering it costs nothing at build
    time and makes the bare-name lookup resolve.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        covers = lambda bounds: bounds[0] <= pr.number <= bounds[1]
        candidates = sorted(
            filter(covers, _intervals()), key=lambda b: b[1] - b[0]
        )
        owners = [impl for _, _, impl in candidates]

        # No default, no guessing: a PR outside every declared interval is a
        # dataset/registry mismatch and must fail loudly rather than be graded by
        # whichever adapter happened to be imported first.
        next(iter(owners), None) or _raise(pr)
        return owners[0](pr, config, *args, **kwargs)


def _raise(pr: PullRequest):
    raise ValueError(
        f"quantumlib/Cirq#{pr.number} is not covered by any registered "
        f"Cirq_<hi>_to_<lo> interval adapter; known intervals: "
        f"{sorted((lo, hi) for lo, hi, _ in _intervals())}"
    )
