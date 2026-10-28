"""Routes TeamNewPipe/NewPipe PRs to the right era config.

A dispatcher is needed here only because this dataset's rows carry an EMPTY
`number_interval`. Instance.create() then builds the key as `org/repo`
(instance.py:41-48), so every PR collapses onto the single key
`TeamNewPipe/NewPipe` and exactly one class can own it. When a dataset does carry
`number_interval`, each era module registers its own interval and the harness
routes straight to it -- no dispatcher, and this file would not exist.

Routing is by the Gradle version pinned at each PR's own base commit, NOT by PR
number. A numeric boundary cannot work for this dataset because the Gradle
versions interleave against PR order -- e.g. PR 2335 already pins Gradle 7.4
while its neighbours 2309/2482 still pin Gradle 4.6-5.4.1, and Gradle 8.x PRs
(8221, 8837, 11712) sit well below the newest Gradle-6 PR (6447). The split that
actually matters is the JDK one:

    Gradle 2.x - 6.8.3   ->  JDK 8 base   (Gradle <= 6 refuses to run on JDK 17)
    Gradle 7.0+          ->  JDK 17 base  (Gradle 8.x REQUIRES JDK 17; Gradle 7
                                           runs on 17 and the AGP these PRs use
                                           needs 11+ anyway)

The map below is the measured toolchain for every PR in the shipped dataset,
read from gradle/wrapper/gradle-wrapper.properties at each base SHA. A PR not in
the map falls back to the Gradle-major heuristic is impossible here (the version
is not known without the tree), so unknown PRs raise rather than risk the wrong
base image.
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

# PR number -> era, derived from the Gradle version pinned at that PR's base
# commit. JDK 8 group is every PR on Gradle 2.x-6.8.3; JDK 17 group is every PR
# on Gradle 7.0+. Kept as an explicit table rather than a range because the two
# groups interleave by PR number and only the measured version separates them.
_JDK8_PRS = {
    397, 1145, 1654, 1825, 2067, 2309, 2482, 2862, 3267, 3478,
    4333, 5148, 5246, 5415, 5456, 5871, 5927, 6432, 6447,
    # pre-existing era members (Gradle 4.6-6.8.3)
    1339, 3278, 3294, 6319,
}
_JDK17_PRS = {
    2335, 6537, 7063, 7858, 8088, 8153, 8221, 8231, 8837, 9182,
    9711, 9869, 9937, 10283, 10470, 11712, 12435, 12746, 12926, 13388,
    # pre-existing era member (Gradle 8.9)
    12325,
}


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
        if pr.number in _JDK8_PRS:
            return NewPipeJdk8Era(pr, config, *args, **kwargs)
        if pr.number in _JDK17_PRS:
            return NewPipeJdk17Era(pr, config, *args, **kwargs)
        raise ValueError(
            f"PR {pr.number} has no measured toolchain mapping; read its pinned "
            f"Gradle version at the base commit and add it to _JDK8_PRS "
            f"(Gradle <= 6) or _JDK17_PRS (Gradle >= 7) rather than letting it "
            f"build on the wrong base image"
        )
