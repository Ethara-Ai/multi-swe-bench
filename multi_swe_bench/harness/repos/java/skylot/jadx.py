"""Generic skylot/jadx config (the only key the lht bundle dataset can reach).

The skylot__jadx_lht_final.jsonl records carry NO ``number_interval`` and NO
``tag``, so ``Instance.create`` resolves every one of them to the plain
``skylot/jadx`` key (see harness/instance.py). The range-keyed era configs
(``jadx_0_to_169`` / ``jadx_170_to_792`` / ``jadx_793_to_1831`` /
``jadx_1832_to_99999``) are therefore unreachable by routing on this dataset --
they would only fire if a record supplied ``number_interval``.

But the dataset spans the whole project history: bundles based on v0.6.x..v1.0.x
build under JDK 8, the v1.1.x..v1.4.x line under JDK 11, and the v1.5.x line
under JDK 21. Aliasing the generic key to a single JDK (the previous behaviour,
JDK 11 only) mis-toolchains the JDK 8 and JDK 21 bundles.

So this generic instance dispatches the build environment by ``pr.number`` --
the first PR of each bundle, i.e. the PR that defines the window's *start* tag /
base commit -- using the same boundaries as the era keys:

    number <= 792    -> JDK 8   (JadxJdk8Era2ImageDefault, jcommander 1.74->1.72)
    793 <= number <= 1831 -> JDK 11 (JadxJdk11ImageDefault, jcommander 1.80->1.78)
    number >= 1832   -> JDK 21  (JadxJdk21ImageDefault, no jcommander substitution)

Everything else (run/test_patch_run/fix_patch_run/parse_log, the binary-fixture
pre-extract, the shared-base + init.gradle setup) is identical across the eras,
so we subclass Jadx793To1831 and override only dependency() to pick the right
per-PR Image. Each per-PR Image points at its era's shared base image (one base
per JDK era, reused across all PRs), so swapping the class swaps the JDK era's
base *and* the matching Gradle tweaks in one step.
"""

from typing import Optional

from multi_swe_bench.harness.image import Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.repos.java.skylot.jadx_170_to_792 import (
    JadxJdk8Era2ImageDefault,
)
from multi_swe_bench.harness.repos.java.skylot.jadx_793_to_1831 import (
    Jadx793To1831,
    JadxJdk11ImageDefault,
)
from multi_swe_bench.harness.repos.java.skylot.jadx_1832_to_99999 import (
    JadxJdk21ImageDefault,
)

# PR-number boundaries between JDK build environments (inclusive upper bounds).
_JDK8_MAX = 792    # v0.6.x .. v1.0.x  -> eclipse-temurin:8
_JDK11_MAX = 1831  # v1.1.x .. v1.4.x  -> eclipse-temurin:11
# number >= 1832    v1.5.x ..          -> eclipse-temurin:21


@Instance.register("skylot", "jadx")
class Jadx(Jadx793To1831):
    def dependency(self) -> Optional[Image]:
        number = self.pr.number
        if number <= _JDK8_MAX:
            return JadxJdk8Era2ImageDefault(self.pr, self._config)
        if number <= _JDK11_MAX:
            return JadxJdk11ImageDefault(self.pr, self._config)
        return JadxJdk21ImageDefault(self.pr, self._config)


# Records whose `number_interval` field is set route via the key
# f"{org}/{number_interval}" (see harness/instance.py Instance.create), NOT via
# the plain "skylot/jadx" key. number_interval is the explicit dash-joined list
# of prs_in_bundle (e.g. "1505-1511-1530") -- NOT a range -- so each bundle that
# carries one needs its exact key registered. The Jadx dispatcher above already
# picks the JDK era by pr.number, so the SAME class serves every interval; we
# just bind it under each rebuilt bundle's number_interval here. Append more
# intervals to this list as other bundles get a number_interval.
_NUMBER_INTERVALS = [
    "1464-1467-1469-1478-1480-1483",
    "1505-1511-1530",
    "1787-1798-1804-1807-1811-1812-1813-1815",
    "2521-2525-2526-2527-2528-2533-2537-2538-2542-2543-2549-2557-2581-2592-2595-2602-2603-2614-2628",
]
for _ni in _NUMBER_INTERVALS:
    Instance.register("skylot", _ni)(Jadx)
