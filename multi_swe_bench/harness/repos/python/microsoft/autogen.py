from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

_NAME_RE = re.compile(r"^microsoft/autogen_(?P<hi>\d+)_to_(?P<lo>\d+)$")


def _intervals() -> list[tuple[int, int, type]]:
    matches = (
        (_NAME_RE.match(name), impl) for name, impl in Instance._registry.items()
    )
    parsed = filter(lambda pair: pair[0], matches)
    return [(int(m.group("lo")), int(m.group("hi")), impl) for m, impl in parsed]


def _raise(pr: PullRequest):
    raise ValueError(
        f"microsoft/autogen#{pr.number} is not covered by any registered "
        f"autogen_<hi>_to_<lo> interval adapter; known intervals: "
        f"{sorted((lo, hi) for lo, hi, _ in _intervals())}"
    )


@Instance.register("microsoft", "autogen")
class AutogenDispatch(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        covers = lambda bounds: bounds[0] <= pr.number <= bounds[1]
        candidates = sorted(filter(covers, _intervals()), key=lambda b: b[1] - b[0])
        owners = [impl for _, _, impl in candidates]
        next(iter(owners), None) or _raise(pr)
        return owners[0](pr, config, *args, **kwargs)
