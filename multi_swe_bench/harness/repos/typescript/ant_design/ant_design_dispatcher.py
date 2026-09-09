"""Routes ant-design/ant-design PRs to the right era config.

A dispatcher is needed here only because a dataset's rows may carry an EMPTY
`number_interval`. Instance.create() then builds the key as `org/repo`
(instance.py:41-48), so every PR in the file collapses onto the single key
`ant-design/ant-design` and exactly one class can own it. That key is claimed by
ant_design.py, whose image is pinned to **node:18** -- correct for present-day
ant-design and wrong for anything before ~2020. Without this file a 2016 PR is
built on a 2022 toolchain and fails in `npm install`, not in the code under test.

When a dataset does carry `number_interval` (e.g. "ant_design_10890_to_4765"),
the harness routes straight to that era module and this dispatcher is bypassed
entirely -- it only fills the gap for interval-less datasets.

Era ladder, from the existing per-era modules. Each was built against the
toolchain the repository actually pinned at that point in its history:

    563   - 4756    node:6
    4765  - 10890   node:8
    10891 - 17846   node:11
    17847 - 25073
    25074 - 30655
    30656 - 35705
    35706 - 40793
    40794 - 46012
    46013 - 51609
    51610 - 55638
    55639 - 57633

The ranges are inclusive and non-overlapping, and they are transcribed from the
module filenames rather than invented here, so this table cannot drift from the
modules it dispatches to without the import failing first.
"""

from multi_swe_bench.harness.image import Config
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_4756_to_563 import (
    ANT_DESIGN_4756_TO_563,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_10890_to_4765 import (
    ANT_DESIGN_10890_TO_4765,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_17846_to_10891 import (
    ANT_DESIGN_17846_TO_10891,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_25073_to_17847 import (
    ANT_DESIGN_25073_TO_17847,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_30655_to_25074 import (
    ANT_DESIGN_30655_TO_25074,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_35705_to_30656 import (
    ANT_DESIGN_35705_TO_30656,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_40793_to_35706 import (
    ANT_DESIGN_40793_TO_35706,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_46012_to_40794 import (
    ANT_DESIGN_46012_TO_40794,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_51609_to_46013 import (
    ANT_DESIGN_51609_TO_46013,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_55638_to_51610 import (
    ANT_DESIGN_55638_TO_51610,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_57633_to_55639 import (
    ANT_DESIGN_57633_TO_55639,
)


# (low, high, era class) -- inclusive and non-overlapping, ordered oldest first.
# Bounds come from the era module names; a PR that falls in a hole between two
# eras (e.g. 4757-4764) is rejected rather than handed to a neighbouring
# toolchain, because "close" is not the same as "the toolchain this commit
# pinned" and the failure would surface as an install error, not a test result.
_ERAS = [
    (563, 4756, ANT_DESIGN_4756_TO_563),
    (4765, 10890, ANT_DESIGN_10890_TO_4765),
    (10891, 17846, ANT_DESIGN_17846_TO_10891),
    (17847, 25073, ANT_DESIGN_25073_TO_17847),
    (25074, 30655, ANT_DESIGN_30655_TO_25074),
    (30656, 35705, ANT_DESIGN_35705_TO_30656),
    (35706, 40793, ANT_DESIGN_40793_TO_35706),
    (40794, 46012, ANT_DESIGN_46012_TO_40794),
    (46013, 51609, ANT_DESIGN_51609_TO_46013),
    (51610, 55638, ANT_DESIGN_55638_TO_51610),
    (55639, 57633, ANT_DESIGN_57633_TO_55639),
]


@Instance.register("ant-design", "ant-design")
class AntDesignDispatcher(Instance):
    """Delegates wholesale to the era class.

    `__new__` returns the era instance itself rather than wrapping it, so this
    module holds no images, scripts or parse_log of its own and therefore cannot
    drift from the era it stands in for. Python skips `__init__` when `__new__`
    returns an object that is not an instance of this class, which is why there
    is no `__init__` here.

    This registration deliberately shadows the one in ant_design.py: both claim
    the key `ant-design/ant-design`, and Instance.register overwrites on
    collision, so whichever module is imported LAST wins. The package
    `__init__.py` imports this file last for exactly that reason.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        for low, high, era_cls in _ERAS:
            if low <= pr.number <= high:
                return era_cls(pr, config, *args, **kwargs)
        raise ValueError(
            f"ant-design PR {pr.number} falls outside every configured era "
            f"({_ERAS[0][0]}-{_ERAS[-1][1]}); add an era module rather than "
            f"letting it build on the wrong toolchain"
        )
