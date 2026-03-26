import os
from multi_swe_bench.harness.instance import Instance
from .base import ThenewbostonWebsiteInstance

# Dynamic registration of thenewboston-blockchain/Website instances
Instance.register("thenewboston-blockchain", "Website")(ThenewbostonWebsiteInstance)
