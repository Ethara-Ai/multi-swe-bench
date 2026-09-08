"""Routes TeamNewPipe/NewPipe PRs to the right era config.

A dispatcher is needed here only because this dataset's rows carry an EMPTY
`number_interval`. Instance.create() then builds the key as `org/repo`
(instance.py:41-48), so all five PRs collapse onto the single key
`TeamNewPipe/NewPipe` and exactly one class can own it. When a dataset does carry
`number_interval`, each era module registers its own interval and the harness
routes straight to it -- no dispatcher, and this file would not exist.

Era split, measured at each PR's own base commit:

    1339 / 3278 / 3294 / 6319   Gradle 4.6-6.8.3   AGP 3.1.1-4.1.3   JDK 8
    12325                       Gradle 8.9         AGP 8.7.1         JDK 17

Gradle below 7 cannot run on JDK 17 and AGP 8.x cannot run on JDK 8, so the two
groups cannot share a base image.
"""

from multi_swe_bench.harness.image import Config
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.java.TeamNewPipe.NewPipe_6319_to_1339 import (
    NewPipeJdk8Era,
)
from multi_swe_bench.harness.repos.java.TeamNewPipe.NewPipe_12325_to_12325 import (
    NewPipeJdk17Era,
)

# (low, high, era class) -- inclusive and non-overlapping. The boundary sits
# anywhere between 6319 and 12325; 12000 is chosen so a later PR from either side
# lands on the toolchain that matches its own Gradle/AGP, not on whichever era
# happens to be listed first.
_ERAS = [
    (0, 11999, NewPipeJdk8Era),
    (12000, 99999, NewPipeJdk17Era),
]


@Instance.register("TeamNewPipe", "NewPipe")
class NewPipe(Instance):
    """Delegates wholesale to the era class.

    `__new__` returns the era instance itself rather than wrapping it, so this
    module holds no images, scripts or parse_log of its own and therefore cannot
    drift from the era it stands in for. Python skips `__init__` when `__new__`
    returns an object that is not an instance of this class, which is why there
    is no `__init__` here.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        for low, high, era_cls in _ERAS:
            if low <= pr.number <= high:
                return era_cls(pr, config, *args, **kwargs)
        raise ValueError(
            f"PR {pr.number} falls outside every configured NewPipe era; add an "
            f"era rather than letting it build on the wrong toolchain"
        )
