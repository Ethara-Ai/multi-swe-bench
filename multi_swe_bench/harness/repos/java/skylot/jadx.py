"""Generic skylot/jadx config.

Used for jsonl records that carry no number_interval, so Instance.create
resolves them to the plain "skylot/jadx" key (the bundle-specific configs
register under jadx_<range> keys instead). These PRs sit on the v1.3.x/v1.4.x
release lines, which match the jadx_793_to_1831 environment (eclipse-temurin:11
+ Gradle), so this just re-registers that exact instance under the generic key.
"""

from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.repos.java.skylot.jadx_793_to_1831 import Jadx793To1831


@Instance.register("skylot", "jadx")
class Jadx(Jadx793To1831):
    pass
